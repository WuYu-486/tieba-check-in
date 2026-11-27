#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autopost_manage.py - 自动发贴目标管理(本地工具, 依赖 paramiko, 改动直接落到服务器)
用法:
  python scripts/autopost_manage.py                       查询全部目标
  python scripts/autopost_manage.py add <帖子URL或tid> --content "内容" [--count N]
  python scripts/autopost_manage.py on  <帖子URL或tid>...  开启自动发贴
  python scripts/autopost_manage.py off <帖子URL或tid>...  关闭自动发贴
  python scripts/autopost_manage.py del <帖子URL或tid>...  删除目标
说明:
  新目标默认开启, 每日 13:05 由服务器 tieba_autopost.py 执行;
  改动即时写入服务器 autopost_targets.json, 不会立即发贴;
  每次操作后重建本地 docs/自动发贴目标清单.txt。
退出码: 0=成功 1=部分操作失败 2=失败
"""

import argparse
import datetime
import json
import sys
import unicodedata

import add_water
from add_water import load_deploy, pad, parse_tids, ssh_connect, disp_width

BASE = add_water.BASE
DOC_FILE = "%s/docs/自动发贴目标清单.txt" % BASE

REMOTE_SCRIPT = r"""
import json
import os
import time
import tieba_water as tw

op = {op!r}
tids = json.loads({tids!r})
content = {content!r}
count = {count!r}
targets_file = {targets_file!r}
state_file = {state_file!r}

raw = tw.load_json(targets_file, [])
targets = raw if isinstance(raw, list) else []
errors, changed = [], []

def index_of(tid):
    for i, t in enumerate(targets):
        if str(t.get("tid")) == tid:
            return i
    return -1

if op == "add":
    cache = tw.load_json(tw.THREAD_CACHE_FILE, {{}})
    bduss = tw.load_bduss()
    for tid in tids:
        if index_of(tid) >= 0:
            errors.append("%s 已在列表中" % tid)
            continue
        fid, kw, err = tw.resolve_thread(tid, bduss, cache)
        if err:
            errors.append("%s 解析失败(%s)" % (tid, err))
            continue
        targets.append({{"tid": tid, "kw": kw, "fid": fid, "content": content,
                        "count": count, "enabled": True}})
        changed.append(tid)
elif op in ("on", "off", "del"):
    for tid in tids:
        i = index_of(tid)
        if i < 0:
            errors.append("%s 不在列表中" % tid)
            continue
        if op == "del":
            targets.pop(i)
            state = tw.load_json(state_file, {{}})
            (state.get("posts") or {{}}).pop(tid, None)
            tw.save_json(state_file, state)
        else:
            targets[i]["enabled"] = (op == "on")
        changed.append(tid)

if changed:
    tmp = targets_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=1)
    os.replace(tmp, targets_file)

state = tw.load_json(state_file, {{}})
if state.get("date") != time.strftime("%Y-%m-%d"):
    state = {{"posts": {{}}}}
progress = {{str(t.get("tid")): (state.get("posts") or {{}})
             .get(str(t.get("tid")), {{}}).get("sent", 0) for t in targets}}
print("RESULT:" + json.dumps({{"targets": targets, "changed": changed,
      "errors": errors, "progress": progress}}, ensure_ascii=True))
