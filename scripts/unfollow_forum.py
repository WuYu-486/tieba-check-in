#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unfollow_forum.py - 取关指定吧并清理该吧的水贴/自动发贴配置(本地工具, 依赖 paramiko)
用法: python scripts/unfollow_forum.py <吧名> [更多吧名...]
说明:
  1. 通过服务器登录态调用移动端 favolike 接口取消关注;
  2. 同步从服务器 water_posts.txt 移除该吧的帖子(按水贴缓存中的吧名匹配),
     并清除其当日进度与开关状态;
  3. 同步从服务器 autopost_targets.json 移除该吧的自动发贴目标;
  4. 重写本地 water_posts.txt 并重建 docs/水贴目标清单.txt 与
     docs/自动发贴目标清单.txt。
  已经发出的帖子/回复不会撤回; 接口有频率限制(no=1990029), 失败请稍后重试。
退出码: 0=成功 1=未关注或状态待确认 2=失败 3=接口限频
"""

import json
import sys

import add_water
import autopost_manage
from add_water import load_deploy, ssh_connect

REMOTE_SCRIPT = r"""
import json
import os
import re
import time
import urllib.parse
import tieba_sign as ts
import tieba_water as tw

kws = json.loads({kws!r})
results = []
water_posts = tw.load_posts()
cache = tw.load_json(tw.THREAD_CACHE_FILE, {{}})
removed_water = []
changed_water = False
at_targets_file = os.path.join(tw.BASE, "autopost_targets.json")
at_state_file = os.path.join(tw.BASE, "autopost_state.json")
at_targets = tw.load_json(at_targets_file, [])
if not isinstance(at_targets, list):
    at_targets = []
removed_ap = []
changed_ap = False

bduss, stoken = ts.load_cookies()
cookie = ts.cookie_header(bduss, stoken)
status, text = ts.http(ts.MOINDEX_URL, cookie=cookie)
try:
    data = json.loads(text).get("data", {{}})
except ValueError:
    data = {{}}
uid = data.get("uid")
itb = data.get("itb_tbs")

def followed_names():
    st, t = ts.http(ts.MOINDEX_URL, cookie=cookie)
    try:
        return {{f.get("forum_name") for f in json.loads(t)["data"].get("like_forum", [])}}
    except Exception:
        return set()

for kw in kws:
    if not uid:
        results.append({{"kw": kw, "status": "auth", "error": "凭证失效"}})
        continue
    names = followed_names()
    fid = ""
    for item in data.get("like_forum", []):
        if item.get("forum_name") == kw:
            fid = str(item.get("forum_id") or "")
            break
    if kw not in names or not fid:
        results.append({{"kw": kw, "status": "not_followed"}})
        continue
    url = ("https://tieba.baidu.com/mo/q/favolike?uid=%s&itb_tbs=%s&fid=%s&kw=%s&is_cancel=1"
           % (uid, itb, fid, urllib.parse.quote(kw)))
    st, resp = ts.http(url, cookie=cookie)
    try:
        obj = json.loads(resp)
        no, msg = obj.get("no"), obj.get("error", "")
    except ValueError:
        no, msg = -1, resp[:80]
    if no == 1990029:
        results.append({{"kw": kw, "status": "rate_limit", "error": msg}})
        continue
    if no == 1990055:
        results.append({{"kw": kw, "status": "realname", "error": msg}})
        continue
    if no != 0:
        results.append({{"kw": kw, "status": "error",
                        "error": "no=%s %s" % (no, msg)}})
        continue
    verified = False
    for _ in range(6):
        time.sleep(5)
        if kw not in followed_names():
            verified = True
            break
    if not verified:
        results.append({{"kw": kw, "status": "pending",
                        "error": "接口返回成功, 但关注状态仍为已关注"}})
        continue
    results.append({{"kw": kw, "status": "ok", "verified": True}})

    # 清理该吧的水贴目标(依据水贴缓存中的吧名匹配)
    hit = [tid for tid in water_posts
           if (cache.get(tid) or {{}}).get("kw") == kw]
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
        removed_water.extend(hit)
        changed_water = True
        water_posts = tw.load_posts()

    kept = [t for t in at_targets if str(t.get("kw")) != kw]
    dropped = [str(t.get("tid")) for t in at_targets if str(t.get("kw")) == kw]
    if dropped:
        removed_ap.extend(dropped)
        changed_ap = True
        at_targets = kept
        astate = tw.load_json(at_state_file, {{}})
        for tid in dropped:
            (astate.get("posts") or {{}}).pop(tid, None)
        tw.save_json(at_state_file, astate)

