#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tieba_sign_now.py - 立即触发全部关注吧的签到(本地工具, 依赖 paramiko)
用法: python scripts/tieba_sign_now.py
说明:
  直接在服务器执行 tieba_sign.py, 输出原样透传;
  签到会写入服务器 tieba-run.log, 与每日 08:05 定时任务共用(计入周报)。
退出码: 0=全部成功 1=部分失败 2=凭证失效 3=接口异常
"""

import sys

from add_water import load_deploy, ssh_connect


def main():
    if sys.version_info >= (3, 7):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    deploy = load_deploy()
    t, sock = ssh_connect(deploy)
    cmd = "cd %s && /opt/python/bin/python3 tieba_sign.py" % deploy["remote_dir"]
    chan = t.open_session(timeout=1800)
    chan.exec_command(cmd)
    for line in chan.makefile("rb"):
        print(line.decode("utf-8", "replace").rstrip())
    err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
    code = chan.recv_exit_status()
    t.close(); sock.close()
    if code == 2:
        print("\n凭证失效: 请重新获取 BDUSS 并更新服务器 cookies 文件", file=sys.stderr)
    elif code == 3:
        print("\n接口异常: %s" % err[:300], file=sys.stderr)
    elif code == 1:
        print("\n存在签到失败的吧, 详见上方日志", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())