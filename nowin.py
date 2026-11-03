#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无窗口启动器：运行任何命令但不弹出黑色控制台窗口。

用法:
    python nowin.py npm run dev
    python nowin.py "python -m uvicorn main:app --port 8800"
    python nowin.py --wait npm run dev      # 等待子进程结束并透传退出码

原理:
    Windows 控制台程序（node.exe / python.exe / cmd.exe / powershell.exe …）
    在没有控制台时启动会自动分配一个新控制台 -> 桌面弹出黑窗。
    创建时加 CREATE_NO_WINDOW 即可让它在后台无窗口运行。

    同时 STARTUPINFO.SW_HIDE 做双保险，并默认吞掉输出（-q 可改为透传）。
"""
import argparse
import os
import shlex
import subprocess
import sys


def _flags():
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _startupinfo():
    if os.name != "nt":
        return None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
        si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        return si
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="无窗口启动器（不弹黑窗）")
    ap.add_argument("--wait", action="store_true", help="等待进程结束并透传输出/退出码")
    ap.add_argument("-o", "--output", default="", help="非 --wait 时的日志文件路径")
    ap.add_argument("-e", "--env", action="append", default=[], help="追加环境变量 K=V")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令")
    a = ap.parse_args()

    cmd = a.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("缺少要执行的命令")

    env = dict(os.environ)
    for kv in a.env:
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v

    # 注意：CREATE_NO_WINDOW 下子进程继承的可能是无效的控制台句柄，
    # 因此必须显式接管 stdout/stderr，否则输出会静默丢失。
    if a.wait:
        try:
            p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, bufsize=1,
                                 text=True, encoding="utf-8", errors="replace",
                                 creationflags=_flags(), startupinfo=_startupinfo())
        except FileNotFoundError:
            print(f"找不到命令: {cmd[0]}", file=sys.stderr)
            return 127
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        return p.wait()

    logpath = a.output or f"nowin-{os.getpid()}.log"
    lf = open(logpath, "w", encoding="utf-8", errors="replace")
    try:
        p = subprocess.Popen(cmd, env=env, stdout=lf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             creationflags=_flags(), startupinfo=_startupinfo())
    except FileNotFoundError:
        lf.close()
        print(f"找不到命令: {cmd[0]}", file=sys.stderr)
        return 127
    print(f"pid={p.pid} log={os.path.abspath(logpath)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
