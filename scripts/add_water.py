#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_water.py - 一键添加水贴(本地工具, 依赖 paramiko)
用法:
  python scripts/add_water.py <帖子URL或tid> [更多URL/tid...] [--dry-run] [--no-water]
  例: python scripts/add_water.py https://tieba.baidu.com/p/8752415673
      python scripts/add_water.py 8752415673 9786663118
说明:
  吧名解析需要贴吧登录态, 本机匿名访问会被 403, 因此解析与服务器列表更新
  都通过 SSH 在服务器上完成(凭证见同目录 deploy.ini, gitignore 排除);
  添加成功后立即在服务器执行水贴, 自动补齐今日 4 条(已有进度的帖子会跳过);
  新帖子默认开启自动水贴(关闭状态由 water_switch.py 管理);
  另外自动更新本地 water_posts.txt 并重建 docs/水贴目标清单.txt。
  --dry-run 只解析并预览, 不做任何修改; --no-water 只添加不立即水贴。
退出码: 0=成功 1=部分失败 2=失败
"""

import argparse
import configparser
import datetime
import json
import os
import re
import sys
import unicodedata

BASE = os.path.dirname(os.path.abspath(__file__))
# 本地仓库中脚本统一放在 scripts/, 配置与数据在仓库根目录; 服务器部署时全部平铺同目录
if os.path.basename(BASE) == "scripts":
    BASE = os.path.dirname(BASE)
DEPLOY_FILE = os.path.join(BASE, "deploy.ini")
POSTS_FILE = os.path.join(BASE, "water_posts.txt")
DOC_FILE = os.path.join(BASE, "docs", "水贴目标清单.txt")

POSTS_HEADER = ("# 水贴目标列表: 每行一个帖子 URL 或 tid, # 开头为注释\n"
                "# 每帖每天发 4 条(贴吧经验上限), 修改后次日生效(当日进度见 water_state.json)\n")

REMOTE_SCRIPT = r"""
import json
import os
import tieba_water as tw
tids = json.loads({tids!r})
bduss = tw.load_bduss()
cache = tw.load_json(tw.THREAD_CACHE_FILE, {{}})
existing = tw.load_posts()
results = []
for tid in tids:
    fid, kw, err = tw.resolve_thread(tid, bduss, cache)
    results.append({{"tid": tid, "fid": fid, "kw": kw, "err": err,
                   "existed": tid in existing}})
