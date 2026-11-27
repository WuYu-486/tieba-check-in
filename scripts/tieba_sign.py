#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tieba_sign.py - 百度贴吧自动签到(网页版接口, 纯标准库, 无第三方依赖)
用法: python3 tieba_sign.py
凭证: 同目录 cookies 文件(BDUSS=xxx / STOKEN=xxx 两行)
退出码: 0=全部成功  1=部分失败  2=凭证失效  3=接口异常
"""

import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
# 本地仓库中脚本统一放在 scripts/, 配置与数据在仓库根目录; 服务器部署时全部平铺同目录
if os.path.basename(BASE) == "scripts":
    BASE = os.path.dirname(BASE)
# 优先使用随程序部署的 CA 证书包(服务器系统证书过旧时避免 SSL 验证失败)
_cacert = os.path.join(BASE, "cacert.pem")
if os.path.exists(_cacert):
    os.environ.setdefault("SSL_CERT_FILE", _cacert)

COOKIE_FILE = os.path.join(BASE, "cookies")
LOG_FILE = os.path.join(BASE, "tieba-run.log")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
SIGN_URL = "https://tieba.baidu.com/sign/add"
MYLIKE_URL = "https://tieba.baidu.com/f/like/mylike?&pn={pn}"
MOINDEX_URL = "https://tieba.baidu.com/mo/q/newmoindex"

# 签到间隔(秒), 随机化防风控
SIGN_DELAY = (2.0, 6.0)


def http(url, data=None, cookie="", charset="utf-8", retries=2):
    """GET/POST, 返回 (status, text); data 为 dict 时以 form 编码提交
    GET 请求自动重试(服务器到百度 CDN 存在间歇性抖动)"""
    headers = {
        "User-Agent": UA,
        "Referer": "https://tieba.baidu.com/index.html",
    }
    if cookie:
        headers["Cookie"] = cookie
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data, encoding=charset).encode("utf-8")
    last_err = None
    attempts = 1 + (retries if data is None else 0)
    for _ in range(attempts):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception:
                return e.code, ""
        except Exception as e:
            last_err = str(e)
            if _ + 1 < attempts:
                time.sleep(2)
    return -1, "network error: %s" % last_err

EXIT_OK, EXIT_PARTIAL, EXIT_AUTH, EXIT_ERROR = 0, 1, 2, 3


def log(msg):
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_cookies():
    """读取 BDUSS 与 STOKEN"""
    bduss, stoken = "", ""
    if not os.path.exists(COOKIE_FILE):
        return bduss, stoken
    with open(COOKIE_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line.startswith("BDUSS="):
                bduss = line[6:]
            elif line.startswith("STOKEN="):
                # 多个 STOKEN 时保留最后一个非空值, 以文件为准
                if line[7:]:
                    stoken = line[7:]
    return bduss, stoken


def cookie_header(bduss, stoken):
    c = "BDUSS=%s" % bduss
    if stoken:
        c += "; STOKEN=%s" % stoken
    return c


def get_tbs(cookie):
    status, text = http(TBS_URL, cookie=cookie)
    if status != 200:
        return None, EXIT_ERROR
    try:
        obj = json.loads(text)
    except ValueError:
        return None, EXIT_ERROR
    if not obj.get("is_login"):
        return None, EXIT_AUTH
    return obj.get("tbs"), None


def fetch_moindex(cookie):
    """newmoindex 一次获取三样: 登录态验证 + tbs + 关注列表
    返回 (tbs, forums, err): forums 为 [(kw, is_sign), ...]; err 为 None 或 "auth"/"error"
    注: 该接口响应自带 uid/tbs/like_forum, 且在 IPv6 服务器上比 tbs 单独接口稳定"""
    status, text = http(MOINDEX_URL, cookie=cookie)
    if status != 200:
        return None, None, "error"
    try:
        obj = json.loads(text)
        data = obj.get("data", {})
        if not data.get("uid"):
            return None, None, "auth"  # 无 uid = 未登录
        tbs = data.get("tbs")
        forums = []
        for item in data.get("like_forum", []):
            kw = item.get("forum_name", "")
            if not kw:
                continue
            if item.get("is_like", 1) == 0:
                continue  # 关注但未加入会员的吧, 签到必失败(1011), 跳过
            forums.append((kw, item.get("is_sign", 0)))
        if tbs is None or not forums:
            return None, None, "error"
        return tbs, forums, None
    except (ValueError, KeyError, TypeError):
        return None, None, "error"


def get_forums_mylike(cookie):
    """网页版 mylike 翻页解析(备用方案): [(kw, is_sign), ...]"""
    forums = []
    pn = 1
    while True:
        status, text = http(MYLIKE_URL.format(pn=pn), cookie=cookie)
        if status != 200:
            break
        # 每行形如: <a href="/f?kw=xxx" title="吧名">吧名</a> ... 是否已签由整块结构判断
        block = re.findall(r'kw=([^"&]+)"[^>]*>([^<]+)</a>', text)
        if not block:
            break
        new_kw = [urllib.parse.unquote(k) for k, _ in block]
        for kw in new_kw:
            if kw not in [f[0] for f in forums]:
                forums.append((kw, 0))
        if len(text) < 5000:  # 最后一页
            break
        pn += 1
        if pn > 50:
            break
    return forums


def sign_one(kw, tbs, cookie):
    """签到单个吧, 返回 (状态, 详情)
    状态: ok=签到成功, signed=今日已签, skip=未加入等级不足(跳过), fail=失败, auth=凭证失效"""
    status, text = http(SIGN_URL, data={"kw": kw, "tbs": tbs},
                        cookie=cookie, charset="gbk")
    if status != 200:
        return "fail", "HTTP %s" % status
    try:
        obj = json.loads(text)
    except ValueError:
        return "fail", "bad json: %s" % text[:80]
    no = obj.get("no")
    err = obj.get("error", "")
    if no == 0:
        return "ok", ""
    if no in (1101, 1102) or "已经签到" in err or "签过" in err:
        return "signed", err
    if no == 1011:
        return "skip", err  # 未加入此吧或等级不够
    if no == 1 and "tbs" in err.lower():
        return "fail_tbs", err
    if "登录" in err or "bduss" in err.lower():
        return "auth", err
    return "fail", "no=%s %s" % (no, err)


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    log("=" * 50)
    log("贴吧自动签到启动")

    bduss, stoken = load_cookies()
    if not bduss:
        log("错误: 未找到 BDUSS, 请配置 cookies 文件")
        return EXIT_ERROR
    cookie = cookie_header(bduss, stoken)

    # 1. 登录验证 + tbs + 关注列表(一次请求全拿)
    tbs, forums, err = fetch_moindex(cookie)
    if err == "auth":
        log("错误: 凭证失效(未登录), 请重新获取 BDUSS/STOKEN")
        return EXIT_AUTH
    if err is not None:
        log("错误: 接口异常(登录验证/列表获取失败)")
        return EXIT_ERROR
    log("登录验证通过, 共关注 %d 个贴吧" % len(forums))

    todo = [kw for kw, is_sign in forums if not is_sign]
    signed_already = len(forums) - len(todo)
    log("待签到 %d 个(今日已签 %d 个)" % (len(todo), signed_already))

    # 3. 逐吧签到
    ok, signed, skipped, failed = 0, signed_already, 0, 0
    for i, kw in enumerate(todo, 1):
        state, detail = sign_one(kw, tbs, cookie)
        if state == "ok":
            ok += 1
            log("(%d/%d) [%s] 签到成功" % (i, len(todo), kw))
        elif state == "signed":
            signed += 1
        elif state == "skip":
            skipped += 1
            log("(%d/%d) [%s] 跳过(未加入)" % (i, len(todo), kw))
        elif state == "fail_tbs":
            log("(%d/%d) [%s] tbs 失效, 重新获取" % (i, len(todo), kw))
            tbs, _, err2 = fetch_moindex(cookie)
            if tbs is None:
                log("错误: 重新获取 tbs 失败(%s), 凭证可能已失效" % err2)
                return EXIT_AUTH
            state, detail = sign_one(kw, tbs, cookie)
            if state == "ok":
                ok += 1
                log("(%d/%d) [%s] 签到成功(重试)" % (i, len(todo), kw))
            else:
                failed += 1
                log("(%d/%d) [%s] 失败: %s" % (i, len(todo), kw, detail))
        elif state == "auth":
            log("错误: 签到过程中凭证失效: %s" % detail)
            return EXIT_AUTH
        else:
            failed += 1
            log("(%d/%d) [%s] 失败: %s" % (i, len(todo), kw, detail))
        if i < len(todo):
            time.sleep(random.uniform(*SIGN_DELAY))

    # 4. 汇总
    log("-" * 50)
    log("签到完成: 成功 %d, 已签 %d, 跳过 %d, 失败 %d" % (ok, signed, skipped, failed))
    if failed:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
