#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tieba_water.py - 百度贴吧水贴(客户端协议, 每帖每日4条, 每条+6经验, 纯标准库)
用法: python3 tieba_water.py [--now]   (--now 跳过启动随机延迟, 手动测试用)
配置: 同目录 water_posts.txt, 每行一个帖子 URL 或 tid, # 开头为注释
凭证: 同目录 cookies 文件(BDUSS=xxx)
状态: water_state.json 记录当日进度, 重复运行不会超发
设备: water_device.json 保存固定设备指纹(首次运行自动生成), 防风控
开关: water_switch.json 记录已关闭的帖子, 默认全部开启; 关闭的不参与本次水贴,
      由本地 water_switch.py 管理
退出码: 0=全部成功  1=部分失败  2=凭证失效  3=接口异常
"""

import gzip
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
_cacert = os.path.join(BASE, "cacert.pem")
if os.path.exists(_cacert):
    os.environ.setdefault("SSL_CERT_FILE", _cacert)

COOKIE_FILE = os.path.join(BASE, "cookies")
POSTS_FILE = os.path.join(BASE, "water_posts.txt")
STATE_FILE = os.path.join(BASE, "water_state.json")
DEVICE_FILE = os.path.join(BASE, "water_device.json")
THREAD_CACHE_FILE = os.path.join(BASE, "water_threads.json")
SWITCH_FILE = os.path.join(BASE, "water_switch.json")
LOG_FILE = os.path.join(BASE, "tieba-water.log")

TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
POST_URL = "https://tieba.baidu.com/c/c/post/add"
THREAD_PAGE_URL = "https://tieba.baidu.com/mo/q/m?kz={tid}&pn=1"

CLIENT_UA = "bdtb for Android 12.57.1.0"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")
SIGN_KEY = "tiebaclient!!!"

PER_POST_COUNT = 4            # 每帖每日条数(贴吧经验上限)
MSG_DELAY = (3.0, 5.0)        # 同帖消息间隔(秒)
POST_DELAY = (8.0, 20.0)      # 帖与帖之间间隔(秒)
START_JITTER = (0, 180)       # 启动随机延迟(秒), 避免每天固定时刻触发
CONTENT_POOL = ["水", "3", "+3", "6"]
VCODE_MAX_RETRY = 3           # 验证码重试上限, 超过报异常

EXIT_OK, EXIT_PARTIAL, EXIT_AUTH, EXIT_ERROR = 0, 1, 2, 3


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def http(url, data=None, ua=WEB_UA, cookie=None, retries=2, raw=False):
    """GET/POST, 返回 (status, text); raw=True 时返回原始字节; 自动解 gzip; GET 自动重试"""
    headers = {"User-Agent": ua}
    if cookie:
        headers["Cookie"] = cookie
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    last_err = None
    attempts = 1 + (retries if data is None else 0)
    for i in range(attempts):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw_bytes = resp.read()
                if raw_bytes[:2] == b"\x1f\x8b":
                    raw_bytes = gzip.decompress(raw_bytes)
                return resp.status, raw_bytes if raw else raw_bytes.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                raw_bytes = e.read()
                if raw_bytes[:2] == b"\x1f\x8b":
                    raw_bytes = gzip.decompress(raw_bytes)
                return e.code, raw_bytes if raw else raw_bytes.decode("utf-8", "replace")
            except Exception:
                return e.code, b"" if raw else ""
        except Exception as e:
            last_err = str(e)
            if i + 1 < attempts:
                time.sleep(2)
    return -1, ("network error: %s" % last_err).encode() if raw else "network error: %s" % last_err


def load_bduss():
    if not os.path.exists(COOKIE_FILE):
        return ""
    with open(COOKIE_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line.startswith("BDUSS="):
                return line[6:]
    return ""


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def load_disabled():
    """已关闭自动水贴的 tid 集合; 文件缺失或损坏时视为全部开启"""
    raw = load_json(SWITCH_FILE, {})
    if isinstance(raw, dict):
        raw = raw.get("disabled", [])
    if not isinstance(raw, list):
        return set()
    return {str(t) for t in raw}


def save_switch(disabled):
    """原子写入开关文件, 避免写一半被打断导致状态丢失"""
    tmp = SWITCH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"disabled": sorted(disabled)}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SWITCH_FILE)


def load_device():
    """固定设备指纹: 同一账号长期用同一设备身份, 比每次随机更不易触发风控"""
    dev = load_json(DEVICE_FILE, None)
    if not dev:
        dev = {
            "cuid": "baidutiebaapp" + str(uuid.uuid4()),
            "client_id": "wappc_%d_%03d" % (int(time.time() * 1000), random.randint(100, 999)),
            "model": "Pixel XL",
            "client_version": "12.57.1.0",
        }
        save_json(DEVICE_FILE, dev)
    return dev


def client_post(bduss, dev, params):
    """客户端协议 POST: 参数按字母序拼接+固定key取大写MD5作为sign"""
    p = dict(params)
    p.update({
        "BDUSS": bduss,
        "_client_id": dev["client_id"],
        "_client_type": "2",
        "_client_version": dev["client_version"],
        "_phone_imei": "000000000000000",
        "cuid": dev["cuid"],
        "model": dev["model"],
        "net_type": "1",
        "timestamp": str(int(time.time() * 1000)),
    })
    s = "".join(k + "=" + str(p[k]) for k in sorted(p))
    p["sign"] = hashlib.md5((s + SIGN_KEY).encode("utf-8")).hexdigest().upper()
    return http(POST_URL, data=p, ua=CLIENT_UA)


def get_tbs(bduss):
    status, text = http(TBS_URL, cookie="BDUSS=" + bduss)
    if status != 200:
        return None, "error"
    try:
        obj = json.loads(text)
    except ValueError:
        return None, "error"
    if not obj.get("is_login"):
        return None, "auth"
    return obj.get("tbs"), None


def load_posts():
    """从 water_posts.txt 解析 tid 列表(支持完整分享URL或纯数字)"""
    tids = []
    if not os.path.exists(POSTS_FILE):
        return tids
    with open(POSTS_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.search(r"/p/(\d{6,12})\b", line) or re.fullmatch(r"(\d{6,12})", line)
            if m:
                tid = m.group(1)
                if tid not in tids:
                    tids.append(tid)
            else:
                log("警告: 无法解析帖子行: %s" % line[:80])
    return tids


def resolve_thread(tid, bduss, cache):
    """取帖子所属吧的 fid 和 kw(移动网页版帖子页解析), 结果缓存
    部分页面没有 forum_id 字段, 备用: 提取吧名后走 fnameShareApi 查 fid"""
    if tid in cache:
        return cache[tid]["fid"], cache[tid]["kw"], None
    status, page = http(THREAD_PAGE_URL.format(tid=tid), cookie="BDUSS=" + bduss, raw=True)
    if status != 200:
        return None, None, "error"
    # 页面为 GBK 编码, 在原始字节上提取, 避免整页按 UTF-8 解码产生乱码
    kw_m = re.search(rb'f\?kw=([^"&]+)', page)
    if not kw_m:
        return None, None, "error"
    raw_kw = kw_m.group(1).decode("ascii")
    try:
        kw = urllib.parse.unquote(raw_kw, errors="strict")
    except UnicodeDecodeError:
        kw = urllib.parse.unquote(raw_kw, encoding="gbk", errors="replace")
    fid_m = re.search(rb'"forum_id"\s*:\s*"?(\d+)', page)
    if fid_m:
        fid = fid_m.group(1).decode("ascii")
    else:
        api = "https://tieba.baidu.com/f/commit/share/fnameShareApi?ie=utf-8&fname=" + urllib.parse.quote(kw)
        status, text = http(api, cookie="BDUSS=" + bduss)
        try:
            fid = str(json.loads(text)["data"]["fid"])
        except (ValueError, KeyError, TypeError):
            return None, None, "error"
    cache[tid] = {"fid": fid, "kw": kw}
    save_json(THREAD_CACHE_FILE, cache)
    return fid, kw, None


class WaterError(Exception):
    pass


def reply_once(bduss, dev, tid, fid, kw, content, tbs):
    """发一条回复, 返回 (ok, detail, new_tbs)
    验证码/风控类错误抛 WaterError 由上层重试; 凭证失效返回 auth"""
    status, text = client_post(bduss, dev, {
        "tid": tid, "fid": fid, "kw": kw, "content": content,
        "tbs": tbs, "quote_id": "", "anonymous": "0",
        "entrance_type": "1", "new_vcode": "1", "vcode_tag": "12",
    })
    if status != 200:
        raise WaterError("HTTP %s" % status)
    try:
        obj = json.loads(text)
    except ValueError:
        raise WaterError("bad json: %s" % text[:80])
    code = str(obj.get("error_code"))
    msg = obj.get("error_msg", "")
    if code == "0":
        inc = obj.get("exp", {}).get("inc", "?")
        return True, "pid=%s exp+%s" % (obj.get("pid"), inc), tbs
    if "登录" in msg or code in ("4", "5"):
        return False, "auth", tbs
    need_vcode = obj.get("info", {}).get("need_vcode", "0")
    if need_vcode not in ("0", "") or code == "40" or "验证码" in msg:
        raise WaterError("触发验证码(code=%s)" % code)
    if code == "300000":
        # 吧信息获取失败: 刷新 tbs 重试一次通常可解
        raise WaterError("吧信息获取失败(code=300000)")
    raise WaterError("code=%s %s" % (code, msg))


def water_post(bduss, dev, tid, fid, kw, ps, state, tbs):
    """补齐某帖当日剩余的条数, 每条成功立即落盘(中断/重试不会超发)
    返回 (tbs, 状态)"""
    content = ps["content"]
    while ps["sent"] < PER_POST_COUNT:
        ok, detail, tbs = reply_once(bduss, dev, tid, fid, kw, content, tbs)
        if not ok and detail == "auth":
            return tbs, "auth"
        ps["sent"] += 1
        save_json(STATE_FILE, state)
        log("[%s] 第%d/%d条 成功(%s) 内容=%r" % (tid, ps["sent"], PER_POST_COUNT, detail, content))
        if ps["sent"] < PER_POST_COUNT:
            time.sleep(random.uniform(*MSG_DELAY))
    return tbs, "ok"


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    log("=" * 50)
    log("贴吧水贴启动")

    if "--now" not in sys.argv:
        jitter = random.uniform(*START_JITTER)
        log("启动随机延迟 %.0f 秒" % jitter)
        time.sleep(jitter)

    bduss = load_bduss()
    if not bduss:
        log("错误: 未找到 BDUSS, 请配置 cookies 文件")
        return EXIT_ERROR
    tbs, err = get_tbs(bduss)
    if err == "auth":
        log("错误: 凭证失效(未登录), 请重新获取 BDUSS")
        return EXIT_AUTH
    if err is not None:
        log("错误: tbs 接口异常")
        return EXIT_ERROR

    tids = load_posts()
    if not tids:
        log("错误: water_posts.txt 为空或不存在, 请先配置帖子列表")
        return EXIT_ERROR
    log("目标帖子 %d 个: %s" % (len(tids), ", ".join(tids)))
    disabled = load_disabled()
    if disabled:
        log("已关闭自动水贴 %d 个: %s" % (len(disabled), ", ".join(sorted(disabled))))

    dev = load_device()
    cache = load_json(THREAD_CACHE_FILE, {})
    today = time.strftime("%Y-%m-%d")
    state = load_json(STATE_FILE, {})
    if state.get("date") != today:
        state = {"date": today, "posts": {}}
    posts_state = state.setdefault("posts", {})

    ok_n, fail_n, skip_n, off_n = 0, 0, 0, 0
    for i, tid in enumerate(tids):
        if tid in disabled:
            log("[%s] 已关闭自动水贴, 跳过" % tid)
            off_n += 1
            continue
        ps = posts_state.setdefault(tid, {"sent": 0, "content": random.choice(CONTENT_POOL)})
        if ps["sent"] >= PER_POST_COUNT:
            log("[%s] 今日已完成%d条, 跳过" % (tid, ps["sent"]))
            skip_n += 1
            continue
        fid, kw, err = resolve_thread(tid, bduss, cache)
        if err:
            log("[%s] 失败: 无法获取帖子所属吧信息" % tid)
            fail_n += 1
            continue
        content = ps["content"]
        log("[%s] 吧=%s 今日常用内容=%r 已发%d条" % (tid, kw, content, ps["sent"]))
        try:
            tbs, status = water_post(bduss, dev, tid, fid, kw, ps, state, tbs)
        except WaterError as e:
            # 验证码等风控: 换 tbs 退避重试, 超上限报异常
            log("[%s] 异常: %s, 开始重试(上限%d次)" % (tid, e, VCODE_MAX_RETRY))
            status = "fail"
            for attempt in range(1, VCODE_MAX_RETRY + 1):
                time.sleep(random.uniform(8.0, 15.0))
                new_tbs, err2 = get_tbs(bduss)
                if err2 == "auth":
                    status = "auth"
                    break
                if new_tbs:
                    tbs = new_tbs
                try:
                    tbs, status = water_post(bduss, dev, tid, fid, kw, ps, state, tbs)
                    break
                except WaterError as e2:
                    log("[%s] 重试%d/%d 仍失败: %s" % (tid, attempt, VCODE_MAX_RETRY, e2))
            if status not in ("ok", "auth"):
                log("[%s] 异常: 重试超过上限, 放弃该帖(%s)" % (tid, e))
        save_json(STATE_FILE, state)
        if status == "auth":
            log("错误: 凭证失效, 中止")
            return EXIT_AUTH
        if status == "ok":
            ok_n += 1
        else:
            fail_n += 1
        if i < len(tids) - 1:
            time.sleep(random.uniform(*POST_DELAY))

    save_json(STATE_FILE, state)
    log("-" * 50)
    summary = "水贴完成: 成功 %d 帖, 失败 %d 帖, 已完成跳过 %d 帖" % (
        ok_n, fail_n, skip_n)
    if off_n:
        summary += ", 已关闭跳过 %d 帖" % off_n
    log(summary)
    return EXIT_PARTIAL if fail_n else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())