if changed_ap:
    tmp = at_targets_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(at_targets, f, ensure_ascii=False, indent=1)
    os.replace(tmp, at_targets_file)

ap_state = tw.load_json(at_state_file, {{}})
if ap_state.get("date") != time.strftime("%Y-%m-%d"):
    ap_state = {{"posts": {{}}}}
ap_progress = {{str(t.get("tid")): (ap_state.get("posts") or {{}})
               .get(str(t.get("tid")), {{}}).get("sent", 0) for t in at_targets}}
print("RESULT:" + json.dumps({{
    "results": results,
    "removed_water": removed_water,
    "removed_autopost": removed_ap,
    "posts": tw.load_posts(),
    "map": {{tid: (cache.get(tid) or {{}}).get("kw") for tid in tw.load_posts()}},
    "disabled": sorted(tw.load_disabled()),
    "ap_targets": at_targets,
    "ap_progress": ap_progress,
}}, ensure_ascii=True))
"""


def run_remote(deploy, kws):
    t, sock = ssh_connect(deploy)
    script = REMOTE_SCRIPT.format(kws=json.dumps(kws))
    cmd = "cd %s && /opt/python/bin/python3 - <<'PYEOF'\n%s\nPYEOF" % (
        deploy["remote_dir"], script)
    chan = t.open_session(timeout=300)
    chan.exec_command(cmd)
    out = chan.makefile("rb").read().decode("utf-8", "replace")
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    if code != 0:
        print("错误: 远程取关失败(退出码 %d)\n%s" % (code, err[:800]), file=sys.stderr)
        sys.exit(2)
    for line in out.splitlines():
        if line.startswith("RESULT:"):
            return json.loads(line[7:])
    print("错误: 远程输出异常:\n%s" % out[:800], file=sys.stderr)
    sys.exit(2)


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if len(sys.argv) < 2:
        print("用法: python scripts/unfollow_forum.py <吧名> [更多吧名...]",
              file=sys.stderr)
        return 2
    kws = []
    for kw in sys.argv[1:]:
        kw = kw.strip()
        if kw and kw not in kws:
            kws.append(kw)

    deploy = load_deploy()
    data = run_remote(deploy, kws)

    rc = 0
    for r in data["results"]:
        st = r["status"]
        if st == "ok":
            print("[%s] 已取消关注" % r["kw"])
        elif st == "pending":
            print("[%s] 接口返回成功但状态仍为已关注, 未删除任何配置; 请稍后重试"
                  % r["kw"], file=sys.stderr)
            rc = max(rc, 1)
        elif st == "not_followed":
            print("[%s] 未关注该吧, 跳过" % r["kw"])
            rc = max(rc, 1)
        elif st == "rate_limit":
            print("[%s] 操作频繁被限流, 请稍后重试: %s" % (r["kw"], r.get("error", "")),
                  file=sys.stderr)
            rc = 3
        else:
            print("[%s] 取关失败: %s" % (r["kw"], r.get("error", st)),
                  file=sys.stderr)
            rc = max(rc, 1)

    if data["removed_water"]:
        print("已移除该吧水贴目标 %d 个: %s"
              % (len(data["removed_water"]), ", ".join(data["removed_water"])))
        add_water.write_local_posts(data["posts"])
        add_water.rebuild_doc(data["posts"], data["map"], data["disabled"])
    if data["removed_autopost"]:
        print("已移除该吧自动发贴目标 %d 个: %s"
              % (len(data["removed_autopost"]), ", ".join(data["removed_autopost"])))
    if data["removed_water"] or data["removed_autopost"]:
        autopost_manage.rebuild_doc(data["ap_targets"], data["ap_progress"])
        print("已重建本地 docs/水贴目标清单.txt 与 docs/自动发贴目标清单.txt")
    if not data["removed_water"] and not data["removed_autopost"]:
        print("该吧没有配置水贴或自动发贴目标")
    return rc


if __name__ == "__main__":
    sys.exit(main())