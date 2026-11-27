#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
water_switch.py - 水贴开关管理(本地工具, 依赖 paramiko)
用法:
  python scripts/water_switch.py                        查看全部帖子: 开关状态 / 今日进度
  python scripts/water_switch.py off <帖子URL或tid>...   关闭自动水贴
  python scripts/water_switch.py on  <帖子URL或tid>...   重新开启自动水贴
说明:
  新加入的帖子默认开启; 状态保存在服务器 water_switch.json, 修改即时生效,
  只决定每日 09:05 是否对该帖发帖, 切换状态本身不会触发水贴;
  修改后自动重建本地 docs/水贴目标清单.txt 的状态列。
退出码: 0=成功 1=部分帖子不在列表 2=失败
"""

import argparse
import json
import sys

import add_water
from add_water import load_deploy, parse_tids, ssh_connect
from tieba_water import PER_POST_COUNT

REMOTE_SCRIPT = r"""
import json
import time
import tieba_water as tw

tids = json.loads({tids!r})
action = {action!r}
posts = tw.load_posts()
cache = tw.load_json(tw.THREAD_CACHE_FILE, {{}})
disabled = tw.load_disabled()
not_found = [tid for tid in tids if tid not in posts]
changed = []
if action:
    for tid in tids:
        if tid not in posts:
            continue
        if action == "off" and tid not in disabled:
            disabled.add(tid)
            changed.append(tid)
        elif action == "on" and tid in disabled:
            disabled.discard(tid)
            changed.append(tid)
    if changed:
        tw.save_switch(disabled)
        for tid in changed:
            kw = (cache.get(tid) or {{}}).get("kw") or tid
            tw.log("[%s] 吧=%s 自动水贴已%s" % (tid, kw,
                   "关闭" if action == "off" else "开启"))
state = tw.load_json(tw.STATE_FILE, {{}})
if state.get("date") != time.strftime("%Y-%m-%d"):
    state = {{"posts": {{}}}}
progress = {{tid: (state.get("posts") or {{}}).get(tid, {{}}).get("sent", 0)
            for tid in posts}}
print("RESULT:" + json.dumps({{"posts": posts,
      "map": {{tid: (cache.get(tid) or {{}}).get("kw") for tid in posts}},
      "disabled": sorted(disabled), "changed": changed,
      "not_found": not_found, "progress": progress}}, ensure_ascii=True))
"""


def run_remote(deploy, tids, action):
    t, sock = ssh_connect(deploy)
    script = REMOTE_SCRIPT.format(tids=json.dumps(tids), action=action)
    cmd = "cd %s && /opt/python/bin/python3 - <<'PYEOF'\n%s\nPYEOF" % (
        deploy["remote_dir"], script)
    chan = t.open_session(timeout=120)
    chan.exec_command(cmd)
    out = chan.makefile("rb").read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    if code != 0:
        print("错误: 远程操作失败(退出码 %d)\n%s" % (code, err[:500]),
              file=sys.stderr)
        sys.exit(2)
    for line in out.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[7:])
    print("错误: 远程输出异常:\n%s" % out[:500], file=sys.stderr)
    sys.exit(2)


def print_table(data):
    disabled = set(data["disabled"])
    rows = []
    for i, tid in enumerate(data["posts"], 1):
        rows.append((str(i), data["map"].get(tid) or "?",
                     tid, "关闭" if tid in disabled else "开启",
                     "%d/%d" % (data["progress"].get(tid, 0), PER_POST_COUNT)))
    headers = ("序号", "吧名", "帖子 tid", "自动水贴", "今日进度")
    widths = [max([add_water.disp_width(headers[i])]
                  + [add_water.disp_width(r[i]) for r in rows]) + 2
              for i in range(len(headers))]
    print("".join(add_water.pad(h, widths[i])
                  for i, h in enumerate(headers)).rstrip())
    for r in rows:
        print("".join(add_water.pad(c, widths[i])
                      for i, c in enumerate(r)).rstrip())
    off_n = sum(1 for tid in data["posts"] if tid in disabled)
    print("\n共 %d 个帖子: 开启 %d, 关闭 %d"
          % (len(data["posts"]), len(data["posts"]) - off_n, off_n))


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="贴吧水贴开关管理")
    parser.add_argument("action", nargs="?", choices=["on", "off"],
                        help="on=开启 off=关闭, 省略则仅查看状态")
    parser.add_argument("targets", nargs="*", help="帖子URL或tid, 可多个")
    args = parser.parse_args()
    if args.action and not args.targets:
        parser.error("on/off 需要提供帖子URL或tid")
    if not args.action and args.targets:
        parser.error("查看状态无需帖子参数, 切换开关请用 on/off")

    tids = []
    if args.action:
        tids = parse_tids(args.targets)
        if not tids:
            print("错误: 没有可解析的帖子ID", file=sys.stderr)
            return 2

    deploy = load_deploy()
    data = run_remote(deploy, tids, args.action)

    if args.action:
        for tid in data["not_found"]:
            print("警告: 服务器列表中没有该帖子: %s" % tid)
        if data["changed"]:
            print("开关已保存(即时生效, 不会立即触发水贴)")
        else:
            print("开关状态未变化(已是目标状态)")

    print_table(data)

    if args.action and data["changed"]:
        add_water.rebuild_doc(data["posts"], data["map"], data["disabled"])
        print("\n已重建本地 docs/水贴目标清单.txt")
    return 1 if data["not_found"] else 0


if __name__ == "__main__":
    sys.exit(main())