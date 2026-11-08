#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发（任务级 + 分块级）与限流冷却的验证

背景：
  `max_workers` 以前只是配置里一个数字（status 会显示），实际 worker 永远只有一个线程；
  大代码包的 12 个分块也只能一块一块审，一个包要十几分钟。
  直接改成多线程有两个坑：
    1) 两个 worker 会抢到同一个任务 -> 同一个包被审两遍、发两封邮件
    2) 并发打模型必然撞 429，各自盲目重试会把上游打得更死

这里验证：
  A. 抢占：同一任务只能被一个 worker 拿到；空闲/退避/终态任务不会被抢
  B. 并行执行器：worker 数生效、结果按序返回、异常能抛出去
  C. 限速闸：并发不超上限、上游繁忙时全局冷却、配置改了即时生效
  D. 分块并行：真跑一次 _review_via_http，观察模型调用确实重叠且不超上限
  E. worker 池：max_workers=2/3 真的起了对应数量的 worker 线程
  F. 失败退避：指数退避 + 上游繁忙加长 + 重试用尽回信
"""
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import bot as B  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {extra}")


TMP = Path(tempfile.mkdtemp(prefix="conc_"))
B.TASKS = TMP / "tasks"
B.WORK = TMP / "work"
B.TASKS.mkdir(parents=True, exist_ok=True)
B.WORK.mkdir(parents=True, exist_ok=True)


def mk(mid, state="SEEN", ts=100, **kw):
    t = {"message_id": mid, "subject": "代码审核 - " + mid, "state": state,
         "created_at_ts": ts, "attempts": 0, "next_retry_at": 0}
    t.update(kw)
    B.save_task(t)
    return t


def reset_claims():
    for mid in list(B.claimed_tasks()):
        B.release_task(mid)


class FakeProvider:
    """假模型：记录并发峰值，可设定前 N 次抛「上游繁忙」"""

    def __init__(self, cfg=None, delay=0.15, busy_times=0):
        self.configured = True
        self.model = "fake-model"
        self.has_key = True
        self.protocol = "openai"
        self.delay = delay
        self.busy_left = busy_times
        self.lock = threading.Lock()
        self.cur = 0
        self.peak = 0
        self.calls = 0

    def describe(self):
        return "fake://model"

    def chat(self, messages, model="", max_tokens=0):
        with self.lock:
            self.calls += 1
            busy = self.busy_left > 0
            if busy:
                self.busy_left -= 1
            self.cur += 1
            self.peak = max(self.peak, self.cur)
        try:
            if busy:
                raise B.LLM.ProviderBusy("触发限流（429）", status=429)
            time.sleep(self.delay)
            return types.SimpleNamespace(
                text=("## 问题清单\n\n| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 |\n"
                      "| --- | --- | --- | --- | --- | --- | --- |\n"
                      "| 1 | 高 | a.py:1 | 示例问题 | 成因 | 后果 | 建议 |\n"),
                elapsed=self.delay, finish_reason="stop", truncated=False)
        finally:
            with self.lock:
                self.cur -= 1


def main():
    old_cfg = {k: B.CONFIG.get(k) for k in
               ("max_workers", "llm_max_concurrency", "chunk_workers",
                "llm_rate_cooldown_sec", "review_chunk_files", "max_chars_per_chunk",
                "max_file_chars", "retry_backoff_sec", "provider_busy_backoff_sec",
                "max_retries")}
    try:
        # ---------- A. 抢占 ----------
        reset_claims()
        ok = []
        ths = [threading.Thread(target=lambda: ok.append(B.claim_task("msg_same")))
               for _ in range(5)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        check("同一任务只能被抢到一次", sum(1 for x in ok if x) == 1, str(ok))
        check("抢到的任务在 claimed 里", "msg_same" in B.claimed_tasks())
        B.release_task("msg_same")
        check("释放后可以再抢", B.claim_task("msg_same"))
        B.release_task("msg_same")

        # ---------- A2. 选任务 ----------
        reset_claims()
        for f in B.TASKS.glob("*.json"):
            f.unlink()
        mk("msg_old", ts=100)
        mk("msg_new", ts=300)
        mk("msg_done", state="REPLIED", ts=50)
        mk("msg_ignored", state="IGNORED", ts=40)
        mk("msg_backoff", state="FAILED", ts=20,
           attempts=1, next_retry_at=time.time() + 999)
        mk("msg_dead", state="FAILED", ts=10, attempts=3)
        got = B.next_task()
        check("取最老的待处理任务", got and got["message_id"] == "msg_old",
              str(got and got["message_id"]))
        B.claim_task("msg_old")
        got = B.next_task()
        check("已被抢的任务不会被再取到", got and got["message_id"] == "msg_new",
              str(got and got["message_id"]))
        B.claim_task("msg_new")
        check("只剩终态/退避/超次数时为 None", B.next_task() is None)
        reset_claims()

        # ---------- B. 并行执行器 ----------
        seen = []

        def job(i, x):
            seen.append(x)
            time.sleep(0.12)
            return x * 2

        t0 = time.time()
        out = B.run_parallel([1, 2, 3, 4], job, workers=4)
        dt = time.time() - t0
        check("结果按传入顺序返回", out == [2, 4, 6, 8], str(out))
        check("4 路并发比串行快", dt < 0.35, f"{dt:.2f}s")
        check("所有子任务都执行了", sorted(seen) == [1, 2, 3, 4], str(seen))

        t0 = time.time()
        B.run_parallel([1, 2, 3, 4], job, workers=1)
        check("workers=1 时串行（旧行为）", time.time() - t0 >= 0.45,
              f"{time.time() - t0:.2f}s")
        check("空列表直接返回", B.run_parallel([], job, workers=4) == [])

        def boom(i, x):
            if x == 2:
                raise RuntimeError("chunk 挂了")
            time.sleep(0.05)
            return x

        raised = False
        try:
            B.run_parallel([1, 2, 3], boom, workers=3)
        except RuntimeError as e:
            raised = "chunk 挂了" in str(e)
        check("子任务失败会抛出去（不静默）", raised)

        # ---------- C. 限速闸 ----------
        B.CONFIG["llm_max_concurrency"] = 2
        B._GATE = None
        gate = B.rate_gate()
        check("闸的并发上限来自配置", gate.limit == 2, str(gate.limit))
        peak = [0]
        cur = [0]
        lock = threading.Lock()

        def hog():
            gate.wait_turn()
            try:
                with lock:
                    cur[0] += 1
                    peak[0] = max(peak[0], cur[0])
                time.sleep(0.1)
            finally:
                with lock:
                    cur[0] -= 1
                gate.release()

        hs = [threading.Thread(target=hog) for _ in range(6)]
        for h in hs:
            h.start()
        for h in hs:
            h.join()
        check("同时进行的调用不超过上限", peak[0] <= 2, f"peak={peak[0]}")

        B.CONFIG["llm_max_concurrency"] = 3
        check("配置改后闸即时生效", B.rate_gate().limit == 3)
        B.CONFIG["llm_max_concurrency"] = 2
        B.rate_gate()

        t0 = time.time()
        gate.cool_down(0.3, "测试冷却")
        check("冷却中 busy_for 有值", gate.busy_for() > 0)
        check("冷却计数增加", gate.cool_count >= 1)
        gate.wait_turn()
        check("冷却结束后能拿到名额（等过冷却期）", time.time() - t0 >= 0.25,
              f"{time.time() - t0:.2f}s")
        gate.release()
        check("冷却会取更长的那个", (gate.cool_down(0.01), gate.busy_for() > 0)[1])

        # ---------- D. 分块并行（真跑一遍） ----------
        src = TMP / "src"
        (src / "pkg").mkdir(parents=True, exist_ok=True)
        for i in range(4):
            (src / "pkg" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n",
                                                 encoding="utf-8")
        files = sorted(src.rglob("*.py"))
        rpt = TMP / "report"
        B.CONFIG.update({"review_chunk_files": 1, "max_chars_per_chunk": 100000,
                         "max_file_chars": 10000, "chunk_workers": 4,
                         "llm_max_concurrency": 2, "llm_rate_cooldown_sec": 0.05})
        fake = FakeProvider(delay=0.15)
        old_provider = B.LLM.Provider
        B.LLM.Provider = lambda cfg=None: fake
        try:
            md, _fix = B._review_via_http("msg_conc", src, rpt, files, "", [])
        finally:
            B.LLM.Provider = old_provider
        check("分块审核产出最终报告", md is not None and md.exists(),
              str(md))
        check("分块确实并行（峰值 >= 2）", fake.peak >= 2, f"peak={fake.peak}")
        check("并行不超模型并发上限", fake.peak <= 2, f"peak={fake.peak}")
        check("4 块 + 1 次汇总 = 5 次调用", fake.calls == 5, f"calls={fake.calls}")

        # 429：冷却后重试一次应成功
        B.CONFIG["llm_max_concurrency"] = 1
        B._GATE = None
        rpt2 = TMP / "report2"
        fake2 = FakeProvider(delay=0.01, busy_times=1)
        B.LLM.Provider = lambda cfg=None: fake2
        try:
            md2, _ = B._review_via_http("msg_429", src, rpt2, files[:2], "", [])
            ok429 = md2 is not None and md2.exists()
        except Exception as e:
            ok429 = False
            print("  429 重试异常:", e)
        finally:
            B.LLM.Provider = old_provider
        check("429 冷却后自动重试成功（不直接判失败）", ok429)
        check("429 触发了全局冷却", B.rate_gate().cool_count >= 1,
              str(B.rate_gate().cool_count))

        # ---------- E. worker 池 ----------
        names = set()
        lock2 = threading.Lock()

        def fake_loop():
            with lock2:
                names.add(threading.current_thread().name)
            time.sleep(0.15)

        old_loop = B.worker_loop
        B.worker_loop = fake_loop
        try:
            B.CONFIG["max_workers"] = 3
            names.clear()
            B.worker_pool()
            check("max_workers=3 起了 3 个 worker 线程", len(names) == 3, str(names))
            B.CONFIG["max_workers"] = 1
            names.clear()
            B.worker_pool()
            check("max_workers=1 在当前线程跑（旧行为）",
                  names == {threading.current_thread().name}, str(names))
        finally:
            B.worker_loop = old_loop

        check("effective_workers 下限为 1", (B.CONFIG.update({"max_workers": 0}),
                                            B.effective_workers(),) [1] == 1)
        B.CONFIG["max_workers"] = 2
        check("分块并发受模型并发上限约束",
              B.effective_chunk_workers(10) == min(
                  B.CONFIG["chunk_workers"], B.CONFIG["llm_max_concurrency"]),
              str(B.effective_chunk_workers(10)))
        check("只有 1 块时分块并发为 1", B.effective_chunk_workers(1) == 1)

        # ---------- F. 失败退避 ----------
        reset_claims()
        t = mk("msg_fail", state="SEEN", attempts=2)
        B.CONFIG["retry_backoff_sec"] = 100
        B.CONFIG["provider_busy_backoff_sec"] = 900
        B.CONFIG["max_retries"] = 5
        t1 = B.mark_failed(t, RuntimeError("普通错误"))
        expect = 100 * (2 ** (t1["attempts"] - 1))
        check("退避按次数指数增长",
              abs((t1["next_retry_at"] - B.now()) - expect) < 3, f"{expect}")
        t2 = B.mark_failed(mk("msg_fail2", state="SEEN", attempts=1),
                           RuntimeError("503 service_unavailable"))
        check("上游繁忙时退避更长（900s）",
              (t2["next_retry_at"] - B.now()) >= 890, "")

        B.CONFIG["max_retries"] = 2
        sent = []
        old_reply = B.do_reply
        B.do_reply = lambda mid, body, files: sent.append((mid, body))
        try:
            B.mark_failed(mk("msg_fail3", state="SEEN", attempts=2),
                          RuntimeError("彻底失败"))
        finally:
            B.do_reply = old_reply
        check("重试用尽会回信告知发件人", len(sent) == 1 and "msg_fail3" in sent[0][0],
              str(sent))

        # ---------- H. 配置重载不能破坏正在被读的那份配置 ----------
        # 旧实现是 CONFIG.clear() + CONFIG.update()：读者会看到一份空 dict -> KeyError
        B.CONFIG["_tick_probe"] = 1
        held = B.CONFIG                      # 模拟：worker 已经拿到手的引用
        real_load = B._load_config
        B._load_config = lambda: dict(held, _source="config.json", _tick_probe=2)
        try:
            B.reload_config()
        finally:
            B._load_config = real_load
        check("重载后旧引用仍是完整配置（不是被清空）",
              bool(held.get("skip_dirs")) and held.get("max_workers") is not None,
              f"skip_dirs={held.get('skip_dirs')!r}")
        check("重载后全局 CONFIG 换成新配置", B.CONFIG.get("_tick_probe") == 2,
              str(B.CONFIG.get("_tick_probe")))
        check("重载是换新 dict，不是原地清空旧对象（读者不会看到空配置）",
              B.CONFIG is not held, "同一对象被 clear+update，读者有窗口读到空 dict")
        B.CONFIG.pop("_tick_probe", None)

        # 并发：一边重载一边读（不要求能复现 KeyError，但至少不能抛）
        errs = []
        stop = [False]

        def reader():
            while not stop[0]:
                try:
                    B.CONFIG["skip_dirs"]
                    B.CONFIG["max_review_files"]
                except Exception as e:
                    errs.append(f"{type(e).__name__}: {e}")
                    return

        real_log = B.log
        B.log = lambda *a, **k: None
        th = threading.Thread(target=reader, daemon=True)
        th.start()
        tick = [10]

        def churn():
            tick[0] += 1
            return dict(held, _source="config.json", _tick_probe=tick[0])

        B._load_config = churn
        try:
            for _ in range(3000):
                B.reload_config()
        finally:
            stop[0] = True
            th.join(timeout=2)
            B._load_config = real_load
            B.log = real_log
        check("并发重载 3000 次时读配置不抛异常", not errs, str(errs[:2]))
        B.CONFIG.pop("_tick_probe", None)

        # ---------- I. 确认信不能吞掉 worker 的进展 ----------
        for f in B.TASKS.glob("*.json"):
            f.unlink()
        mid_ack = "msg_ack_race"
        B.save_task({"message_id": mid_ack, "subject": "代码审核 - x",
                     "state": "SEEN", "attempts": 1,
                     "created_at_ts": B.now() - 999})
        B.CONFIG["ack_enabled"] = True
        B.CONFIG["ack_timeout_sec"] = 300
        old_ack = B.send_ack

        def fake_ack(_task):
            # 模拟：发确认信的网络调用期间，另一个 worker 把任务推进了
            cur = B.load_task(mid_ack)
            cur["state"] = "DOWNLOADED"
            cur["attempts"] = 3
            B.save_task(cur)

        B.send_ack = fake_ack
        try:
            B.ack_sweep()
        finally:
            B.send_ack = old_ack
        after = B.load_task(mid_ack)
        check("确认信不会把状态回写成 SEEN", after["state"] == "DOWNLOADED",
              str(after["state"]))
        check("确认信不会抹掉重试次数", after["attempts"] == 3, str(after["attempts"]))
        check("确认信标记确实写进去了", bool(after.get("ack_sent_at")))
        check("确认信不改其他字段", after["subject"] == "代码审核 - x")

        # ---------- G. 配置默认值 ----------
        ex = (BASE / "config.example.json").read_text(encoding="utf-8")
        for k in ("max_workers", "llm_max_concurrency", "chunk_workers",
                  "llm_rate_cooldown_sec"):
            check(f"配置示例含 {k}", f'"{k}"' in ex)
        src_txt = (BASE / "bot.py").read_text(encoding="utf-8")
        check("run_all 用 worker 池启动", "worker_pool()" in src_txt.split("def run_all")[1][:800])
        check("worker 池里的线程是 daemon（不会卡住退出）",
              "daemon=True)" in src_txt.split("def worker_pool")[1][:600])
    finally:
        for k, v in old_cfg.items():
            if v is None:
                B.CONFIG.pop(k, None)
            else:
                B.CONFIG[k] = v
        reset_claims()
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
