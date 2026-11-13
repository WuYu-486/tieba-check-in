#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_mail.py - 通用日志邮件推送(QQ SMTP, 纯标准库, 无第三方依赖)
用法:
  python push_mail.py --log tieba-run.log --subject "贴吧签到日报"
      提取日志文件中「今天」的行作为正文发送(无当日内容时发送"今日无日志"提醒)
  python push_mail.py --subject "紧急告警" --alert "贴吧凭证已失效"
      直接发送 --alert 内容的即时告警邮件
凭证: 同目录 notify.ini (gitignore 排除, 严禁提交仓库)
"""

import argparse
import configparser
import os
import smtplib
import ssl
import sys
import time
from email.header import Header
from email.mime.text import MIMEText

BASE = os.path.dirname(os.path.abspath(__file__))
# 优先使用随程序部署的 CA 证书包(服务器系统证书过旧时避免 SSL 验证失败)
_cacert = os.path.join(BASE, "cacert.pem")
if os.path.exists(_cacert):
    os.environ.setdefault("SSL_CERT_FILE", _cacert)

CONFIG_FILE = os.path.join(BASE, "notify.ini")


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


def send(subject, body, cfg):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["user"]
    msg["To"] = cfg["to"]
    msg["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S +0800")
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
    parser.add_argument("--subject", required=True, help="邮件主题")
    parser.add_argument("--alert", help="直接发送的告警正文")
    args = parser.parse_args()

    cfg = load_config()
    try:
        if args.alert:
            body = "【即时告警】\n\n" + args.alert + "\n\n时间: " + \
                time.strftime("%Y-%m-%d %H:%M:%S")
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
            parser.error("需要 --log 或 --alert 之一")
        send(args.subject, body, cfg)
        print("[%s] 邮件已发送: %s -> %s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), args.subject, cfg["to"]))
        return 0
    except Exception as e:
        print("[%s] 邮件发送失败: %s" % (time.strftime("%H:%M:%S"), e),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
