"""验证 CREATE_NO_WINDOW 是否真的阻止了黑窗闪烁（精确判定）。

做法：让子进程自己回答「我有没有控制台窗口」
      GetConsoleWindow() == 0  -> 没有控制台 -> 不会弹黑窗
      GetConsoleWindow() != 0  -> 有控制台 -> 会弹黑窗

关键：父进程必须**本身没有控制台**（用 DETACHED_PROCESS 启动），
      否则子进程会继承父的控制台，两种情况都非 0，实验无意义。

用法: python test_no_window.py [输出文件]
"""
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROBE = (
    "import ctypes,sys;"
    "sys.stdout.write(str(ctypes.windll.kernel32.GetConsoleWindow()))"
)

_lines = []


def emit(s):
    print(s, flush=True)
    _lines.append(s)


def probe(flags):
    """返回 (是否有控制台窗口, 句柄)"""
    p = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, creationflags=flags)
    out = (p.stdout or "").strip()
    try:
        h = int(out)
    except ValueError:
        h = -1
    return h


def main():
    logf = Path(sys.argv[1]) if len(sys.argv) > 1 else None

    if os.name != "nt":
        emit("非 Windows 平台，跳过")
        return 0

    # 父进程自身是否已有控制台
    self_h = probe(0)
    emit(f"父进程自身控制台句柄: {self_h} "
         f"({'有控制台，实验需改用脱离控制台的父进程' if self_h else '无控制台，实验有效'})")

    noflag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    emit("")
    emit("实验 1：不加标志（模拟修复前的旧行为）")
    h1 = probe(0)
    emit(f"  子进程控制台句柄 = {h1}  ->  {'有控制台，会弹黑窗' if h1 else '无控制台'}")
    emit("")
    emit("实验 2：加 CREATE_NO_WINDOW（修复后的行为）")
    h2 = probe(noflag)
    emit(f"  子进程控制台句柄 = {h2}  ->  {'有控制台，会弹黑窗' if h2 else '无控制台，不弹窗'}")

    ok = (h2 == 0)
    emit("")
    emit(f"结论: 加 CREATE_NO_WINDOW 后{'仍然' if not ok else '不再'}分配控制台窗口")
    if logf:
        logf.write_text("\n".join(_lines), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
