"""重试/退避/上游繁忙的回归测试：python test_retry.py

锁住两个真实踩过的坑：
  1. enqueue() 的 REQUEUE 分支曾把 next_retry_at 清零 -> 指数退避形同虚设，
     上游 503 时变成持续轰炸。
  2. 上游模型繁忙（503 service_unavailable）曾被当成「代码问题」，
     错误信息空洞且退避太短。
"""
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


BUSY = ('503: {"message":"Service is too busy. We advise users to temporarily switch to '
        'alternative LLM API service providers. (request_id: baa1ead4)","type":'
        '"service_unavailable_error","code":"service_unavailable_error"}')

mid = "msg_RETRYtest01"
msg = {"message_id": mid, "subject": "代码审核 - 退避测试", "from": {"email": "a@b.c", "name": "t"}}

try:
    # --- 1. 上游繁忙识别 ---
    check("识别 503 service_unavailable", B.is_provider_busy(BUSY))
    check("识别 'Service is too busy'", B.is_provider_busy("Service is too busy."))
    check("识别 overloaded", B.is_provider_busy("model is overloaded"))
    check("普通错误不算繁忙", not B.is_provider_busy("未生成审核报告 (pi rc=1)"))
    check("空 stderr 不算繁忙", not B.is_provider_busy(""))

    # --- 2. 退避不被 REQUEUE 绕过 ---
    t = B.load_task(mid) or {}
    t.update({"message_id": mid, "subject": msg["subject"], "from": "a@b.c",
              "from_name": "t", "created_at": "2026-01-01T00:00:00Z",
              "created_at_ts": time.time(), "state": "FAILED", "attempts": 1,
              "last_error": "上游模型繁忙", "next_retry_at": B.now() + 300,
              "report": None, "replied_at": None})
    B.save_task(t)

    r = B.enqueue(msg)
    check("退避中的 FAILED 任务被跳过而非重新入队", r == "BACKOFF_SKIP", r)
    after = B.load_task(mid)
    check("退避时间未被清零", after.get("next_retry_at", 0) > B.now(),
          f"next_retry_at={after.get('next_retry_at')}")
    check("状态仍为 FAILED", after.get("state") == "FAILED", after.get("state"))
    check("attempts 未增加", after.get("attempts") == 1, after.get("attempts"))

    # --- 3. 退避到期后允许重试 ---
    t2 = B.load_task(mid)
    t2["next_retry_at"] = B.now() - 1
    B.save_task(t2)
    r2 = B.enqueue(msg)
    check("退避到期后重新入队", r2 == "REQUEUE", r2)
    after2 = B.load_task(mid)
    check("重新入队后状态为 SEEN", after2.get("state") == "SEEN", after2.get("state"))

    # --- 4. 超过最大重试次数则不再入队 ---
    t3 = B.load_task(mid)
    t3["state"] = "FAILED"
    t3["attempts"] = B.CONFIG["max_retries"]
    t3["next_retry_at"] = B.now() - 1
    B.save_task(t3)
    r3 = B.enqueue(msg)
    check("超过 max_retries 不再入队", r3 == "FAILED_SKIP", r3)

    # --- 5. REPLIED 终态仍直接跳过 ---
    t4 = B.load_task(mid)
    t4["state"] = "REPLIED"
    t4["attempts"] = 0
    t4["next_retry_at"] = 0
    B.save_task(t4)
    check("REPLIED 终态直接跳过", B.enqueue(msg) == "DONE_SKIP", B.enqueue(msg))

    # --- 6. worker 选任务时不会提前拿退避中的任务 ---
    t5 = B.load_task(mid)
    t5["state"] = "FAILED"
    t5["attempts"] = 1
    t5["next_retry_at"] = B.now() + 600
    B.save_task(t5)
    picked = None
    for tt in B.all_tasks():
        if tt.get("state") in ("SEEN", "DOWNLOADED", "REVIEWED"):
            picked = tt
            break
        if tt.get("state") == "FAILED" and tt.get("attempts", 0) < B.CONFIG["max_retries"]:
            if tt.get("next_retry_at", 0) and tt["next_retry_at"] > B.now():
                continue
            picked = tt
            break
    check("worker 跳过退避中的 FAILED 任务", picked is None or picked.get("message_id") != mid,
          str(picked)[:120])
finally:
    B.TASKS.joinpath(f"{mid}.json").unlink(missing_ok=True)

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
