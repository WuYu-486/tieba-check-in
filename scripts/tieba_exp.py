#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tieba_exp.py - 关注吧经验查询(本地工具, 依赖 paramiko)
用法:
  python scripts/tieba_exp.py                 查询全部关注吧: 当前等级/经验/距下一级
  python scripts/tieba_exp.py --sort name     排序: need(默认, 差得最少在前)|level|name
说明:
  经验阈值来自 docs/贴吧各等级经验值.md, 修改该文档表格即可调整计算口径;
  等级与当前经验通过服务器登录态查询(不写日志、不改服务器状态)。
退出码: 0=成功 2=失败
"""

import argparse
import json
import os
import re
import sys

from add_water import BASE, load_deploy, pad, ssh_connect, disp_width

LEVEL_DOC = os.path.join(BASE, "docs", "贴吧各等级经验值.md")

REMOTE_SCRIPT = r"""
import json
import tieba_sign as ts

bduss, stoken = ts.load_cookies()
if not bduss:
    print("RESULT:" + json.dumps({"error": "未找到 BDUSS"}))
else:
    cookie = ts.cookie_header(bduss, stoken)
    status, text = ts.http(ts.MOINDEX_URL, cookie=cookie)
    try:
        obj = json.loads(text)
        data = obj.get("data", {})
    except ValueError:
        data = {}
    if not data.get("uid"):
        print("RESULT:" + json.dumps({"error": "凭证失效(未登录)"}))
    else:
        forums = []
        for item in data.get("like_forum", []):
            if item.get("is_like") == 0:
                continue
            forums.append({
                "kw": item.get("forum_name", ""),
                "level": int(item.get("user_level") or 0),
                "exp": int(item.get("user_exp") or 0),
                "signed": int(item.get("is_sign") or 0),
                "fid": str(item.get("forum_id") or ""),
            })
        print("RESULT:" + json.dumps({"forums": forums}, ensure_ascii=True))
"""


def load_thresholds():
    """解析等级经验文档 -> [(等级, 累计经验), ...]"""
    if not os.path.exists(LEVEL_DOC):
        print("错误: 未找到 %s" % LEVEL_DOC, file=sys.stderr)
        sys.exit(2)
    rows = []
    with open(LEVEL_DOC, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|", line)
            if m:
                rows.append((int(m.group(1)), int(m.group(2))))
    if len(rows) < 2:
        print("错误: 等级经验文档中没有可解析的表格行", file=sys.stderr)
        sys.exit(2)
    for i, (lv, exp) in enumerate(rows, 1):
        if lv != i:
            print("错误: 等级不连续(第 %d 行等级为 %d), 请检查等级经验文档" % (i, lv),
                  file=sys.stderr)
            sys.exit(2)
        if i > 1 and exp <= rows[i - 2][1]:
            print("错误: 等级 %d 的累计经验未递增, 请检查等级经验文档" % lv,
                  file=sys.stderr)
            sys.exit(2)
    return rows


def run_remote(deploy):
    t, sock = ssh_connect(deploy)
    cmd = "cd %s && /opt/python/bin/python3 - <<'PYEOF'\n%s\nPYEOF" % (
        deploy["remote_dir"], REMOTE_SCRIPT)
    chan = t.open_session(timeout=120)
    chan.exec_command(cmd)
    out = chan.makefile("rb").read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    if code != 0:
        print("错误: 远程查询失败(退出码 %d)\n%s" % (code, err[:500]), file=sys.stderr)
        sys.exit(2)
    for line in out.splitlines():
        if line.startswith("RESULT:"):
            data = json.loads(line[7:])
            if data.get("error"):
                print("错误: %s" % data["error"], file=sys.stderr)
                sys.exit(2)
            return data["forums"]
    print("错误: 远程输出异常:\n%s" % out[:500], file=sys.stderr)
    sys.exit(2)


def compute(forums, thresholds):
    """给每个吧补充: 当前等级经验阈值 / 下一级阈值 / 还差经验"""
    lv_exp = dict(thresholds)
    rows = []
    for f in forums:
        lv = f["level"] if f["level"] > 0 else 1
        next_lv = lv + 1
        if next_lv in lv_exp:
            need = max(0, lv_exp[next_lv] - f["exp"])
            target = next_lv
        else:
            need = None
            target = None
        rows.append({**f, "next_level": target, "need": need})
    return rows


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="关注吧经验查询")
    parser.add_argument("--sort", choices=["need", "level", "name"], default="need",
                        help="排序方式: need=距升级最少在前(默认) level name")
    args = parser.parse_args()

    thresholds = load_thresholds()
    forums = run_remote(load_deploy())
    rows = compute(forums, thresholds)
    if args.sort == "level":
        rows.sort(key=lambda r: (-r["level"], r["kw"]))
    elif args.sort == "name":
        rows.sort(key=lambda r: r["kw"])
    else:
        rows.sort(key=lambda r: (r["need"] is None, r["need"] or 0, r["kw"]))

    table = []
    for i, r in enumerate(rows, 1):
        table.append((
            str(i), r["kw"], str(r["level"]), str(r["exp"]),
            str(r["next_level"]) if r["next_level"] else "满级",
            str(r["need"]) if r["need"] is not None else "—",
            "已签" if r["signed"] else "未签",
        ))
    headers = ("序号", "吧名", "等级", "当前经验", "下一级", "还差", "今日签到")
    widths = [max([disp_width(headers[i])]
                  + [disp_width(t[i]) for t in table]) + 2
              for i in range(len(headers))]
    print("".join(pad(h, widths[i]) for i, h in enumerate(headers)).rstrip())
    for t in table:
        print("".join(pad(c, widths[i]) for i, c in enumerate(t)).rstrip())

    max_lv = thresholds[-1][0]
    full = sum(1 for r in rows if r["level"] >= max_lv)
    signed = sum(1 for r in rows if r["signed"])
    print("\n共 %d 个吧: 满级(%d级) %d 个, 今日已签到 %d 个"
          % (len(rows), max_lv, full, signed))
    print("经验阈值: docs/贴吧各等级经验值.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())