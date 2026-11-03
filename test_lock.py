"""僵尸锁检测的单元验证：python test_lock.py

场景：守护进程在持有任务锁时被重启/被 kill -> 锁目录残留，
      下一个进程会白等 1 小时。锁里记了 pid，应据此判断持有者已死。
"""
import os
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import bot as B  # noqa: E402

results = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))
    results.append(cond)


lock_dir = B.LOCKS / "task-testlock.lock"
shutil.rmtree(lock_dir, ignore_errors=True)

try:
    # 1. 持锁者存活 -> 不算僵尸
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    lk = B.Lock("task-testlock")
    check("持锁进程存活 -> 不判僵尸", not lk._stale(), "误判为僵尸")

    # 2. 持锁者已死（用一个几乎不可能存在的 pid）
    dead_pid = 999999
    (lock_dir / "pid").write_text(str(dead_pid), encoding="utf-8")
    lk2 = B.Lock("task-testlock")
    check("持锁进程已死 -> 判为僵尸", lk2._stale(), "未识别僵尸锁")

    # 3. 超过 1 小时 -> 判为僵尸
    (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    old = time.time() - 7200
    os.utime(lock_dir, (old, old))
    lk3 = B.Lock("task-testlock")
    check("超过 1 小时 -> 判为僵尸", lk3._stale(), "")

    # 4. 锁里没有 pid 文件 -> 不判僵尸（保守，等超时）
    (lock_dir / "pid").unlink(missing_ok=True)
    os.utime(lock_dir, None)
    lk4 = B.Lock("task-testlock")
    check("无 pid 文件 -> 保守不判僵尸", not lk4._stale(), "")

    # 5. 真能拿到锁（僵尸锁被清理）
    shutil.rmtree(lock_dir, ignore_errors=True)
    with B.Lock("task-testlock", timeout=5) as l:
        got = l.acquired
    check("僵尸锁清理后可正常获取", got, "")
finally:
    shutil.rmtree(lock_dir, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
