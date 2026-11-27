#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_mail.py - 日志邮件推送与周报(QQ SMTP, 纯标准库, 无第三方依赖)
用法:
  python push_mail.py --weekly tieba-run.log [--water-log tieba-water.log]
                      [--autopost-log tieba-autopost.log] --subject "贴吧签到周报"
      周报: 正文=一、本周运行总结(签到/水贴/自动发贴)+二、各吧本周经验(三类经验融合);
      三、签到明细 + 四、水贴明细 + 五、自动发贴明细 封装为 .txt 附件;
      发送成功后全部日志轮转
  python push_mail.py --log tieba-run.log --subject "贴吧签到日报"
      提取日志文件中「今天」的行作为正文发送(无当日内容时发送"今日无日志"提醒)
  python push_mail.py --subject "紧急告警" --alert "贴吧凭证已失效"
      直接发送 --alert 内容的即时告警邮件
  附加: --dry-run 只打印正文/附件内容不发送(用于测试预览)
凭证: 同目录 notify.ini (gitignore 排除, 严禁提交仓库)
周报日志轮转: 发送成功后 log -> log.prev(覆盖更旧归档), 每周日志独立;
  发送失败则不轮转, 未送达数据自动累积到下一期
水贴吧名映射: 同目录 water_threads.json (tieba_water.py 维护的 tid->吧名 缓存)
自动发贴吧名映射: 同目录 autopost_targets.json (autopost_manage.py 维护)
"""

import argparse
import configparser
import json
import os
import re
import smtplib
import ssl
import sys
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE = os.path.dirname(os.path.abspath(__file__))
# 本地仓库中脚本统一放在 scripts/, 配置与数据在仓库根目录; 服务器部署时全部平铺同目录
if os.path.basename(BASE) == "scripts":
    BASE = os.path.dirname(BASE)
# 优先使用随程序部署的 CA 证书包(服务器系统证书过旧时避免 SSL 验证失败)
_cacert = os.path.join(BASE, "cacert.pem")
if os.path.exists(_cacert):
    os.environ.setdefault("SSL_CERT_FILE", _cacert)

CONFIG_FILE = os.path.join(BASE, "notify.ini")
THREAD_MAP_FILE = os.path.join(BASE, "water_threads.json")
AUTOPOST_TARGETS_FILE = os.path.join(BASE, "autopost_targets.json")

# 签到明细行: [时间] (i/n) [吧名] 签到成功(+N经验) / 签到成功 / 跳过(未加入) / 失败: xxx
RE_SIGN_DETAIL = re.compile(
    r"^\[([\d\- :]+)\] \((\d+/\d+)\) \[(.+?)\] "
    r"(签到成功(?:\(\+(\d+)经验\))?|跳过\(未加入\)|失败.*|tbs 失效.*)$")
# 签到每日汇总行: [时间] 签到完成: 成功 N, 已签 N, 跳过 N, 失败 N
RE_SIGN_SUMMARY = re.compile(
    r"^\[([\d\- :]+)\] 签到完成: 成功 (\d+), 已签 (\d+), 跳过 (\d+), 失败 (\d+)$")
# 水贴明细行: [时间] [tid] 第i/n条 成功(pid=xxx exp+N) 内容='xxx'
RE_WATER_DETAIL = re.compile(
    r"^\[([\d\- :]+)\] \[(\d+)\] 第\d+/\d+条 成功\((?:pid=\d+ )?exp\+(\d+)\) 内容='(.*)'$")
# 水贴每日汇总行: [时间] 水贴完成: 成功 N 帖, 失败 N 帖, 已完成跳过 N 帖(, 已关闭跳过 N 帖)
RE_WATER_SUMMARY = re.compile(
    r"^\[([\d\- :]+)\] 水贴完成: 成功 (\d+) 帖, 失败 (\d+) 帖, 已完成跳过 (\d+) 帖"
    r"(?:, 已关闭跳过 (\d+) 帖)?$")
# 自动发贴每日汇总行: [时间] 自动发贴完成: 成功 N 帖, 失败 N 帖, 已完成跳过 N 帖(, 已关闭跳过 N 帖)
RE_AP_SUMMARY = re.compile(
    r"^\[([\d\- :]+)\] 自动发贴完成: 成功 (\d+) 帖, 失败 (\d+) 帖, 已完成跳过 (\d+) 帖"
    r"(?:, 已关闭跳过 (\d+) 帖)?$")
# 自动发贴明细行: 与水贴明细格式一致
RE_AP_DETAIL = RE_WATER_DETAIL


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print("错误: 未找到 notify.ini 配置文件", file=sys.stderr)
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")
    return {
        "host": cfg.get("smtp", "host", fallback="smtp.qq.com"),
        "port": cfg.getint("smtp", "port", fallback=465),
        "user": cfg.get("smtp", "user"),
        "password": cfg.get("smtp", "password"),
        "to": cfg.get("smtp", "to"),
    }


def load_thread_map():
    """tid -> 吧名 映射(tieba_water.py 的缓存文件)"""
    try:
        with open(THREAD_MAP_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {tid: info.get("kw") or tid for tid, info in raw.items()}
    except (OSError, ValueError):
        return {}


def today_lines(path):
    """提取日志中属于今天的行, 兼容 YYYY-MM-DD 与 YYYY/MM/DD 两种日期格式"""
    if not os.path.exists(path):
        return None
    today = time.strftime("%Y-%m-%d")
    today_alt = time.strftime("%Y/%m/%d")
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if today in line or today_alt in line:
                lines.append(line.rstrip())
    return lines


def parse_sign(path):
    """解析签到日志 -> (汇总行, {吧名: {days, total}}, 明细行, 日期集合)"""
    summaries, details, exp_map, dates = [], [], {}, set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip()
                m = RE_SIGN_SUMMARY.match(line)
                if m:
                    summaries.append(line)
                    dates.add(m.group(1)[:10])
                    continue
                m = RE_SIGN_DETAIL.match(line)
                if m:
                    details.append(line)
                    dates.add(m.group(1)[:10])
                    d = exp_map.setdefault(m.group(3), {"days": 0, "total": 0})
                    d["days"] += 1
                    if m.group(5):
                        d["total"] += int(m.group(5))
    return summaries, exp_map, details, dates


def load_autopost_map():
    """tid -> 吧名 映射(autopost_targets.json)"""
    try:
        with open(AUTOPOST_TARGETS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {str(t.get("tid")): t.get("kw") or str(t.get("tid"))
                for t in raw if isinstance(t, dict)}
    except (OSError, ValueError):
        return {}


def parse_daily_log(path, thread_map, re_summary, re_detail):
    """解析按帖发贴日志(水贴/自动发贴通用) -> (汇总行, {吧名: {count, total}}, 明细行, 日期集合)"""
    summaries, details, exp_map, dates = [], [], {}, set()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip()
                m = re_summary.match(line)
                if m:
                    summaries.append(line)
                    dates.add(m.group(1)[:10])
                    continue
                m = re_detail.match(line)
                if m:
                    details.append(line)
                    dates.add(m.group(1)[:10])
                    kw = thread_map.get(m.group(2), m.group(2))
                    d = exp_map.setdefault(kw, {"count": 0, "total": 0})
                    d["count"] += 1
                    d["total"] += int(m.group(3))
    return summaries, exp_map, details, dates


def parse_water(path, thread_map):
    return parse_daily_log(path, thread_map, RE_WATER_SUMMARY, RE_WATER_DETAIL)


def parse_autopost(path, thread_map):
    return parse_daily_log(path, thread_map, RE_AP_SUMMARY, RE_AP_DETAIL)


def fmt_fused_line(kw, sign_d, water_d, ap_d=None):
    """融合签到/水贴/自动发贴经验的单吧统计行, 如 [clannad] 签到 48 + 水贴 24 = 72"""
    parts = []
    if sign_d and sign_d["total"]:
        parts.append("签到 %d" % sign_d["total"])
    elif sign_d and sign_d["days"]:
        parts.append("签到成功*%d(旧日志无经验记录)" % sign_d["days"])
    if water_d and water_d["total"]:
        parts.append("水贴 %d" % water_d["total"])
    if ap_d and ap_d["total"]:
        parts.append("自动发贴 %d" % ap_d["total"])
    total = sum(d["total"] for d in (sign_d, water_d, ap_d) if d)
    return "[%s] %s = %d" % (kw, " + ".join(parts), total)


def build_weekly(sign_log, water_log, autopost_log=None):
    """生成周报; 返回 (正文, 附件文本, 日期范围, 是否有数据)"""
    thread_map = load_thread_map()
    s_sum, s_exp, s_det, s_dates = parse_sign(sign_log)
    w_sum, w_exp, w_det, w_dates = parse_water(water_log, thread_map)
    a_sum, a_exp, a_det, a_dates = parse_autopost(autopost_log,
                                                  load_autopost_map())
    dates = s_dates | w_dates | a_dates
    rng = ""
    if dates:
        ds = sorted(dates)
        rng = ds[0] if len(ds) == 1 else "%s ~ %s" % (ds[0], ds[-1])

    sep = "-" * 40
    out = []
    # 一、本周运行总结: 签到 -> 水贴 -> 自动发贴
    out.append("一、本周运行总结")
    out.append(sep)
    out.extend(s_sum)
    out.extend(w_sum)
    out.extend(a_sum)
    out.append("")
    ok_n = fail_n = 0
    for line in s_sum:
        m = RE_SIGN_SUMMARY.match(line)
        if m:
            ok_n += int(m.group(2))
            fail_n += int(m.group(5))
    out.append("本周合计: 签到成功 %d 次, 失败 %d 次" % (ok_n, fail_n))

    def total_line(label, sums, re_summary):
        if not sums:
            return "%s合计: 本周无%s记录" % (label, label)
        t_ok = t_fail = t_skip = t_closed = 0
        for line in sums:
            m = re_summary.match(line)
            if m:
                t_ok += int(m.group(2))
                t_fail += int(m.group(3))
                t_skip += int(m.group(4))
                if m.group(5):
                    t_closed += int(m.group(5))
        line = "%s合计: 成功 %d 帖次, 失败 %d 帖次, 已完成跳过 %d 帖次" % (
            label, t_ok, t_fail, t_skip)
        if t_closed:
            line += ", 已关闭 %d 帖次" % t_closed
        return line

    out.append(total_line("水贴", w_sum, RE_WATER_SUMMARY))
    out.append(total_line("自动发贴", a_sum, RE_AP_SUMMARY))

    # 二、各吧本周经验: 签到+水贴+自动发贴融合, 按总经验降序
    out.append("")
    out.append("")
    out.append("二、各吧本周经验")
    out.append(sep)
    kws = set(s_exp) | set(w_exp) | set(a_exp)
    if kws:
        def total_of(kw):
            return (s_exp.get(kw, {}).get("total", 0)
                    + w_exp.get(kw, {}).get("total", 0)
                    + a_exp.get(kw, {}).get("total", 0))
        for kw in sorted(kws, key=lambda k: (-total_of(k), k)):
            out.append(fmt_fused_line(kw, s_exp.get(kw), w_exp.get(kw),
                                      a_exp.get(kw)))
        out.append("")
        detail = "签到 %d + 水贴 %d" % (
            sum(d["total"] for d in s_exp.values()),
            sum(d["total"] for d in w_exp.values()))
        if a_exp:
            detail += " + 自动发贴 %d" % sum(d["total"] for d in a_exp.values())
        out.append("本周经验总计: %d (%s)" % (
            sum(total_of(k) for k in kws), detail))
    else:
        out.append("(无经验数据)")

    if not s_sum and not s_det and not w_sum and not w_det \
            and not a_sum and not a_det:
        return ("周报周期内无任何签到/水贴/自动发贴日志!\n"
                "可能定时任务未运行或早已失败, 请检查!"), "", rng, False

    body = "\n".join(out)

    # 附件: 三、签到明细 + 四、水贴明细 + 五、自动发贴明细
    att = ["三、签到明细(按时间顺序)", sep]
    att.extend(s_det if s_det else ["(无签到明细)"])
    att.append("")
    att.append("")
    att.append("四、水贴明细(按时间顺序)")
    att.append(sep)
    att.extend(w_det if w_det else ["(无水贴明细)"])
    att.append("")
    att.append("")
    att.append("五、自动发贴明细(按时间顺序)")
    att.append(sep)
    att.extend(a_det if a_det else ["(无自动发贴明细)"])
    return body, "\n".join(att), rng, True


def rotate_log(path):
    """日志轮转: log -> log.prev (覆盖更旧归档)"""
    if not path or not os.path.exists(path):
        return
    prev = path + ".prev"
    try:
        if os.path.exists(prev):
            os.remove(prev)
        os.rename(path, prev)
    except OSError:
        pass


def send(subject, body, cfg, attachment=None):
    """发送邮件; attachment 为 (文件名, 文本) 时作为 .txt 附件"""
    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["user"]
    msg["To"] = cfg["to"]
    msg["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S +0800")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if attachment:
        fname, text = attachment
        part = MIMEText(text, "plain", "utf-8")
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", fname))
        msg.attach(part)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30,
                          context=ctx) as server:
        server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["user"], [cfg["to"]], msg.as_string())


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="日志邮件推送")
    parser.add_argument("--log", help="日志文件路径(取当日内容)")
    parser.add_argument("--weekly", help="签到日志路径(汇总全部内容生成周报)")
    parser.add_argument("--water-log", help="水贴日志路径(周报融合用)")
    parser.add_argument("--autopost-log", help="自动发贴日志路径(周报融合用)")
    parser.add_argument("--subject", required=True, help="邮件主题")
    parser.add_argument("--alert", help="直接发送的告警正文")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印正文/附件不发送(测试)")
    args = parser.parse_args()

    attachment = None
    rotate_paths = []
    try:
        if args.alert:
            body = "【即时告警】\n\n" + args.alert + "\n\n时间: " + \
                time.strftime("%Y-%m-%d %H:%M:%S")
        elif args.weekly:
            body, att_text, rng, _ = build_weekly(args.weekly, args.water_log,
                                                  args.autopost_log)
            body = "【贴吧签到周报】%s\n\n%s" % (rng, body)
            if att_text:
                attachment = ("贴吧周报明细_%s.txt" % time.strftime("%Y-%m-%d"),
                              att_text)
            rotate_paths = [args.weekly, args.water_log, args.autopost_log]
        elif args.log:
            lines = today_lines(args.log)
            if lines is None:
                body = "日志文件不存在: %s\n可能任务未运行, 请检查!" % args.log
            elif not lines:
                body = "今日(%s)无日志记录!\n可能定时任务未运行或早已失败, 请检查!" \
                    % time.strftime("%Y-%m-%d")
            else:
                body = "【今日运行日志】\n\n" + "\n".join(lines)
        else:
            parser.error("需要 --log / --weekly / --alert 之一")

        if args.dry_run:
            print("=" * 50)
            print("Subject:", args.subject)
            print("=" * 50)
            print(body)
            if attachment:
                print()
                print("=" * 50)
                print("附件:", attachment[0])
                print("=" * 50)
                print(attachment[1])
            return 0

        cfg = load_config()
        send(args.subject, body, cfg, attachment)
        print("[%s] 邮件已发送: %s -> %s%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), args.subject, cfg["to"],
            " (含附件 %s)" % attachment[0] if attachment else ""))
        for path in rotate_paths:
            rotate_log(path)
            if os.path.exists(path + ".prev"):
                print("[%s] 日志已轮转: %s -> %s.prev" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), path, path))
        return 0
    except Exception as e:
        print("[%s] 邮件发送失败: %s" % (time.strftime("%H:%M:%S"), e),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())