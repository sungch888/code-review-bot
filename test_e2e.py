#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端（e2e）harness：一个任务从「收邮件」到「回信」跑完整条链路

为什么需要它：
  真实的 bug 全落在「只有真实环境才走到」的路径上——
  A 的 `per_batch` IndexError（只在分批+无基线时触发）、
  B 的直链报错文案（只有真实 CDN 会返回 400）、
  C 的确认信回写（只有并发时才有那个窗口）。
  单元测试覆盖的是零件，这里用「假 agently-cli + 假模型」把整机跑起来。

harness 提供：
  - 假 CLI：message +read / attachment +download / message +reply（记录发出去的正文与附件）
  - 假模型：按提示词类型返回不同的罐装报告（单批 / 分批 / 汇总 / 修复）
  - 临时 WORK/TASKS/LOCKS/LOGS/配置，不碰线上数据
  - 可选本地 HTTP 服务：模拟超大附件直链（含「需要 MAIL5K」的 400）

覆盖场景：
  S1 正常单包：SEEN -> DOWNLOADED -> REVIEWED -> REPLIED（报告 md/html + 回信附件）
  S2 大包分批 + 无基线（A 的 IndexError 回归）
  S3 同一项目第 2 轮（A 的闭环端到端：基线识别 + 跟踪表 + 回信闭环数字）
  S4 超大附件直链需要登录态（B 的回归：如实说明 + 3 种替代办法）
  S5 直链带 Cookie 能取回（B 的功能）
  S6 模型失败 -> FAILED + 指数退避（C 的回归）
  S7 两个任务并发跑，互不踩（C 的端到端）
  S8 处理期间发确认信，不回写 worker 的进展（C2 的端到端）
"""
import http.server
import io
import os
import sys
import tempfile
import threading
import time
import types
import urllib.parse
import zipfile
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


# ---------------------------------------------------------------- 罐装报告
def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


ISSUES = [("严重", "app/main.py:10", "SQL 字符串拼接", "直接拼参数", "可读全库"),
          ("高危", "app/store.py:8", "缓存无过期", "只在写入处设值", "内存持续增长"),
          ("中危", "app/api.py:22", "无入参校验", "校验分散", "可被负数下单")]


def report_part(kind: str, tracking=None) -> str:
    """按段返回报告内容（模拟真实分段生成）"""
    full = report_text(tracking)
    heads = ["## 总体评价", "## 问题清单", "## 严重与高危问题详述",
             "## 安全性专项检查", "## 架构与可维护性", "## 测试与工程化",
             "## 结论与放行建议"]
    # 切成 [段首, 下一个段首) 的片段
    idx = []
    for h in heads:
        p = full.find(h)
        if p >= 0:
            idx.append((p, h))
    idx.sort()
    def seg(a, b):
        s = full.find(a)
        e = full.find(b) if b else len(full)
        return full[s:e].strip() if s >= 0 else ""
    if kind == "table":
        return seg("## 总体评价", "## 严重与高危问题详述")
    if kind == "detail":
        return seg("## 严重与高危问题详述", "## 安全性专项检查")
    if kind == "sections":
        return seg("## 安全性专项检查", "## 结论与放行建议")
    if kind == "verdict":
        return seg("## 结论与放行建议", None)
    return full


def report_text(tracking=None) -> str:
    rows = "\n".join(
        f"| {i} | {lv} | {loc} | {desc} | {cause} | {cons} | 改成参数化并统一校验 |"
        for i, (lv, loc, desc, cause, cons) in enumerate(ISSUES, 1))
    track = ""
    if tracking:
        trows = "\n".join(
            f"| {i} | {lv} | {loc} {desc} | {verdict} | app/main.py:{30 + i} | 见代码 |"
            for i, (lv, loc, desc, verdict) in enumerate(tracking, 1))
        track = ("## 上轮问题跟踪\n\n"
                 "| 编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明 |\n"
                 "| --- | --- | --- | --- | --- | --- |\n" + trows + "\n\n")
    return f"""# 代码审核报告

## 本次审核要求
无，按标准审核流程执行。

## 总体评价
这是一个订单服务的示例工程，技术栈 Python + sqlite3，代码规模约 120 行。
整体质量评分：78 分。评分依据：入口缺少统一校验，SQL 全部为字符串拼接，
缓存没有失效机制；但模块划分清楚、命名直白。

{track}## 问题清单

| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
{rows}

## 严重与高危问题详述

```python
cur.execute("SELECT * FROM orders WHERE id = " + str(order_id))
```

同一段判断在入口和 DAO 各写了一份，改一处会漏另一处；一旦有人只改一边，
未改的那条路径就会绕过校验。