added = []
if not {dry_run!r}:
    raw = ""
    if os.path.exists(tw.POSTS_FILE):
        with open(tw.POSTS_FILE, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    for r in results:
        if not r["err"] and not r["existed"]:
            with open(tw.POSTS_FILE, "a", encoding="utf-8") as f:
                if raw and not raw.endswith("\n"):
                    f.write("\n")
                f.write(r["tid"] + "\n")
            raw = r["tid"] + "\n"
            added.append(r["tid"])
    # 新加入的帖子默认开启自动水贴(清除历史遗留的关闭状态)
    disabled = tw.load_disabled()
    if disabled.intersection(added):
        tw.save_switch(disabled.difference(added))
final_posts = tw.load_posts()
mapping = {{tid: (cache.get(tid) or {{}}).get("kw") for tid in final_posts}}
print("RESULT:" + json.dumps({{"results": results, "added": added,
      "posts": final_posts, "map": mapping,
      "disabled": sorted(tw.load_disabled())}}, ensure_ascii=True))
"""


def load_deploy():
    if not os.path.exists(DEPLOY_FILE):
        print("错误: 未找到 deploy.ini (SSH 连接配置, 参考仓库说明创建)", file=sys.stderr)
        sys.exit(2)
    cfg = configparser.ConfigParser()
    cfg.read(DEPLOY_FILE, encoding="utf-8")
    return {k: cfg.get("ssh", k) for k in
            ("host", "user", "password", "hostkey", "remote_dir")} | {
        "port": cfg.getint("ssh", "port", fallback=22)}


def parse_tids(args):
    tids = []
    for arg in args:
        m = re.search(r"/p/(\d{6,12})\b", arg) or \
            re.fullmatch(r"(\d{6,12})", arg.strip())
        if not m:
            print("警告: 无法解析为帖子ID: %s" % arg, file=sys.stderr)
            continue
        if m.group(1) not in tids:
            tids.append(m.group(1))
    return tids


def ssh_connect(deploy):
    try:
        import paramiko
        import hashlib
        import base64
        import socket
    except ImportError:
        print("错误: 需要 paramiko (pip install paramiko)", file=sys.stderr)
        sys.exit(2)

    infos = socket.getaddrinfo(deploy["host"], deploy["port"],
                               socket.AF_INET6, socket.SOCK_STREAM)
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.settimeout(15)
    sock.connect(infos[0][4])
    t = paramiko.Transport(sock)
    t.start_client(timeout=15)
    key = t.get_remote_server_key()
    fp = "SHA256:" + base64.b64encode(
        hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
    if fp != deploy["hostkey"]:
        t.close(); sock.close()
        print("错误: 服务器 host key 指纹不匹配(%s), 已中止" % fp, file=sys.stderr)
        sys.exit(2)
    t.auth_password(deploy["user"], deploy["password"])
    if not t.is_authenticated():
        t.close(); sock.close()
        print("错误: SSH 认证失败", file=sys.stderr)
        sys.exit(2)
    return t, sock


def ssh_resolve(deploy, tids, dry_run):
    t, sock = ssh_connect(deploy)
    script = REMOTE_SCRIPT.format(tids=json.dumps(tids), dry_run=dry_run)
    cmd = "cd %s && /opt/python/bin/python3 - <<'PYEOF'\n%s\nPYEOF" % (
        deploy["remote_dir"], script)
    chan = t.open_session(timeout=120)
    chan.exec_command(cmd)
    out = chan.makefile("rb").read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    if code != 0:
        print("错误: 远程解析失败(退出码 %d)\n%s" % (code, err[:500]), file=sys.stderr)
        sys.exit(2)
    for line in out.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[7:])
    print("错误: 远程输出异常:\n%s" % out[:500], file=sys.stderr)
    sys.exit(2)


def run_water_remote(deploy, new_tids):
    """在服务器立即执行水贴任务(已有今日进度的帖子自动跳过), 返回退出码"""
    t, sock = ssh_connect(deploy)
    cmd = "cd %s && /opt/python/bin/python3 tieba_water.py --now" % deploy["remote_dir"]
    chan = t.open_session(timeout=900)
    chan.exec_command(cmd)
    out = chan.makefile("rb").read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    prefixes = tuple("[%s]" % tid for tid in new_tids)
    for line in out.splitlines():
        if (line.startswith(prefixes) or "水贴完成" in line
                or "异常" in line or "错误" in line or "失败" in line):
            print(line)
    if code != 0:
        print("警告: 即时水贴未完全成功(退出码 %d):\n%s" % (code, err[:300]),
              file=sys.stderr)
    return code


def update_local_posts(tids):
    existing = set()
    raw = ""
    if os.path.exists(POSTS_FILE):
        with open(POSTS_FILE, "r", encoding="utf-8-sig") as f:
            raw = f.read()
        for line in raw.splitlines():
            m = re.fullmatch(r"(\d{6,12})", line.strip())
            if m:
                existing.add(m.group(1))
    else:
        with open(POSTS_FILE, "w", encoding="utf-8") as f:
            f.write(POSTS_HEADER)
    new = [tid for tid in tids if tid not in existing]
    if new:
        with open(POSTS_FILE, "a", encoding="utf-8") as f:
            if raw and not raw.endswith("\n"):
                f.write("\n")
            for tid in new:
                f.write(tid + "\n")
    return new


def parse_own_names():
    """从现有文档保留人工标注(如"自己的水楼")"""
    marks = {}
    if os.path.exists(DOC_FILE):
        with open(DOC_FILE, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^(\S.*?)\s+(\d{10,})\s+(?:\S+\s+)?https://", line)
                if m and "自己的水楼" in m.group(1):
                    marks[m.group(2)] = "自己的水楼"
    return marks


def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in s)


def pad(s, width):
    return s + " " * max(0, width - disp_width(s))


def rebuild_doc(posts, mapping, disabled=()):
    marks = parse_own_names()
    disabled = set(disabled)
    rows = []
    for tid in posts:
        kw = mapping.get(tid) or tid
        name = "%s(%s)" % (kw, marks[tid]) if tid in marks else kw
        status = "关闭" if tid in disabled else "开启"
        rows.append((name, tid, status, "https://tieba.baidu.com/p/%s" % tid))
    w1 = max([disp_width("吧名")] + [disp_width(r[0]) for r in rows]) + 2
    w2 = max([disp_width("帖子 tid")] + [len(r[1]) for r in rows]) + 2
    w3 = disp_width("自动水贴") + 2
    off_n = sum(1 for r in rows if r[2] == "关闭")
    on_n = len(rows) - off_n
    lines = [
        "贴吧水贴目标清单",
        "================",
        "更新时间: %s" % datetime.date.today().isoformat(),
        "运行方式: 服务器 /opt/tieba-sign/tieba_water.py, cron 每天 09:05 触发"
        "(内含 0~3 分钟随机抖动)",
        "规则: 每帖每天 4 条(贴吧经验上限), 每条 +6 经验(客户端协议), "
        "同帖同日内容一致(水/3/+3/6 随机选一)",
        "配置文件: 服务器 /opt/tieba-sign/water_posts.txt (仓库根目录有同名副本); "
        "推荐用 python scripts/add_water.py 一键添加, "
        "自动同步两边并立即完成当日水贴",
        "开关管理: python scripts/water_switch.py [on|off] <帖子URL或tid>, "
        "关闭的帖子不参与每日水贴(新加入默认开启)",
        "",
        "已加入水贴的吧及帖子(共 %d 个吧, %d 个帖子; 自动水贴开启 %d, 关闭 %d):"
        % (len(rows), len(rows), on_n, off_n),
        "",
        pad("吧名", w1) + pad("帖子 tid", w2) + pad("自动水贴", w3) + "帖子链接",
        "-" * w1 + "  " + "-" * w2 + "  " + "-" * w3 + "  " + "-" * 40,
    ]
    for name, tid, status, url in rows:
        lines.append(pad(name, w1) + pad(tid, w2) + pad(status, w3) + url)
    lines += [
        "",
        "备注:",
        "- 标注\"(自己的水楼)\"的为自己的帖子, 其余为吧友指定的公共水楼",
        "- 满额运行每天最多 +%d 经验(%d 帖开启 x 4 条 x 6, 各吧经验独立计算)"
        % (on_n * 24, on_n),
        "- 当日进度记录在服务器 water_state.json, 重复运行不会超发",
        "- 增删目标: 推荐 python scripts/add_water.py <帖子URL或tid> "
        "(自动同步服务器与本文档, 并立即完成当日 4 条水贴);",
        "  也可手动编辑 water_posts.txt 后上传服务器, 当天想立即生效可执行",
        "  /opt/python/bin/python3 /opt/tieba-sign/tieba_water.py --now",
        "- 开关: 关闭后不触发每日自动水贴, 但保留列表和当日进度; "
        "重新开启即时恢复(切换状态本身不会立即水贴)",
        "- 运行日志: 服务器 /opt/tieba-sign/tieba-water.log",
        "- 异常告警: 任务非零退出(凭证失效/部分失败/验证码重试超限等)自动发告警邮件",
        "- 周报: 每周五 22:05 与签到合并推送, 正文含运行总结与各吧经验(签到+水贴融合),",
        "  签到明细与水贴明细在 .txt 附件中",
    ]
    with open(DOC_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="一键添加贴吧水贴")
    parser.add_argument("targets", nargs="+", help="帖子URL或tid, 可多个")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析预览, 不做任何修改")
    parser.add_argument("--no-water", action="store_true",
                        help="只添加列表, 不立即执行今日水贴(次日 09:05 自动生效)")
    args = parser.parse_args()

    tids = parse_tids(args.targets)
    if not tids:
        print("错误: 没有可解析的帖子ID", file=sys.stderr)
        return 2

    deploy = load_deploy()
    data = ssh_resolve(deploy, tids, args.dry_run)

    ok_n = 0
    for r in data["results"]:
        if r["err"]:
            print("[%s] 解析失败(%s)" % (r["tid"], r["err"]))
            continue
        ok_n += 1
        state = "已存在" if r["existed"] else ("将添加" if args.dry_run else "已添加")
        print("[%s] 吧=%s fid=%s -> %s" % (r["tid"], r["kw"], r["fid"], state))

    if args.dry_run:
        print("dry-run: 未修改任何文件")
        return 0 if ok_n == len(tids) else 1
    if not data["added"]:
        print("没有新增帖子, 本地文件未变动")
        return 0 if ok_n == len(tids) else 1

    update_local_posts(data["added"])
    rebuild_doc(data["posts"], data["map"], data.get("disabled", []))
    print("已更新本地 water_posts.txt 与 docs/水贴目标清单.txt")
    print("服务器列表已同步")
    rc = 0 if ok_n == len(tids) else 1
    if args.no_water:
        print("已跳过即时水贴(--no-water), 次日 09:05 自动生效")
    else:
        print("开始即时水贴(今日已完成的水贴会自动跳过)...")
        wrc = run_water_remote(deploy, data["added"])
        if wrc != 0:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())