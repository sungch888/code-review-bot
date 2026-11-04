"""审批 + 处理中确认信 的测试：python test_approval.py

安全：所有「发送」都被 monkeypatch 成记录器，**不会真的发邮件**。
覆盖：
  - 审批闸：require_approval 开 -> 审核完停在 AWAITING_APPROVAL；approve -> REPLIED；reject -> REJECTED
  - enqueue 对待审批/已驳回任务幂等跳过
  - ack_sweep：超时未出结果才发、每任务最多一封、已回复/驳回/忽略不发、失败用尽不发
  - 确认信文案按当前状态区分（排队 / 审核中 / 待审批 / 重试中）
"""
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import bot as B      # noqa: E402
import anomaly as A  # noqa: E402

results = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))
    results.append(cond)


# ---------- 用一个隔离目录，别污染真实 state/work ----------
TMP = Path(tempfile.mkdtemp(prefix="appr_test_"))
OLD_TASKS, OLD_WORK = B.TASKS, B.WORK
B.TASKS = TMP / "tasks"
B.WORK = TMP / "work"
B.TASKS.mkdir(parents=True)

# 记录器：替代真实发送
SENT = []
def fake_do_reply(mid, body, files):
    SENT.append({"mid": mid, "body": body, "files": list(files)})
    return {"queued": True}
def fake_send_reply(task, report_dir):
    SENT.append({"mid": task["message_id"], "body": "<report>", "files": ["report"]})
    return {"queued": True}

B.do_reply = fake_do_reply
B.send_reply = fake_send_reply

MID = "msg_appr_test"


def mk_task(state, created_ago=0, **kw):
    t = {
        "message_id": MID, "subject": "代码审核-审批测试",
        "from": "t@example.com", "from_name": "T",
        "created_at": B.iso(), "created_at_ts": B.now() - created_ago,
        "state": state, "attempts": 0, "last_error": None, "report": None,
        "replied_at": None, "next_retry_at": 0,
    }
    t.update(kw)
    B.save_task(t)
    return t


# 造一个"报告已生成"的目录，供 approve 发送用
rpt = B.WORK / MID / "report"
rpt.mkdir(parents=True, exist_ok=True)
(rpt / "代码审核报告.md").write_text("# 报告\n内容", encoding="utf-8")
(rpt / "summary.md").write_text("摘要", encoding="utf-8")

