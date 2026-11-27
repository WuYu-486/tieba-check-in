#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tieba_autopost.py - 百度贴吧自动发贴(客户端协议, 按目标每日发送指定内容, 纯标准库)
用法: python3 tieba_autopost.py [--now] [--dry-run]
      --now 跳过启动随机延迟(手动测试用); --dry-run 只打印计划, 不发贴不写状态
配置: 同目录 autopost_targets.json, 由 autopost_manage.py 维护:
      [{"tid": "9078893516", "kw": "失忆投捕", "fid": "123",
        "content": "早安", "count": 2, "enabled": true}]
凭证: 同目录 cookies 文件(BDUSS=xxx)
状态: autopost_state.json 记录当日进度, 重复运行不会超发
日志: tieba-autopost.log
退出码: 0=全部成功  1=部分失败  2=凭证失效  3=接口异常
"""

import json
import os
import random
import sys
import time

import tieba_water as tw

BASE = os.path.dirname(os.path.abspath(__file__))
# 本地仓库中脚本统一放在 scripts/, 配置与数据在仓库根目录; 服务器部署时全部平铺同目录
if os.path.basename(BASE) == "scripts":
    BASE = os.path.dirname(BASE)

TARGETS_FILE = os.path.join(BASE, "autopost_targets.json")
STATE_FILE = os.path.join(BASE, "autopost_state.json")
LOG_FILE = os.path.join(BASE, "tieba-autopost.log")

MSG_DELAY = (5.0, 8.0)        # 同一目标的消息间隔(秒)
POST_DELAY = (8.0, 20.0)      # 目标与目标之间间隔(秒)
START_JITTER = (0, 180)       # 启动随机延迟(秒)
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


def load_targets():
    raw = tw.load_json(TARGETS_FILE, [])
    if not isinstance(raw, list):
        log("警告: autopost_targets.json 格式错误, 应按列表配置")
        return []
    targets = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("tid", "")).strip()
        content = str(item.get("content", "")).strip()
        try:
            count = int(item.get("count", 1))
        except (TypeError, ValueError):
            count = 0
        if not (tid.isdigit() and 6 <= len(tid) <= 12) or not content or count < 1:
            log("警告: 配置项无效, 已忽略: %r" % item)
            continue
        targets.append({
            "tid": tid,
            "kw": str(item.get("kw") or ""),
            "fid": str(item.get("fid") or ""),
            "content": content,
            "count": count,
            "enabled": bool(item.get("enabled", True)),
        })
    return targets


def send_target(bduss, dev, tgt, ps, state, tbs):
    """补齐某目标当日剩余条数, 每条成功立即落盘(中断/重试不会超发)"""
    while ps["sent"] < tgt["count"]:
        ok, detail, tbs = tw.reply_once(bduss, dev, tgt["tid"], tgt["fid"],
                                        tgt["kw"], tgt["content"], tbs)
        if not ok and detail == "auth":
            return tbs, "auth"
        ps["sent"] += 1
        tw.save_json(STATE_FILE, state)
        log("[%s] 第%d/%d条 成功(%s) 内容=%r"
            % (tgt["tid"], ps["sent"], tgt["count"], detail, tgt["content"]))
        if ps["sent"] < tgt["count"]:
            time.sleep(random.uniform(*MSG_DELAY))
    return tbs, "ok"


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    dry_run = "--dry-run" in sys.argv
    log("=" * 50)
    log("自动发贴启动" + ("(dry-run)" if dry_run else ""))

    targets = load_targets()
    if not targets:
        log("未配置自动发贴目标(autopost_targets.json), 本次跳过")
        return EXIT_OK

    today = time.strftime("%Y-%m-%d")
    state = tw.load_json(STATE_FILE, {})
    if state.get("date") != today:
        state = {"date": today, "posts": {}}
    posts_state = state.setdefault("posts", {})

    if dry_run:
        for tgt in targets:
            sent = (posts_state.get(tgt["tid"]) or {}).get("sent", 0)
            plan = max(0, tgt["count"] - sent)
            log("[%s] 吧=%s 状态=%s 计划发送%d条(今日已发%d/%d) 内容=%r"
                % (tgt["tid"], tgt["kw"] or "?", "开启" if tgt["enabled"] else "关闭",
                   plan, sent, tgt["count"], tgt["content"]))
        log("-" * 50)
        log("dry-run: 未发送任何内容, 未写入状态")
        return EXIT_OK

    if "--now" not in sys.argv:
        jitter = random.uniform(*START_JITTER)
        log("启动随机延迟 %.0f 秒" % jitter)
        time.sleep(jitter)

    enabled = [t for t in targets if t["enabled"]]
    if not enabled:
        log("所有目标均已关闭自动发贴, 本次跳过")
        return EXIT_OK

    bduss = tw.load_bduss()
    if not bduss:
        log("错误: 未找到 BDUSS, 请配置 cookies 文件")
        return EXIT_ERROR
    tbs, err = tw.get_tbs(bduss)
    if err == "auth":
        log("错误: 凭证失效(未登录), 请重新获取 BDUSS")
        return EXIT_AUTH
    if err is not None:
        log("错误: tbs 接口异常")
        return EXIT_ERROR

    dev = tw.load_device()
    cache = tw.load_json(tw.THREAD_CACHE_FILE, {})

    ok_n, fail_n, skip_n, off_n = 0, 0, 0, 0
    for i, tgt in enumerate(targets):
        tid = tgt["tid"]
        if not tgt["enabled"]:
            log("[%s] 已关闭自动发贴, 跳过" % tid)
            off_n += 1
            continue
        ps = posts_state.setdefault(tid, {"sent": 0})
        if ps["sent"] >= tgt["count"]:
            log("[%s] 今日已完成%d条, 跳过" % (tid, ps["sent"]))
            skip_n += 1
            continue
        fid, kw = tgt["fid"], tgt["kw"]
        if not fid or not kw:
            fid, kw, err = tw.resolve_thread(tid, bduss, cache)
            if err:
                log("[%s] 失败: 无法获取帖子所属吧信息" % tid)
                fail_n += 1
                continue
        log("[%s] 吧=%s 目标%d条 已发%d条 内容=%r"
            % (tid, kw, tgt["count"], ps["sent"], tgt["content"]))
        try:
            tbs, status = send_target(
                bduss, dev,
                {"tid": tid, "fid": fid, "kw": kw,
                 "content": tgt["content"], "count": tgt["count"]},
                ps, state, tbs)
        except tw.WaterError as e:
            log("[%s] 异常: %s, 开始重试(上限%d次)" % (tid, e, VCODE_MAX_RETRY))
            status = "fail"
            for attempt in range(1, VCODE_MAX_RETRY + 1):
                time.sleep(random.uniform(8.0, 15.0))
                new_tbs, err2 = tw.get_tbs(bduss)
                if err2 == "auth":
                    status = "auth"
                    break
                if new_tbs:
                    tbs = new_tbs
                try:
                    tbs, status = send_target(
                        bduss, dev,
                        {"tid": tid, "fid": fid, "kw": kw,
                         "content": tgt["content"], "count": tgt["count"]},
                        ps, state, tbs)
                    break
                except tw.WaterError as e2:
                    log("[%s] 重试%d/%d 仍失败: %s" % (tid, attempt, VCODE_MAX_RETRY, e2))
            if status not in ("ok", "auth"):
                log("[%s] 异常: 重试超过上限, 放弃该目标(%s)" % (tid, e))
        tw.save_json(STATE_FILE, state)
        if status == "auth":
            log("错误: 凭证失效, 中止")
            return EXIT_AUTH
        if status == "ok":
            ok_n += 1
        else:
            fail_n += 1
        if i < len(targets) - 1:
            time.sleep(random.uniform(*POST_DELAY))

    tw.save_json(STATE_FILE, state)
    log("-" * 50)
    summary = "自动发贴完成: 成功 %d 帖, 失败 %d 帖, 已完成跳过 %d 帖" % (
        ok_n, fail_n, skip_n)
    if off_n:
        summary += ", 已关闭跳过 %d 帖" % off_n
    log(summary)
    return EXIT_PARTIAL if fail_n else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())