## 安全性专项检查
命令注入未发现；SQL 注入存在 2 处；路径穿越未发现；硬编码密钥未发现；
敏感信息泄漏未发现；不安全反序列化未发现；依赖风险未评估；日志泄漏未发现；
并发与竞态存在（缓存无锁）；资源耗尽存在（缓存无上限）。

## 架构与可维护性
模块划分为 main / store / api 三层，但数据访问与业务判断混在同一函数里；
错误处理基本没有；类型标注缺失；命名尚可。

## 测试与工程化
没有测试用例，没有 CI 配置，依赖清单缺失；建议先补 3 个入口校验的回归用例。

## 结论与放行建议
修改后放行。必须先处理 SQL 字符串拼接（2 处）与缓存过期策略。
"""


CHUNK_TEXT = """### 一、本批问题清单

| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 高危 | app/main.py:10 | 本批 SQL 拼接 | 直接拼参数 | 可读全库 | 参数化 | | 代码事实 main.py:10 |

### 二、严重与高危问题的代码片段
见问题清单。
"""


class FakeProvider:
    """按提示词类型返回不同内容；可配置失败次数"""

    def __init__(self, fail_first=0, delay=0.0):
        self.configured = True
        self.model = "fake-model"
        self.has_key = True
        self.protocol = "openai"
        self.fail_left = fail_first
        self.delay = delay
        self.tracking = None
        self.prompts = []
        self.lock = threading.Lock()

    def describe(self):
        return "fake://model"

    def chat(self, messages, model="", max_tokens=0):
        prompt = messages[-1]["content"] if messages else ""
        with self.lock:
            self.prompts.append(prompt)
            if self.fail_left > 0:
                self.fail_left -= 1
                raise B.LLM.LLMError("HTTP 401 无效的 API Key")
        if self.delay:
            time.sleep(self.delay)
        # 顺序要紧：第 2 段的提示词里也提到「第 1 段」（指已输出的内容）
        if "第 4 段" in prompt:
            text = report_part("verdict", self.tracking)
        elif "第 3 段" in prompt:
            text = report_part("sections", self.tracking)
        elif "第 2 段" in prompt:
            text = report_part("detail", self.tracking)
        elif "第 1 段" in prompt:
            text = report_part("table", self.tracking)
        elif "未通过结构校验" in prompt:
            text = report_text(self.tracking)
        elif "分批代码审核的第" in prompt:
            text = CHUNK_TEXT
        elif "总审" in prompt or "合并成" in prompt:
            text = report_text(self.tracking)
        else:
            text = report_text(self.tracking)
        return types.SimpleNamespace(text=text, elapsed=0.01, finish_reason="stop",
                                     truncated=False)


# ---------------------------------------------------------------- 本地直链服务
class UrlServer:
    def __init__(self, blobs: dict):
        self.blobs = blobs          # path -> (bytes, headers) or ("login", None)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                p = urllib.parse.urlparse(self.path).path
                spec = outer.blobs.get(p)
                if spec is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                kind, body = spec
                if kind == "login":
                    if "MAIL5K=" not in (self.headers.get("Cookie") or ""):
                        self.send_response(400)
                        self.send_header("X-Error-Message", "Need MAIL5K= in Cookie")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = outer.blobs["/zip"][1]
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.srv.server_address[1]}{path}"

    def close(self):
        self.srv.shutdown()


# ---------------------------------------------------------------- harness
class Env:
    def __init__(self, cfg: dict = None, provider: FakeProvider = None):
        # 必须放在 BASE 之下：do_reply 会算附件的相对路径（./work/...）
        root = BASE / ".e2e_tmp"
        root.mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="run_", dir=str(root)))
        self.saved = {}
        for name, val in (("WORK", self.tmp / "work"), ("TASKS", self.tmp / "tasks"),
                          ("LOCKS", self.tmp / "lock"), ("LOGS", self.tmp / "logs"),
                          ("REPORTS", self.tmp / "reports"),
                          ("ACKS", self.tmp / "ack"), ("FP_DIR", self.tmp / "fp"),
                          ("LEDGER_DIR", self.tmp / "ledger")):
            if hasattr(B, name):
                self.saved[name] = getattr(B, name)
                setattr(B, name, val)
            val.mkdir(parents=True, exist_ok=True)

        self.base_cfg = dict(B.CONFIG)
        self.cfg = dict(self.base_cfg)
        self.cfg.update({"llm_backend": "http", "require_approval": False,
                         "ack_enabled": True, "retry_backoff_sec": 60,
                         "max_retries": 3, "max_review_files": 200,
                         "review_chunk_files": 40, "chunk_workers": 1,
                         "llm_max_concurrency": 2, "max_file_chars": 10000})
        self.cfg.update(cfg or {})
        self.saved["_load_config"] = B._load_config
        B._load_config = lambda: dict(self.cfg)
        B.CONFIG = dict(self.cfg)
        B._GATE = None

        self.provider = provider or FakeProvider()
        self.saved["Provider"] = B.LLM.Provider
        B.LLM.Provider = lambda cfg=None: self.provider

        self.saved["PI"] = B.PI
        B.PI = [sys.executable, str(BASE / "testdata" / "pi_stub.py")]

        self.saved["run_cli"] = B.run_cli
        B.run_cli = self._fake_cli

        self.saved["log"] = B.log
        self.logs = []
        B.log = lambda msg, level="INFO": self.logs.append(f"[{level}] {msg}")

        self.msgs = {}
        self.blobs = {}
        self.names = {}
        self.replies = {}

    # ---- 假 CLI ----
    def _fake_cli(self, args, timeout=240, _retry=True):
        a = [str(x) for x in args]
        if a[:2] != ["message", "+read"] and a[:2] != ["attachment", "+download"] \
                and a[:2] != ["message", "+reply"]:
            raise AssertionError(f"未预期的 CLI 调用: {a}")
        if a[:2] == ["message", "+read"]:
            mid = a[a.index("--id") + 1]
            m = self.msgs.get(mid)
            if m is None:
                raise B.CliError("邮件不存在")
            return {"message_id": mid, "subject": m["subject"], "body": m.get("body", ""),
                    "attachments": m["attachments"],
                    "from": {"email": "sender@example.com", "name": "发件人"}}
        if a[:2] == ["attachment", "+download"]:
            mid = a[a.index("--msg") + 1]
            aid = a[a.index("--att") + 1]
            dl = B.WORK / mid / "downloads"
            dl.mkdir(parents=True, exist_ok=True)
            p = dl / (self.names.get(aid) or f"{aid}.zip")
            p.write_bytes(self.blobs[aid])
            return {"saved_to": str(p)}
        mid = a[a.index("--id") + 1]
        body = a[a.index("--body") + 1]
        files = [a[i + 1] for i, x in enumerate(a) if x == "--attachment"]
        self.replies.setdefault(mid, []).append({"body": body, "files": files})
        return {"ok": True}

    # ---- 造数据 ----
    def add_task(self, mid, subject, zip_files=None, att=None, url=None, size=None):
        """att: 普通附件（有 attachment_id）；url: 超大附件（只有 download_url）"""
        if zip_files is not None:
            aid = f"att_{mid[-6:]}"
            self.blobs[aid] = make_zip(zip_files)
            self.names[aid] = "code.zip"
            att = {"attachment_id": aid, "filename": "code.zip",
                   "size": size or len(self.blobs[aid])}
        attachments = []
        if att:
            attachments.append(att)
        if url:
            attachments.append({"filename": "big.zip",
                                "size": size or 123456, "download_url": url})
        self.msgs[mid] = {"subject": subject, "attachments": attachments}
        B.save_task({"message_id": mid, "subject": subject, "state": "SEEN",
                     "created_at_ts": time.time(), "attempts": 0, "next_retry_at": 0,
                     "from": "sender@example.com", "from_name": "发件人"})
        return B.load_task(mid)

    def run(self, mid):
        return B.process_task(mid)

    def body(self, mid):
        rs = self.replies.get(mid) or []
        return rs[-1]["body"] if rs else ""

    def files(self, mid):
        rs = self.replies.get(mid) or []
        return rs[-1]["files"] if rs else []

    def close(self):
        B.PI = self.saved["PI"]
        B.run_cli = self.saved["run_cli"]
        B.log = self.saved["log"]
        B.LLM.Provider = self.saved["Provider"]
        B._load_config = self.saved["_load_config"]
        for k, v in self.saved.items():
            if k in ("run_cli", "log", "Provider", "_load_config", "PI"):
                continue
            setattr(B, k, v)
        B.CONFIG = self.base_cfg
        B._GATE = None
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


SMALL = {"app/main.py": "def f():\n    return 1\n",
         "app/store.py": "def g():\n    return 2\n",
         "app/api.py": "def h():\n    return 3\n"}


def main():
    # ================= S1 正常单包 =================
    env = Env()
    try:
        t = env.add_task("msg_s1", "代码审核 - demo 订单服务", SMALL)
        r = env.run("msg_s1")
        t = B.load_task("msg_s1")
        check("S1 走完 -> REPLIED", r == "REPLIED" and t["state"] == "REPLIED", r)
        rpt = Path(t["report"])
        check("S1 产出 Markdown 报告", (rpt / "代码审核报告.md").is_file())
        check("S1 产出 HTML 报告", (rpt / "代码审核报告.html").is_file())
        check("S1 产出摘要", (rpt / "summary.md").is_file())
        check("S1 报告结构合规（无修复重跑）",
              not any("未通过结构校验" in p for p in env.provider.prompts),
              f"{len(env.provider.prompts)} 次调用")
        check("S1 回信带两个附件", len(env.files("msg_s1")) == 2, str(env.files("msg_s1")))
        check("S1 回信含审核摘要", "审核摘要" in env.body("msg_s1"))
        check("S1 统计到 3 条问题", t.get("new_issues") == 3, str(t.get("new_issues")))
        check("S1 没有上一轮基线", not t.get("baseline_mid"))
    finally:
        env.close()

    # ================= S2 大包分批 + 无基线（A 回归）=================
    env = Env({"review_chunk_files": 10})
    try:
        big = {f"pkg/m{i:02d}.py": f"def f{i}():\n    return {i}\n" for i in range(45)}
        env.add_task("msg_s2", "代码审核 - big 项目", big)
        r = env.run("msg_s2")
        t = B.load_task("msg_s2")
        rpt = Path(t["report"])
        check("S2 分批审核不崩（A 的 IndexError 回归）", r == "REPLIED", r)
        chunks = sorted(rpt.glob("chunk_*_报告.md"))
        check("S2 真的分了 5 批", len(chunks) == 5, f"{len(chunks)} 批")
        check("S2 有最终报告", (rpt / "代码审核报告.md").is_file())
        check("S2 无基线时不要求跟踪表",
              "上轮问题跟踪" not in (rpt / "代码审核报告.md").read_text(encoding="utf-8"))
        check("S2 回信正常", "审核摘要" in env.body("msg_s2"))
    finally:
        env.close()

    # ================= S3 第 2 轮闭环（A 端到端）=================
    env = Env()
    try:
        env.add_task("msg_s3a", "代码审核 - cycle 项目", SMALL)
        env.run("msg_s3a")
        env.provider.tracking = [
            ("严重", "app/main.py:10", "SQL 字符串拼接", "已修复"),
            ("高危", "app/store.py:8", "缓存无过期", "未修复"),
            ("中危", "app/api.py:22", "无入参校验", "部分修复"),
        ]
        env.add_task("msg_s3b", "代码审核 - cycle 项目（第2轮）", SMALL)
        r = env.run("msg_s3b")
        t = B.load_task("msg_s3b")
        check("S3 第 2 轮走通", r == "REPLIED", r)
        check("S3 认出上一轮基线", t.get("baseline_mid") == "msg_s3a",
              str(t.get("baseline_mid")))
        check("S3 轮次记为第 2 轮", t.get("review_round") == 2, str(t.get("review_round")))
        tr = t.get("tracking") or {}
        check("S3 闭环统计正确", (tr.get("total"), tr.get("fixed"), tr.get("unfixed"),
                                  tr.get("partial")) == (3, 1, 1, 1), str(tr))
        body = env.body("msg_s3b")
        check("S3 回信含闭环摘要", "上轮问题跟踪" in body and "已修复 1" in body, body[-300:])
        check("S3 回信列出未修完的项", "缓存无过期" in body, body[-300:])
        md = (Path(t["report"]) / "代码审核报告.md").read_text(encoding="utf-8")
        check("S3 报告含跟踪表", "上轮问题跟踪" in md and "本轮判定" in md)
    finally:
        env.close()

    # ================= S4 直链需要登录态（B 回归）=================
    srv = UrlServer({"/login": ("login", b"")})
    env = Env()
    try:
        t = env.add_task("msg_s4", "代码审核 - 超大附件", url=srv.url("/login"), size=371907)
        r = env.run("msg_s4")
        t = B.load_task("msg_s4")
        body = env.body("msg_s4")
        check("S4 直链取不回时仍然回信（不崩）", r == "REPLIED_LINK_ONLY", r)
        check("S4 错误里写清了登录态", "登录态" in (t.get("last_error") or ""),
              str(t.get("last_error"))[:120])
        check("S4 回信解释「超大附件」", "超大附件" in body)
        check("S4 回信给 3 种替代办法", "换成普通附件重发" in body)
        check("S4 不给发件人看服务端英文报错", "MAIL5K" not in body)
        check("S4 没有误报 20MB", "20MB 上限" not in body)
    finally:
        env.close()

    # ================= S5 直链 + Cookie 能取回（B 功能）=================
    srv5 = UrlServer({"/login": ("login", b""), "/zip": ("zip", make_zip(SMALL))})
    env = Env({"download_cookie": "MAIL5K=test-cookie"})
    try:
        env.add_task("msg_s5", "代码审核 - 直链可取", url=srv5.url("/login"), size=2048)
        r = env.run("msg_s5")
        t = B.load_task("msg_s5")
        check("S5 带 Cookie 的直链能下载并审核", r == "REPLIED", r)
        check("S5 报告已产出",
              (Path(t["report"]) / "代码审核报告.md").is_file())
        check("S5 日志里有直链下载成功", any("直链下载成功" in x for x in env.logs),
              str([x for x in env.logs if "直链" in x][:2]))
    finally:
        env.close()

    # ================= S6 模型失败 -> FAILED + 退避（C 回归）=================
    env = Env(provider=FakeProvider(fail_first=99))
    try:
        env.add_task("msg_s6", "代码审核 - 模型挂了", SMALL)
        cand = B.load_task("msg_s6")
        B.process_candidate(cand)
        t = B.load_task("msg_s6")
        check("S6 模型失败 -> FAILED", t["state"] == "FAILED", str(t["state"]))
        check("S6 记录了错误", "401" in (t.get("last_error") or ""), str(t.get("last_error")))
        check("S6 设置了退避时间", (t.get("next_retry_at") or 0) > time.time())
        check("S6 未到重试上限时不发失败通知", not env.replies.get("msg_s6"))
    finally:
        env.close()

    # ================= S7 两个任务并发（C 端到端）=================
    env = Env()
    try:
        env.provider.delay = 0.05
        env.add_task("msg_s7a", "代码审核 - 并发A", SMALL)
        env.add_task("msg_s7b", "代码审核 - 并发B", SMALL)
        out = {}

        def go(mid):
            out[mid] = B.process_task(mid)

        ths = [threading.Thread(target=go, args=(m,)) for m in ("msg_s7a", "msg_s7b")]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        ta, tb = B.load_task("msg_s7a"), B.load_task("msg_s7b")
        check("S7 两个任务都完成", out.get("msg_s7a") == "REPLIED"
              and out.get("msg_s7b") == "REPLIED", str(out))
        check("S7 各自都有报告",
              (Path(ta["report"]) / "代码审核报告.md").is_file()
              and (Path(tb["report"]) / "代码审核报告.md").is_file())
        check("S7 各回一封信", len(env.replies.get("msg_s7a", [])) == 1
              and len(env.replies.get("msg_s7b", [])) == 1)
        check("S7 报告内容各自独立（没有写串）",
              "订单服务" in (Path(ta["report"]) / "代码审核报告.md").read_text(encoding="utf-8"))
        check("S7 没有残留任务临时文件", not list(B.TASKS.glob("*.tmp")))
    finally:
        env.close()

    # ================= S8 处理期间发确认信（C2 端到端）=================
    env = Env({"ack_timeout_sec": 0})
    try:
        env.add_task("msg_s8", "代码审核 - 确认信并发", SMALL)
        t0 = B.load_task("msg_s8")
        t0["created_at_ts"] = time.time() - 999
        B.save_task(t0)
        env.provider.delay = 0.3          # 让审核期间足够长

        res = {}

        def go():
            res["r"] = B.process_task("msg_s8")

        th = threading.Thread(target=go)
        th.start()
        time.sleep(0.15)
        B.ack_sweep()                     # 处理中发确认信
        th.join()
        t = B.load_task("msg_s8")
        check("S8 确认信没有把任务拉回 SEEN", t["state"] == "REPLIED", str(t["state"]))
        check("S8 确认信标记进台账且保留", bool(B.ack_sent_at("msg_s8")))
        check("S8 正常发了报告回信", res.get("r") == "REPLIED" and env.replies.get("msg_s8"),
              str(res))
    finally:
        env.close()

    # ================= S9 pi 后端全链路（合并流水线后仍要能跑）=================
    env = Env({"llm_backend": "pi", "pi_model": "",
               "review_chunk_files": 10})
    try:
        txt = env.tmp / "pi_report.md"
        txt.write_text(report_text(), encoding="utf-8")
        os.environ["PI_STUB_TEXT"] = str(txt)
        big = {f"pkg/m{i:02d}.py": f"def f{i}():\n    return {i}\n" for i in range(25)}
        env.add_task("msg_s9", "代码审核 - pi 后端", big)
        r = env.run("msg_s9")
        t = B.load_task("msg_s9")
        rpt = Path(t["report"])
        check("S9 pi 后端走完 -> REPLIED", r == "REPLIED", r)
        check("S9 pi 后端也分了 3 批", len(list(rpt.glob("chunk_*_报告.md"))) == 3,
              str(len(list(rpt.glob("chunk_*_报告.md")))))
        md = rpt / "代码审核报告.md"
        check("S9 pi 后端产出最终报告", md.is_file() and md.stat().st_size > 500)
        check("S9 pi 后端写了 .pi.log 原始输出", (B.LOGS / "msg_s9.pi.log").is_file())
        check("S9 pi 后端报告同样经过来源擦除/校验",
              not any("Brooks" in md.read_text(encoding="utf-8") for _ in [0]))
        check("S9 回信正常", "审核摘要" in env.body("msg_s9"))
    finally:
        os.environ.pop("PI_STUB_TEXT", None)
        env.close()

    # ================= S10 pi 后端上游繁忙（503）===============
    env = Env({"llm_backend": "pi", "fallback_model": ""})
    try:
        os.environ["PI_STUB_MODE"] = "busy"
        env.add_task("msg_s10", "代码审核 - pi 繁忙", SMALL)
        cand = B.load_task("msg_s10")
        B.process_candidate(cand)
        t = B.load_task("msg_s10")
        check("S10 pi 繁忙 -> FAILED（不静默）", t["state"] == "FAILED", str(t["state"]))
        check("S10 错误说明是上游过载", "繁忙" in (t.get("last_error") or ""),
              str(t.get("last_error"))[:120])
        check("S10 已按提供商繁忙拉长退避",
              (t.get("next_retry_at") or 0) > time.time() + 100,
              str((t.get("next_retry_at") or 0) - time.time()))
    finally:
        os.environ.pop("PI_STUB_MODE", None)
        env.close()

    # ================= S11 poll 扫描不能把「正在处理」的任务拉回队列 ============
    env = Env()
    try:
        env.add_task("msg_s11", "代码审核 - 扫描不抢活", SMALL)
        t = B.load_task("msg_s11")
        t["state"] = B.State.DOWNLOADED          # 模拟：worker 正在处理
        B.save_task(t)
        r = B.enqueue(dict(env.msgs["msg_s11"], message_id="msg_s11"))
        after = B.load_task("msg_s11")
        check("扫描不会把处理中的任务拉回 SEEN", after["state"] == B.State.DOWNLOADED,
              str(after["state"]))
        check("扫描对处理中的任务返回「进行中」而不是重新入队",
              r == "DONE_SKIP_IN_PROGRESS", r)
    finally:
        env.close()

    # ================= S12 摘要从报告生成（不再用兼底文案）=================
    env = Env()
    try:
        env.add_task("msg_s12", "代码审核 - 摘要", SMALL)
        env.run("msg_s12")
        t = B.load_task("msg_s12")
        summ = (Path(t["report"]) / "summary.md").read_text(encoding="utf-8")
        check("摘要不是那句兼底文案",
              "代码包已完成静态代码审核，详细结论见附件报告。" not in summ, summ[:80])
        check("摘要含结论档位",
              any(k in summ for k in ("可放行", "修改后放行", "不建议放行")), summ[:200])
        check("摘要含问题条数", "条问题" in summ, summ[:200])
        check("摘要在 15 行以内", len(summ.splitlines()) <= 15, str(len(summ.splitlines())))
        body = env.body("msg_s12")
        check("回信正文里的摘要是提炼后的内容",
              "代码包已完成静态代码审核，详细结论见附件报告。" not in body, body[:300])
        check("报告注意事项里没有「摘要缺失」",
              not any("摘要缺失" in w for w in (t.get("report_warnings") or [])),
              str(t.get("report_warnings")))
    finally:
        env.close()

    # ================= S13 resend：补发不重审，摘要自动修好 ============
    env = Env()
    try:
        env.add_task("msg_s13", "代码审核 - 补发", SMALL)
        env.run("msg_s13")
        t = B.load_task("msg_s13")
        rpt = Path(t["report"])
        # 模拟「摘要坏掉 + 带着过时警告」
        (rpt / "summary.md").write_text("代码包已完成静态代码审核，详细结论见附件报告。",
                                         encoding="utf-8")
        t["report_warnings"] = ["摘要缺失，已使用兼底文案"]
        B.save_task(t)
        n_before = len(env.provider.prompts)

        t2 = B.resend_task("msg_s13")
        check("resend 没有重新调用模型（不重审）",
              len(env.provider.prompts) == n_before, f"{n_before} -> {len(env.provider.prompts)}")
        summ = (rpt / "summary.md").read_text(encoding="utf-8")
        check("resend 把摘要修好了", "详细结论见附件报告" not in summ, summ[:80])
        check("resend 摘要有结论档位",
              any(k in summ for k in ("可放行", "修改后放行", "不建议放行")), summ[:200])
        check("resend 清掉了过时的「摘要缺失」警告",
              not any("摘要缺失" in w for w in (t2.get("report_warnings") or [])),
              str(t2.get("report_warnings")))
        check("resend 记录了补发次数", t2.get("resend_count") == 1 and t2.get("resent_at"),
              str(t2.get("resend_count")))
        check("resend 又发了一封邮件", len(env.replies.get("msg_s13", [])) == 2,
              str(len(env.replies.get("msg_s13", []))))
        check("resend 的邮件带了报告附件",
              len(env.files("msg_s13")) >= 1, str(env.files("msg_s13")))
        check("resend 邮件正文里没有兼底文案",
              "详细结论见附件报告" not in env.body("msg_s13"), env.body("msg_s13")[:200])
    finally:
        env.close()

    # ================= S19 多轮评分同口径（修好了分数必须体现）=================
    env = Env()
    try:
        code = {f"app/m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(10)}
        # 第一轮：8 条严重 -> 分数应该很低
        env.add_task("msg_s19a", "代码审核 - 评分对比", code)
        env.run("msg_s19a")
        ta = B.load_task("msg_s19a")
        check("S19 第一轮算了分", ta.get("score") is not None, str(ta.get("score")))
        check("S19 第一轮分数带口径版本",
              (ta.get("score_detail") or {}).get("version") == B.scoring_version(),
              str(ta.get("score_detail")))

        # 第二轮：同一份代码，严重问题全修好（只剩中低危）
        env.provider.tracking = [("严重", "app/main.py:10", "旧问题", "已修复")]
        fixed = dict(code)
        fixed["app/m0.py"] = "def f0():\n    return 0  # fixed"
        env.add_task("msg_s19b", "代码审核 - 评分对比（第2轮）", fixed)
        env.run("msg_s19b")
        tb = B.load_task("msg_s19b")
        check("S19 第二轮记住了上轮分数", tb.get("score_prev") is not None,
              str(tb.get("score_prev")))
        check("S19 两轮用同一口径（版本一致）",
              (tb.get("score_detail") or {}).get("version") == B.scoring_version())
        body = env.body("msg_s19b")
        basis = (tb.get("score_detail") or {}).get("basis")
        check("S19 分数按问题台账算", basis == "ledger", str(tb.get("score_detail")))
        check("S19 回信给出台账评分对比",
              ("台账评分" in body if basis == "ledger" else "同口径评分" in body), body[-600:])
        check("S19 回信给出按上轮清单的加权修复率", "修复率" in body, body[-600:])
        check("S19 修复率已算进任务字段",
              ((tb.get("tracking") or {}).get("recovery") or {}).get("pct") is not None,
              str((tb.get("tracking") or {}).get("recovery")))
        md = (Path(tb["report"]) / "代码审核报告.md").read_text(encoding="utf-8")
        check("S19 报告里的分数是系统算的（带口径版本）", "统一口径 v" in md, md[:300])
        recalc = B.compute_score(B.extract_issue_rows(md))
        check("S19 报告分数与重算一致", recalc["score"] == tb.get("score"),
              f'{recalc["score"]} vs {tb.get("score")}')
        check("S19 上轮分数是 int（可直接比较）",
              isinstance(tb.get("score_prev"), int), str(tb.get("score_prev")))
    finally:
        env.close()

    # ================= S18 内容匹配：标题完全不同也认得出复审 =================
    env = Env()
    try:
        code = {f"app/m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(12)}
        code.update({"README.md": "说明\n"})
        env.add_task("msg_s18a", "代码审核 - 订单系统", code)
        env.run("msg_s18a")
        ta = B.load_task("msg_s18a")
        check("S18 第一轮完成", ta["state"] == B.State.REPLIED, ta["state"])
        check("S18 第一轮存了代码包指纹", bool(B.load_fp("msg_s18a")),
              str(list((B.load_fp("msg_s18a") or {}).keys())))

        # 改了两个文件、外层目录改名、**标题完全不一样**
        code2 = dict(code)
        code2["app/m0.py"] = "def f0():\n    return 100  # fixed"
        code2["app/m1.py"] = "def f1():\n    return 101  # fixed"
        env.provider.tracking = [
            ("严重", "app/main.py:10", "SQL 拼接", "已修复"),
            ("高危", "app/store.py:8", "缓存无过期", "未修复"),
            ("中危", "app/api.py:22", "无入参校验", "部分修复"),
        ]
        env.add_task("msg_s18b", "代码审核 - 完全不同的标题 改好了", code2)
        env.run("msg_s18b")
        tb = B.load_task("msg_s18b")
        check("S18 标题不同也认出了同一项目",
              tb.get("baseline_mid") == "msg_s18a", str(tb.get("baseline_mid")))
        check("S18 匹配依据是内容而非标题",
              (tb.get("baseline_match") or {}).get("via") == "content",
              str(tb.get("baseline_match")))
        body = env.body("msg_s18b")
        check("S18 回信写明了按内容匹配", "按代码包内容匹配" in body, body[-500:])
        check("S18 回信给了相似度", "相似度" in body, body[-500:])
        check("S18 报告里有上轮跟踪表",
              "上轮问题跟踪" in (Path(tb["report"]) / "代码审核报告.md").read_text(
                  encoding="utf-8"))
    finally:
        env.close()

    # ================= S17 复审没匹配上基线时要明说 =================
    env = Env()
    try:
        env.add_task("msg_s17", "代码审核 - 某项目（第2轮）", SMALL)
        env.run("msg_s17")
        t = B.load_task("msg_s17")
        check("标题带轮次但没基线 -> 标记了不匹配", t.get("baseline_mismatch") is True,
              str(t.get("baseline_mismatch")))
        body = env.body("msg_s17")
        check("回信里明说未匹配到上一轮", "未匹配到上一轮" in body, body[-400:])
        check("回信里说清了识别依据不是标题",
              "不是邮件标题" in body, body[-400:])
        check("回信里给了两条替代做法",
              "【审核要求】" in body and "指定基线" in body, body[-400:])
    finally:
        env.close()

    # ================= S14 只审不发 / 复审（不读邮件、不发信）=================
    env = Env()
    try:
        env.add_task("msg_s14", "代码审核 - 只审不发", SMALL)
        env.run("msg_s14")
        t = B.load_task("msg_s14")
        t["no_reply"] = True
        B.save_task(t)
        env.replies.clear()
        n_before = len(env.provider.prompts)
        rpt = B.review_existing("msg_s14", reply=False)
        check("review_existing 生成了报告", Path(rpt, "代码审核报告.md").is_file(), rpt)
        check("review_existing 默认不发邮件", not env.replies.get("msg_s14"),
              str(env.replies.get("msg_s14")))
        check("review_existing 确实重新调了模型",
              len(env.provider.prompts) > n_before, str(len(env.provider.prompts)))
        check("review_existing 后状态是 REVIEWED（不是 REPLIED）",
              B.load_task("msg_s14")["state"] == B.State.REVIEWED,
              B.load_task("msg_s14")["state"])
    finally:
        env.close()

    # ================= S15 续写重复 / 重复检测 =================
    env = Env()
    try:
        # 1) 裁剪器：模型续写时从头重写
        prev = ("| 1 | 严重 | a.py:1 | 问题一 | 成因 | 后果 | 建议 | 证据 |\n"
                "| 2 | 高 | b.py:2 | 问题二 | 成因 | 后果 | 建议 | 证据 |\n\n"
                "## 详细\n已经写好的正文……")
        again = ("| 1 | 严重 | a.py:1 | 问题一 | 成因 | 后果 | 建议 | 证据 |\n"
                 "| 2 | 高 | b.py:2 | 问题二 | 成因 | 后果 | 建议 | 证据 |\n\n"
                 "## 详细\n已经写好的正文……\n| 3 | 中 | c.py:3 | 问题三 | 成因 | 后果 | 建议 | 证据 |\n")
        cut = B._strip_repeated_prefix(prev, again)
        check("续写重复前缀被裁掉", cut.count("| 1 | 严重") == 0, cut[:120])
        check("裁剪后保留了新内容", "| 3 | 中" in cut, cut[-120:])
        check("没有重复时不裁剪", B._strip_repeated_prefix(prev, "全新的内容\n第二行\n第三行"))
        check("空输入不炸", B._strip_repeated_prefix("", "x") == "x"
              and B._strip_repeated_prefix("x", "") == "")

        # 2) 重复检测
        dup_md = ("| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |\n"
                  "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                  "| 1 | 严重 | a.py:1 | 同一个问题 | x | y | z | w |\n"
                  "| 2 | 高 | a.py:1 | 同一个问题 | x | y | z | w |\n")
        check("能数出重复条数", B.count_duplicate_issues(dup_md) == 1,
              str(B.count_duplicate_issues(dup_md)))
        check("无重复时为 0", B.count_duplicate_issues(
            "| 1 | 严重 | a.py:1 | 问题一 | x | y | z | w |\n"
            "| 2 | 高 | b.py:2 | 问题二 | x | y | z | w |\n") == 0)
    finally:
        env.close()

    # ================= S16 报告分段生成（不撞 max_tokens）=================
    env = Env()
    try:
        env.add_task("msg_s16", "代码审核 - 分段", SMALL)
        env.run("msg_s16")
        t = B.load_task("msg_s16")
        md = (Path(t["report"]) / "代码审核报告.md").read_text(encoding="utf-8")
        kinds = [k for k, _ in B.REPORT_PARTS]
        check("报告分了 5 段（清单由本地生成，不在段里）", len(kinds) == 5, str(kinds))
        check("清单段不在模型分段里（本地生成，避免超限）",
              "table" not in kinds, str(kinds))
        seg_calls = [p for p in env.provider.prompts if "第 " in p and "段" in p]
        check("每段各调用一次模型（而不是一次写完整份）", len(seg_calls) >= 4,
              str(len(seg_calls)))
        for title in ("总体评价", "问题清单", "严重与高危问题详述",
                      "安全性专项检查", "架构与可维护性", "测试与工程化",
                      "结论与放行建议"):
            check(f"分段拼接后含章节「{title}」", title in md, "")
        check("分段拼接后结构校验通过", B.validate_report(md, 3) == [],
              str(B.validate_report(md, 3)))
        check("分段生成没有重复问题", B.count_duplicate_issues(md) == 0,
              str(B.count_duplicate_issues(md)))
    finally:
        env.close()

    srv.close()
    srv5.close()
    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
