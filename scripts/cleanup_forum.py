#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_forum.py - 清理某个吧的水贴/自动发贴目标(本地工具, 依赖 paramiko)
用法: python scripts/cleanup_forum.py <吧名> [更多吧名...]
说明:
  从服务器 water_posts.txt 移除属于该吧的帖子(按水贴缓存中的吧名匹配),
  并清除其当日进度与开关状态; 同时从 autopost_targets.json 移除该吧的自动发贴目标;
  最后重写本地 water_posts.txt 并重建 docs/水贴目标清单.txt、
  docs/自动发贴目标清单.txt。不涉及关注/取关操作, 已发出的帖子不会撤回。
退出码: 0=已清理 1=未找到该吧的目标 2=失败
"""

import json
import os
import sys

import add_water
import autopost_manage
from add_water import load_deploy, ssh_connect

REMOTE_SCRIPT = r"""
import json
import os
import re
import time
import tieba_water as tw

kws = set(json.loads({kws!r}))
cache = tw.load_json(tw.THREAD_CACHE_FILE, {{}})
posts = tw.load_posts()
hit = [tid for tid in posts if (cache.get(tid) or {{}}).get("kw") in kws]

if hit:
    with open(tw.POSTS_FILE, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    keep = []
    for line in raw.splitlines():
        s = line.strip()
        m = re.search(r"/p/(\d{{6,12}})\b", s) or re.fullmatch(r"(\d{{6,12}})", s)
        if m and m.group(1) in hit:
            continue
        keep.append(line)
    with open(tw.POSTS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(keep).rstrip("\n") + "\n")
    state = tw.load_json(tw.STATE_FILE, {{}})
    for tid in hit:
        (state.get("posts") or {{}}).pop(tid, None)
    tw.save_json(tw.STATE_FILE, state)
    disabled = tw.load_disabled()
    if disabled.intersection(hit):
        tw.save_switch(disabled.difference(hit))

at_file = os.path.join(tw.BASE, "autopost_targets.json")
at_state_file = os.path.join(tw.BASE, "autopost_state.json")
targets = tw.load_json(at_file, [])
if not isinstance(targets, list):
    targets = []
dropped = [str(t.get("tid")) for t in targets if str(t.get("kw")) in kws]
if dropped:
    kept = [t for t in targets if str(t.get("kw")) not in kws]
    tmp = at_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=1)
    os.replace(tmp, at_file)
    astate = tw.load_json(at_state_file, {{}})
    for tid in dropped:
        (astate.get("posts") or {{}}).pop(tid, None)
    tw.save_json(at_state_file, astate)
else:
    kept = targets

astate = tw.load_json(at_state_file, {{}})
if astate.get("date") != time.strftime("%Y-%m-%d"):
    astate = {{"posts": {{}}}}
ap_progress = {{str(t.get("tid")): (astate.get("posts") or {{}})
               .get(str(t.get("tid")), {{}}).get("sent", 0) for t in kept}}
posts = tw.load_posts()
print("RESULT:" + json.dumps({{
    "removed_water": hit,
    "removed_autopost": dropped,
    "posts": posts,
    "map": {{tid: (cache.get(tid) or {{}}).get("kw") for tid in posts}},
    "disabled": sorted(tw.load_disabled()),
    "ap_targets": kept,
    "ap_progress": ap_progress,
}}, ensure_ascii=True))
"""


def run_remote(deploy, kws):
    t, sock = ssh_connect(deploy)
    script = REMOTE_SCRIPT.format(kws=json.dumps(kws))
    cmd = "cd %s && /opt/python/bin/python3 - <<'PYEOF'\n%s\nPYEOF" % (
        deploy["remote_dir"], script)
    chan = t.open_session(timeout=120)
    chan.exec_command(cmd)
    out = chan.makefile("rb").read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    if code != 0:
        print("错误: 远程清理失败(退出码 %d)\n%s" % (code, err[:500]), file=sys.stderr)
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
        print("用法: python scripts/cleanup_forum.py <吧名> [更多吧名...]",
              file=sys.stderr)
        return 2
    kws = []
    for kw in sys.argv[1:]:
        kw = kw.strip()
        if kw and kw not in kws:
            kws.append(kw)

    data = run_remote(load_deploy(), kws)
    removed = data["removed_water"] + data["removed_autopost"]
    if not removed:
        print("未找到这些吧的水贴/自动发贴目标: %s (按水贴缓存的吧名匹配)"
              % "、".join(kws))
        return 1

    if data["removed_water"]:
        print("已删除水贴目标 %d 个: %s"
              % (len(data["removed_water"]), ", ".join(data["removed_water"])))
    if data["removed_autopost"]:
        print("已删除自动发贴目标 %d 个: %s"
              % (len(data["removed_autopost"]), ", ".join(data["removed_autopost"])))

    add_water.write_local_posts(data["posts"])
    add_water.rebuild_doc(data["posts"], data["map"], data["disabled"])
    autopost_manage.rebuild_doc(data["ap_targets"], data["ap_progress"])
    print("已同步本地 water_posts.txt 并重建两份目标清单文档")
    return 0


if __name__ == "__main__":
    sys.exit(main())