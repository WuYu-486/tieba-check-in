#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
water_delete.py - 删除水贴目标(本地工具, 依赖 paramiko)
用法: python scripts/water_delete.py <帖子URL或tid> [更多...]
说明:
  从服务器 water_posts.txt 移除目标, 同时清除该帖的当日进度与开关状态,
  并同步重写本地 water_posts.txt / 重建 docs/水贴目标清单.txt;
  只影响后续水贴, 已经发出的回复不会撤回。
退出码: 0=成功 1=部分帖子不在列表 2=失败
"""

import json
import sys

import add_water
from add_water import load_deploy, parse_tids, ssh_connect

REMOTE_SCRIPT = r"""
import json
import re
import tieba_water as tw

tids = json.loads({tids!r})
posts = tw.load_posts()
removed = [t for t in tids if t in posts]
not_found = [t for t in tids if t not in posts]

if removed:
    with open(tw.POSTS_FILE, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    keep = []
    for line in raw.splitlines():
        s = line.strip()
        m = re.search(r"/p/(\d{{6,12}})\b", s) or re.fullmatch(r"(\d{{6,12}})", s)
        if m and m.group(1) in removed:
            continue
        keep.append(line)
    with open(tw.POSTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(keep).rstrip("\n") + "\n")

    state = tw.load_json(tw.STATE_FILE, {{}})
    for tid in removed:
        (state.get("posts") or {{}}).pop(tid, None)
    tw.save_json(tw.STATE_FILE, state)

    disabled = tw.load_disabled()
    hit = disabled.intersection(removed)
    if hit:
        tw.save_switch(disabled.difference(removed))

cache = tw.load_json(tw.THREAD_CACHE_FILE, {{}})
posts = tw.load_posts()
print("RESULT:" + json.dumps({{
    "removed": removed, "not_found": not_found, "posts": posts,
    "map": {{tid: (cache.get(tid) or {{}}).get("kw") for tid in posts}},
    "disabled": sorted(tw.load_disabled()),
}}, ensure_ascii=True))
"""


def run_remote(deploy, tids):
    t, sock = ssh_connect(deploy)
    script = REMOTE_SCRIPT.format(tids=json.dumps(tids))
    cmd = "cd %s && /opt/python/bin/python3 - <<'PYEOF'\n%s\nPYEOF" % (
        deploy["remote_dir"], script)
    chan = t.open_session(timeout=120)
    chan.exec_command(cmd)
    out = chan.makefile("rb").read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    if code != 0:
        print("错误: 远程删除失败(退出码 %d)\n%s" % (code, err[:500]), file=sys.stderr)
        sys.exit(2)
    for line in out.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[7:])
    print("错误: 远程输出异常:\n%s" % out[:500], file=sys.stderr)
    sys.exit(2)


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print("用法: python scripts/water_delete.py <帖子URL或tid> ...",
              file=sys.stderr)
        return 2
    tids = parse_tids(sys.argv[1:])
    if not tids:
        print("错误: 没有可解析的帖子ID", file=sys.stderr)
        return 2

    deploy = load_deploy()
    data = run_remote(deploy, tids)
    for tid in data["removed"]:
        print("[%s] 已从水贴目标删除(含当日进度/开关状态)" % tid)
    for tid in data["not_found"]:
        print("警告: 水贴列表中不存在: %s" % tid)

    if data["removed"]:
        add_water.write_local_posts(data["posts"])
        add_water.rebuild_doc(data["posts"], data["map"], data["disabled"])
        print("已同步本地 water_posts.txt 与 docs/水贴目标清单.txt")
    return 1 if data["not_found"] else 0


if __name__ == "__main__":
    sys.exit(main())