try:
    # ---------- 1. require_approval 助手 ----------
    B.CONFIG["require_approval"] = False
    check("默认关闭审批", B.require_approval() is False)
    B.CONFIG["require_approval"] = True
    check("开启审批", B.require_approval() is True)

    # ---------- 2. 审批通过 ----------
    SENT.clear()
    mk_task("AWAITING_APPROVAL", report=str(rpt))
    t = B.approve_task(MID, approver="tester")
    check("approve 后状态 REPLIED", t["state"] == "REPLIED", t["state"])
    check("approve 触发了发送", len(SENT) == 1, str(SENT))
    check("approve 记录了审批人", t.get("approved_by") == "tester", str(t.get("approved_by")))
    check("approve 记录了审批时间", bool(t.get("approved_at")))

    # 非待审批状态不能 approve
    mk_task("REPLIED")
    try:
        B.approve_task(MID)
        check("已 REPLIED 不能再 approve", False, "没报错")
    except Exception:
        check("已 REPLIED 不能再 approve", True)

    # ---------- 3. 驳回 ----------
    SENT.clear()
    mk_task("AWAITING_APPROVAL", report=str(rpt))
    t = B.reject_task(MID, reason="报告质量不达标")
    check("reject 后状态 REJECTED", t["state"] == "REJECTED", t["state"])
    check("reject 未触发发送", len(SENT) == 0, str(SENT))
    check("reject 记录了原因", t.get("reject_reason") == "报告质量不达标", str(t.get("reject_reason")))

    # 已 REJECTED 不能再 approve
    try:
        B.approve_task(MID)
        check("已 REJECTED 不能再 approve", False, "没报错")
    except Exception:
        check("已 REJECTED 不能再 approve", True)

    # ---------- 4. enqueue 幂等 ----------
    B.CONFIG["require_approval"] = False
    mk_task("AWAITING_APPROVAL")
    r = B.enqueue({"message_id": MID, "subject": "代码审核-审批测试", "from": {"email": "x@x"}})
    check("待审批任务 enqueue 跳过", r.startswith("DONE_SKIP"), r)
    mk_task("REJECTED")
    r = B.enqueue({"message_id": MID, "subject": "代码审核-审批测试", "from": {"email": "x@x"}})
    check("已驳回任务 enqueue 跳过", r.startswith("DONE_SKIP"), r)
    mk_task("REPLIED")
    r = B.enqueue({"message_id": MID, "subject": "代码审核-审批测试", "from": {"email": "x@x"}})
    check("已回复任务 enqueue 跳过", r.startswith("DONE_SKIP"), r)

    # ---------- 5. ack_sweep ----------
    B.CONFIG["ack_enabled"] = True
    B.CONFIG["ack_timeout_sec"] = 300

    # 5.1 新任务（< 5min）不发
    SENT.clear()
    mk_task("SEEN", created_ago=60)
    n = B.ack_sweep()
    check("新任务(<5min)不发确认信", n == 0 and len(SENT) == 0, f"n={n} sent={len(SENT)}")

    # 5.2 超时任务（> 5min）发一封
    SENT.clear()
    mk_task("SEEN", created_ago=600)
    n = B.ack_sweep()
    check("超时任务发确认信", n == 1 and len(SENT) == 1, f"n={n} sent={len(SENT)}")
    t = B.load_task(MID)
    check("记录 ack_sent_at", bool(t.get("ack_sent_at")), str(t.get("ack_sent_at")))
    check("确认信文案含「已收到」", "已收到" in SENT[0]["body"], SENT[0]["body"][:80])

    # 5.3 已经发过不再重复发
    SENT.clear()
    n = B.ack_sweep()
    check("已发过确认信不重复发", n == 0 and len(SENT) == 0, f"n={n}")

    # 5.4 已 REPLIED 不发
    SENT.clear()
    mk_task("REPLIED", created_ago=9999)
    n = B.ack_sweep()
    check("已 REPLIED 不发确认信", n == 0 and len(SENT) == 0, f"n={n}")

    # 5.5 已 REJECTED 不发
    SENT.clear()
    mk_task("REJECTED", created_ago=9999)
    n = B.ack_sweep()
    check("已 REJECTED 不发确认信", n == 0 and len(SENT) == 0, f"n={n}")

    # 5.6 失败且重试用尽不发（走失败通知）
    SENT.clear()
    B.CONFIG["max_retries"] = 3
    mk_task("FAILED", created_ago=9999, attempts=3)
    n = B.ack_sweep()
    check("失败用尽不发「处理中」确认信", n == 0 and len(SENT) == 0, f"n={n}")

    # 5.7 失败但还有重试额度 -> 发
    SENT.clear()
    mk_task("FAILED", created_ago=9999, attempts=1)
    n = B.ack_sweep()
    check("失败仍在重试 -> 发确认信并说明重试", n == 1 and "重试" in SENT[0]["body"],
          f"n={n} body={SENT[0]['body'][:80] if SENT else ''}")

    # 5.8 ack_enabled 关闭 -> 全不发
    SENT.clear()
    B.CONFIG["ack_enabled"] = False
    mk_task("SEEN", created_ago=9999)
    n = B.ack_sweep()
    check("ack_enabled 关闭时不发", n == 0 and len(SENT) == 0, f"n={n}")
    B.CONFIG["ack_enabled"] = True

    # ---------- 6. 状态文案区分 ----------
    check("排队文案", "排队" in A.describe_state("SEEN"), A.describe_state("SEEN"))
    check("审核中文案", "审核" in A.describe_state("DOWNLOADED"), A.describe_state("DOWNLOADED"))
    check("待审批文案", "审批" in A.describe_state("AWAITING_APPROVAL"), A.describe_state("AWAITING_APPROVAL"))
    check("重试文案", "重试" in A.describe_state("FAILED", 2), A.describe_state("FAILED", 2))
    body = A.reply_ack("代码审核-X", "排队中", "约 10 分钟")
    check("确认信有预计时间", "约 10 分钟" in body, body[:120])
    check("确认信让发件人别重复发", "无需重复发送" in body, body[:160])

    # ---------- 7. process_task 的分支：审批闸在自动回复之前 ----------
    src = (BASE / "bot.py").read_text(encoding="utf-8")
    i = src.index('def process_task')
    j = src.index('\n\n\ndef ', i + 5)          # 到下一个顶层函数
    seg = src[i:j]
    check("process_task 里审批闸在自动回复之前",
          "AWAITING_APPROVAL" in seg and seg.index("AWAITING_APPROVAL") < seg.index("send_reply"),
          "见 process_task 源码")
    check("审批闸受 require_approval() 控制",
          "if require_approval():" in seg, "")

finally:
    B.TASKS, B.WORK = OLD_TASKS, OLD_WORK
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