"""


def run_remote(deploy, op, tids, content=None, count=None):
    t, sock = ssh_connect(deploy)
    script = REMOTE_SCRIPT.format(
        op=op, tids=json.dumps(tids), content=content, count=count,
        targets_file=deploy["remote_dir"] + "/autopost_targets.json",
        state_file=deploy["remote_dir"] + "/autopost_state.json")
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


def clip(text, width):
    """按显示宽度截断, 超出部分用 .. 表示"""
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if disp_width(text) <= width:
        return text
    out, w = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 2:
            break
        out += ch
        w += cw
    return out + ".."


def rebuild_doc(targets, progress):
    on_n = sum(1 for t in targets if t.get("enabled", True))
    rows = []
    for i, t in enumerate(targets, 1):
        tid = str(t.get("tid"))
        rows.append((str(i), str(t.get("kw") or tid), tid,
                     clip(str(t.get("content", "")), 24),
                     str(t.get("count", 1)),
                     "开启" if t.get("enabled", True) else "关闭",
                     "%d/%d" % (progress.get(tid, 0), int(t.get("count", 1))),
                     "https://tieba.baidu.com/p/%s" % tid))
    headers = ("序号", "吧名", "帖子 tid", "内容", "条数", "自动发贴", "今日进度")
    widths = [max([disp_width(headers[i])]
                  + [disp_width(r[i]) for r in rows]) + 2
              for i in range(len(headers))]
    lines = [
        "自动发贴目标清单",
        "================",
        "更新时间: %s" % datetime.date.today().isoformat(),
        "运行方式: 服务器 /opt/tieba-sign/tieba_autopost.py, cron 每天 13:05 触发"
        "(内含 0~3 分钟随机抖动)",
        "规则: 每个目标每天发送 n 条指定内容, 同一目标条间隔 5~8 秒, "
        "每条 +6 经验(客户端协议); 内容按目标配置, 当天重复运行不会超发",
        "管理命令(改动直接生效于服务器, 不会立即发贴):",
        "  python scripts/autopost_manage.py                      查询全部目标",
        "  python scripts/autopost_manage.py add <帖子URL或tid> --content \"内容\" [--count N]",
        "  python scripts/autopost_manage.py on|off <帖子URL或tid>...",
        "  python scripts/autopost_manage.py del <帖子URL或tid>...",
        "",
        "已配置目标(共 %d 个, 自动发贴开启 %d, 关闭 %d):"
        % (len(rows), on_n, len(rows) - on_n),
        "",
    ]
    if rows:
        lines.append("".join(pad(h, widths[i])
                             for i, h in enumerate(headers)).rstrip())
        for r in rows:
            lines.append((pad(r[0], widths[0]) + pad(r[1], widths[1])
                          + pad(r[2], widths[2]) + pad(r[3], widths[3])
                          + pad(r[4], widths[4]) + pad(r[5], widths[5])
                          + pad(r[6], widths[6]) + r[7]).rstrip())
    else:
        lines.append("(暂无目标, 用 add 命令添加)")
    lines += [
        "",
        "备注:",
        "- 关闭的目标不参与每日自动发贴, 保留配置与当日进度, 随时可 on 恢复",
        "- 删除目标同时清除其当日进度, 重新添加从头开始",
        "- 运行日志: 服务器 /opt/tieba-sign/tieba-autopost.log",
        "- 异常告警: 任务非零退出(凭证失效/部分失败/验证码重试超限等)自动发告警邮件",
        "- 周报: 每周五 22:05 与签到、水贴合并推送, 运行总结与经验统计包含本功能,",
        "  明细在 \"五、自动发贴明细\" 附件中",
    ]
    with open(DOC_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def print_table(targets, progress):
    on_n = sum(1 for t in targets if t.get("enabled", True))
    rows = []
    for i, t in enumerate(targets, 1):
        tid = str(t.get("tid"))
        rows.append((str(i), str(t.get("kw") or tid), tid,
                     clip(str(t.get("content", "")), 24),
                     "%d/%d" % (progress.get(tid, 0), int(t.get("count", 1))),
                     "开启" if t.get("enabled", True) else "关闭"))
    headers = ("序号", "吧名", "帖子 tid", "内容", "今日进度", "自动发贴")
    if rows:
        widths = [max([disp_width(headers[i])]
                      + [disp_width(r[i]) for r in rows]) + 2
                  for i in range(len(headers))]
        print("".join(pad(h, widths[i])
                      for i, h in enumerate(headers)).rstrip())
        for r in rows:
            print("".join(pad(c, widths[i])
                          for i, c in enumerate(r)).rstrip())
    else:
        print("(暂无自动发贴目标)")
    print("\n共 %d 个目标: 开启 %d, 关闭 %d"
          % (len(targets), on_n, len(targets) - on_n))


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="自动发贴目标管理")
    parser.add_argument("op", nargs="?", default="list",
                        choices=["list", "add", "on", "off", "del"],
                        help="list=查询(默认) add=新增 on/off=开关 del=删除")
    parser.add_argument("targets", nargs="*", help="帖子URL或tid, 可多个")
    parser.add_argument("--content", help="发贴内容(add 必填, 单行)")
    parser.add_argument("--count", type=int, default=1,
                        help="每个目标每天发送条数(add 用, 默认 1)")
    args = parser.parse_args()

    if args.op != "list" and not args.targets:
        parser.error("%s 需要提供帖子URL或tid" % args.op)
    if args.op == "list" and args.targets:
        parser.error("查询无需帖子参数")

    content = None
    if args.op == "add":
        content = (args.content or "").strip()
        if not content:
            parser.error("add 需要 --content 指定发贴内容")
        if "\n" in content or "\r" in content:
            parser.error("--content 只支持单行文本")
        if not 1 <= args.count <= 50:
            parser.error("--count 取值范围 1~50")

    tids = parse_tids(args.targets) if args.op != "list" else []
    if args.op != "list" and not tids:
        print("错误: 没有可解析的帖子ID", file=sys.stderr)
        return 2

    deploy = load_deploy()
    op = None if args.op == "list" else args.op
    data = run_remote(deploy, op, tids,
                      content=content, count=args.count if op == "add" else None)

    for msg in data["errors"]:
        print("警告: %s" % msg)
    if data["changed"]:
        print("服务器配置已更新(%d 个目标, 改动不会立即发贴)" % len(data["changed"]))
    elif op:
        print("没有发生改动")

    print_table(data["targets"], data["progress"])
    rebuild_doc(data["targets"], data["progress"])
    print("\n已重建本地 docs/自动发贴目标清单.txt")
    return 1 if data["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())