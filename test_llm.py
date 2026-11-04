"""通用模型接入（lib/llm.py）的测试：python test_llm.py

用本地 mock 服务验证：填 base_url + api_key 就能审核，不依赖任何本地 CLI。
"""
import json
import shutil
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import llm as L          # noqa: E402
import bot as B          # noqa: E402

results = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))
    results.append(cond)


# ---------------------------------------------------------------- mock 服务
COMPLIANT = None


def compliant_text() -> str:
    parts = []
    for s in B.REPORT_SPEC["sections"]:
        extra = "\n整体质量评分：82 分（依据：结构清晰但缺测试覆盖）。" \
            if s["title"] == "总体评价" else ""
        parts.append(f"## {s['title']}\n这一节是针对被审代码的具体分析说明，覆盖常见风险点。{extra}")
        if s.get("type") == "table":
            cols = " | ".join(s["columns"])
            rows = [f"| {i} | 中 | app.py:{i} | 这里有一个真实存在的问题 | 建议这样改 |"
                    for i in range(1, 9)]
            parts.append(f"| {cols} |\n|---|---|---|---|---|\n" + "\n".join(rows))
    parts.append("## 结论与放行建议\n整体判断：修改后放行。必须先处理上面列出的高优先级问题。")
    return "\n\n".join(parts)


class Mock(BaseHTTPRequestHandler):
    calls = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Mock.calls.append({"path": urllib.parse.urlparse(self.path).path,
                           "headers": dict(self.headers), "body": None})
        p = urllib.parse.urlparse(self.path).path
        if p == "/v1/models":
            return self._send(200, {"data": [{"id": "mock-b"}, {"id": "mock-a"},
                                             {"id": "mock-b"}]})
        if p == "/api/tags":
            return self._send(200, {"models": [{"name": "llama3:8b"}, {"name": "qwen2.5-coder"}]})
        return self._send(404, {"error": {"message": "no such path"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        Mock.calls.append({"path": urllib.parse.urlparse(self.path).path,
                           "headers": dict(self.headers), "body": body})
        model = body.get("model") or ""
        if model == "err-503":
            return self._send(503, {"error": {"message": "Service is too busy",
                                              "type": "service_unavailable_error"}})
        if model == "err-429":
            return self._send(429, {"error": {"message": "rate limit exceeded"}})
        if model == "err-401":
            return self._send(401, {"error": {"message": "invalid api key"}})
        if model == "err-404":
            return self._send(404, {"error": {"message": "The model `x` does not exist"}})
        if model == "err-400ctx":
            return self._send(400, {"error": {"message": "maximum context length exceeded"}})
        text = compliant_text()
        if model == "empty":
            return self._send(200, {"choices": [{"message": {"content": ""},
                                                 "finish_reason": "stop"}]})
        if model == "reasoning":
            return self._send(200, {"choices": [{"message": {"reasoning_content": text},
                                                 "finish_reason": "stop"}]})
        trunc = (model == "trunc")
        if self.path.startswith("/v1/messages"):
            return self._send(200, {"content": [{"type": "text", "text": text}],
                                    "stop_reason": "max_tokens" if trunc else "end_turn"})
        if self.path.startswith("/api/chat"):
            return self._send(200, {"message": {"content": text},
                                    "done_reason": "length" if trunc else "stop"})
        return self._send(200, {"choices": [{"message": {"content": text},
                                             "finish_reason": "length" if trunc else "stop"}],
                                "usage": {"total_tokens": 123}})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{PORT}"


def cfg(**kw):
    c = {"llm_base_url": URL, "llm_api_key": "sk-FAKE-NOT-REAL-0001",
         "llm_model": "mock-a", "llm_protocol": "openai", "llm_backend": "http",
         "llm_timeout_sec": 20, "llm_max_output_tokens": 4000}
    c.update(kw)
    return c


# ---------- 1. 地址归一化 / 协议识别 ----------
check("裸域名自动补 https 与 /v1",
      L.normalize_base("api.deepseek.com") == "https://api.deepseek.com/v1",
      L.normalize_base("api.deepseek.com"))
check("已带 /v1 不重复补",
      L.normalize_base("https://api.openai.com/v1") == "https://api.openai.com/v1")
check("用户粘贴完整接口路径会被剥掉",
      L.normalize_base("https://api.openai.com/v1/chat/completions") == "https://api.openai.com/v1",
      L.normalize_base("https://api.openai.com/v1/chat/completions"))
check("自建网关带自定义 path 不被破坏",
      L.normalize_base("https://gw.example.com/v1") == "https://gw.example.com/v1")
check("ollama 不加 /v1",
      L.normalize_base("http://127.0.0.1:11434", "ollama") == "http://127.0.0.1:11434")
check("anthropic 自动补 /v1",
      L.normalize_base("https://api.anthropic.com", "anthropic") == "https://api.anthropic.com/v1")
check("空地址不炸", L.normalize_base("") == "")
check("ollama 端口自动识别协议", L.suggest_protocol("http://127.0.0.1:11434") == "ollama")
check("anthropic 域名自动识别", L.suggest_protocol("https://api.anthropic.com") == "anthropic")
check("deepseek 默认 openai 兼容", L.suggest_protocol("https://api.deepseek.com") == "openai")

# ---------- 2. 密钥永不外泄 ----------
check("mask_key 保留首尾不暴露全文", L.mask_key("sk-FAKE-NOT-REAL-0001") == "sk-F***0001",
      L.mask_key("sk-FAKE-NOT-REAL-0001"))
check("mask_key 短密钥全掩", L.mask_key("abc") == "***")
p = L.Provider(cfg())
check("describe() 不含密钥原文", L.mask_key("sk-FAKE-NOT-REAL-0001") in p.describe()
      and "sk-FAKE-NOT-REAL-0001" not in p.describe(), p.describe())
check("未配置时 describe 说明用 pi", "pi" in L.Provider({}).describe())

# ---------- 3. 模型列表（三种协议）----------
check("openai 列表解析+去重+排序", p.list_models() == ["mock-a", "mock-b"], str(p.list_models()))
po = L.Provider(cfg(llm_protocol="ollama", llm_base_url=URL, llm_api_key=""))
check("ollama 列表走 /api/tags", po.list_models() == ["llama3:8b", "qwen2.5-coder"],
      str(po.list_models()))
pa = L.Provider(cfg(llm_protocol="anthropic", llm_base_url=URL))
check("anthropic 列表走 /v1/models", pa.list_models() == ["mock-a", "mock-b"])
def hdr(call, name):
    for k, v in call["headers"].items():
        if k.lower() == name.lower():
            return v
    return None


check("列表请求带 Bearer 头",
      any(hdr(c, "authorization") == "Bearer sk-FAKE-NOT-REAL-0001"
          for c in Mock.calls if c["path"] == "/v1/models"), str(Mock.calls[-1]["headers"]))

def _err(prov, msgs, needle):
    try:
        prov.chat(msgs)
        return False
    except Exception as e:
        return needle in str(e)


def expect(err_cls, model, needle, label):
    try:
        L.Provider(cfg(llm_model=model)).chat([{"role": "user", "content": "U"}])
        check(label, False, "没抛异常")
    except Exception as e:
        check(label, isinstance(e, err_cls) and needle in str(e), f"{type(e).__name__}: {e}")


# ---------- 4. 对话请求形态 ----------
r = p.chat([{"role": "system", "content": "S"}, {"role": "user", "content": "U"}])
last = Mock.calls[-1]
check("openai 走 /chat/completions", last["path"] == "/v1/chat/completions", last["path"])
check("openai 请求含 model/messages/max_tokens",
      last["body"]["model"] == "mock-a" and len(last["body"]["messages"]) == 2
      and last["body"]["max_tokens"] == 4000, str(last["body"])[:200])
check("返回正文可用", "结论与放行建议" in r.text, r.text[:80])
check("finish_reason 被捕获", r.finish_reason == "stop", r.finish_reason)
check("未截断", not r.truncated)

pa2 = L.Provider(cfg(llm_protocol="anthropic"))
pa2.chat([{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}])
last = Mock.calls[-1]
check("anthropic 走 /v1/messages", last["path"] == "/v1/messages", last["path"])
check("anthropic system 抽到顶层", last["body"].get("system") == "SYS", str(last["body"])[:200])
check("anthropic messages 不含 system",
      all(m["role"] != "system" for m in last["body"]["messages"]))
check("anthropic 带 x-api-key 头", hdr(last, "x-api-key") == "sk-FAKE-NOT-REAL-0001",
      str(last["headers"])[:200])
check("anthropic 带版本号", hdr(last, "anthropic-version") == "2023-06-01",
      str(last["headers"])[:200])
check("anthropic 必须有 max_tokens", "max_tokens" in last["body"])
check("anthropic 正文从 content 块拼接", "结论与放行建议" in pa2.chat(
    [{"role": "user", "content": "U"}]).text)

po2 = L.Provider(cfg(llm_protocol="ollama", llm_api_key=""))
po2.chat([{"role": "user", "content": "U"}])
last = Mock.calls[-1]
check("ollama 走 /api/chat", last["path"] == "/api/chat", last["path"])
check("ollama 非流式", last["body"].get("stream") is False)
check("ollama 无 api_key 也能调用", "结论与放行建议" in po2.chat(
    [{"role": "user", "content": "U"}]).text)
check("ollama 请求不带 Authorization", hdr(Mock.calls[-1], "authorization") is None,
      str(Mock.calls[-1]["headers"])[:150])

L.Provider(cfg(llm_thinking="high")).chat([{"role": "user", "content": "U"}])
check("思考强度映射为 reasoning_effort",
      Mock.calls[-1]["body"].get("reasoning_effort") == "high",
      str(Mock.calls[-1]["body"]))
L.Provider(cfg(llm_thinking="off")).chat([{"role": "user", "content": "U"}])
check("thinking=off 不发 reasoning_effort",
      "reasoning_effort" not in Mock.calls[-1]["body"], str(Mock.calls[-1]["body"]))
L.Provider(cfg(llm_thinking="off")).chat([{"role": "user", "content": "U"}])
check("ollama thinking=off 不发 think", Mock.calls[-1]["body"].get("think") is not True)
ab2 = L.Provider(cfg(llm_protocol="anthropic", llm_thinking="medium"))
ab2.chat([{"role": "user", "content": "U"}])
check("anthropic thinking.budget_tokens 生效",
      Mock.calls[-1]["body"].get("thinking", {}).get("budget_tokens") == 6000,
      str(Mock.calls[-1]["body"]))
eb = L.Provider(cfg(llm_extra_body={"vendor_flag": True}))
eb.chat([{"role": "user", "content": "U"}])
check("extra_body 透传服务商自定义字段",
      Mock.calls[-1]["body"].get("vendor_flag") is True)
hb = L.Provider(cfg(llm_headers={"X-Tenant": "t1"}))
hb.chat([{"role": "user", "content": "U"}])
check("自定义请求头生效", Mock.calls[-1]["headers"].get("X-Tenant") == "t1")
check("未指定模型时报错友好",
      _err(L.Provider(cfg(llm_model="")), [{"role": "user", "content": "x"}], "未指定 llm_model"),
      "")

# ---------- 5. 错误翻译与繁忙判定 ----------
expect(L.LLMError, "err-401", "API Key", "401 翻译成「API Key 无效」")
expect(L.ProviderBusy, "err-503", "繁忙", "503 归类为 ProviderBusy")
expect(L.ProviderBusy, "err-429", "限流", "429 归类为 ProviderBusy（触发退避）")
expect(L.LLMError, "err-404", "模型", "404 提示模型名不对")
expect(L.LLMError, "err-400ctx", "上下文", "上下文超限给出调小分批的建议")
expect(L.LLMError, "empty", "没有返回内容", "空返回也算失败（不能写空报告）")
rr = L.Provider(cfg(llm_model="reasoning")).chat([{"role": "user", "content": "U"}])
check("推理型模型只回 reasoning_content 时也能取到正文", "结论与放行建议" in rr.text)
tr = L.Provider(cfg(llm_model="trunc")).chat([{"role": "user", "content": "U"}])
check("finish_reason=length 被识别为截断", tr.truncated, tr.finish_reason)
bad = L.Provider(cfg(llm_base_url="http://127.0.0.1:1", llm_timeout_sec=5))
try:
    bad.list_models()
    check("连不上时给出可操作提示", False, "没抛异常")
except Exception as e:
    check("连不上时提示检查 base_url/端口/网络", "连不上" in str(e), str(e))
bad2 = L.Provider(cfg(llm_base_url="http://127.0.0.1:1", llm_timeout_sec=5))
check("非 HTTP scheme 地址也会被规范化而不是崩", "http://" in bad2.base_url, bad2.base_url)

# ---------- 6. 连通性自检 ----------
t = L.Provider(cfg()).test()
check("test() 报告模型数量", "2 个模型" in t["message"], t["message"])
check("test() 会真的试一次对话", "对话正常" in t["message"], t["message"])
check("test() 不泄露密钥原文", "sk-FAKE-NOT-REAL-0001" not in json.dumps(t), str(t)[:200])
t2 = L.Provider(cfg(llm_model="not-in-list")).test()
check("test() 提醒所选模型不在列表里", "不在返回的模型列表里" in t2["message"], t2["message"])
t3 = L.Provider(cfg(llm_api_key="")).test()
check("test() 提醒缺 api_key", "api_key" in t3["message"], t3["message"])

# ---------- 7. bot 侧：正文渲染 / 分批 / 去围栏 ----------
tmp = BASE / "_t_llm_pkg"
shutil.rmtree(tmp, ignore_errors=True)
(tmp / "app").mkdir(parents=True)
(tmp / "app" / "main.py").write_text(
    "import os\n\nAPI_KEY = 'sk-FAKE-123'\n\n\ndef f():\n    return 1\n", encoding="utf-8")
(tmp / "app" / "util.py").write_text("\n".join(f"x = {i}" for i in range(300)), encoding="utf-8")
files = [tmp / "app" / "main.py", tmp / "app" / "util.py"]
payload = B.file_payload(files, tmp, 0)
check("正文给出文件路径", "### FILE: app/main.py" in payload, payload[:100])
check("正文给出行号（模型不用猜行号）", "3 | API_KEY = 'sk-FAKE-123'" in payload, payload[:200])
trunc_payload = B.file_payload(files, tmp, 500)
check("超长文件按上限截断并说明", "仅给出前" in trunc_payload and "文件过大" in trunc_payload,
      trunc_payload[-200:])
check("_unwrap_fences 剥掉整份围栏",
      B._unwrap_fences("```markdown\n## A\n内容\n```") == "## A\n内容",
      repr(B._unwrap_fences("```markdown\n## A\n内容\n```")))
check("不剥正常内容里的代码块", "```python" in B._unwrap_fences("## A\n```python\nx=1\n```"))

# 分批：文件数与字符数双上限
big = []
for i in range(10):
    fp = tmp / "app" / f"b{i}.py"
    fp.write_text("x\n" * 1000, encoding="utf-8")     # ~2KB
    big.append(fp)
b1 = B.plan_batches(big, 4, 10 ** 9)
check("按文件数分批", [len(b) for b in b1] == [4, 4, 2], str([len(b) for b in b1]))
b2 = B.plan_batches(big, 100, 3000)                    # 每文件 ~2KB -> 每批 1 个
check("按字符上限分批（文件数没超也会分）", all(len(b) <= 2 for b in b2) and len(b2) >= 5,
      str([len(b) for b in b2]))
b3 = B.plan_batches(big, 0, 0)
check("上限为 0 时用默认值不炸", len(b3) >= 1 and sum(len(b) for b in b3) == 10)

# ---------- 8. 端到端：HTTP 后端真能出一份报告 ----------
old_cfg = dict(B.CONFIG)
B.CONFIG.clear()
B.CONFIG.update(cfg(review_chunk_files=40, max_chars_per_chunk=120000,
                    max_file_chars=30000, fallback_model=""))
rpt = tmp / "report"
Mock.calls.clear()
md, fix = B._review_via_http("testmid", tmp, rpt, files, "", [])
check("HTTP 后端生成了报告文件", md is not None and md.exists() and md.stat().st_size > 300,
      str(md))
check("prompt 里带上了代码正文", any("### FILE: app/main.py" in str(c["body"])
                                    for c in Mock.calls), "")
check("system 提示词包含安全规则", any("不可信" in str(c["body"]) for c in Mock.calls), "")
check("报告通过结构校验（无需修复重跑）",
      B.validate_report(md.read_text(encoding="utf-8"), len(files)) == [],
      str(B.validate_report(md.read_text(encoding="utf-8"), len(files))))
check("未调用 pi（完全不依赖 pi CLI）", all(c["path"].startswith("/v1/") for c in Mock.calls),
      str({c["path"] for c in Mock.calls}))

# 截断 -> 续写一次
Mock.calls.clear()
B.CONFIG["llm_model"] = "trunc"
md2, _ = B._review_via_http("testmid2", tmp, tmp / "report2", files, "", [])
check("被截断时自动续写（同一批调了 2 次）", len(Mock.calls) >= 2, str(len(Mock.calls)))
check("续写后报告仍在", md2 is not None and md2.exists())

# 上游繁忙 -> 自动切备用模型
Mock.calls.clear()
B.CONFIG["llm_model"] = "err-503"
B.CONFIG["fallback_model"] = "mock-a"
warns = []
md3, _ = B._review_via_http("testmid3", tmp, tmp / "report3", files, "", warns)
check("上游繁忙自动切换 fallback_model", md3 is not None and md3.exists(), str(warns))
check("切换动作记录进注意事项", any("自动切换" in w for w in warns), str(warns))

# 上游繁忙且无备用 -> 抛错（交给退避重试）
B.CONFIG["fallback_model"] = ""
try:
    B._review_via_http("testmid4", tmp, tmp / "report4", files, "", [])
    check("无备用模型时明确报「上游繁忙」", False, "没抛异常")
except Exception as e:
    check("无备用模型时明确报「上游繁忙」", "上游模型繁忙" in str(e), str(e)[:160])

check("llm_backend 按配置选择", B.llm_backend() == "http")
B.CONFIG["llm_base_url"] = ""
B.CONFIG["llm_backend"] = "auto"
check("auto + 没配 base_url 时自动回落到 pi 后端", B.llm_backend() == "pi", B.llm_backend())
B.CONFIG["llm_backend"] = "http"
check("显式 http 且没 base_url 时不会假装 pi", B.llm_backend() == "http", B.llm_backend())
st = B.provider_status()
check("provider_status 不含密钥原文", "sk-FAKE-NOT-REAL-0001" not in json.dumps(st))

# ---------- 9. 配置校验 / 保存语义 ----------
B.CONFIG.clear()
B.CONFIG.update(old_cfg)
B.CONFIG["llm_base_url"] = ""
B.CONFIG["llm_api_key"] = "sk-FAKE-NOT-REAL-0003"
check("http 后端缺 base_url 被拦住",
      "Base URL" in B.validate_provider("http", None, None, "m", ""),
      B.validate_provider("http", None, None, "m", ""))
check("http 后端缺模型被拦住", "模型" in B.validate_provider("http", "https://x.com", None, "", ""))
check("base_url 缺协议被拦住", "http" in B.validate_provider("http", "x.com", None, "m", ""))
check("非法协议被拦住", "协议" in B.validate_provider("http", "https://x.com", "grpc", "m", ""))
check("非法 thinking 被拦住", "thinking" in B.validate_provider(None, None, None, None, "ultra"))
check("合法配置通过", B.validate_provider("http", "https://x.com", "openai", "m", "high") == "",
      B.validate_provider("http", "https://x.com", "openai", "m", "high"))
err = B.apply_provider({"llm_api_key": "", "api_key": "", "model": ""}, save=False)
check("apply_provider 允许空操作", err == "", err)
check("api_key 留空不会擦掉旧密钥", B.CONFIG["llm_api_key"] == "sk-FAKE-NOT-REAL-0003",
      B.CONFIG["llm_api_key"])
err = B.apply_provider({"base_url": "https://api.deepseek.com", "api_key": "sk-FAKE-NOT-REAL-0004",
                        "model": "deepseek-chat", "thinking": "high",
                        "max_output_tokens": "12000", "backend": "http"}, save=False)
check("填全了就保存成功", err == "", err)
check("自动识别协议为 openai", B.CONFIG["llm_protocol"] == "openai")
check("模型写入 llm_model（不动 pi_model）",
      B.CONFIG["llm_model"] == "deepseek-chat")
check("max_output_tokens 转 int", B.CONFIG["llm_max_output_tokens"] == 12000)
check("非法整数直接报错不写坏配置",
      "整数" in B.apply_provider({"max_output_tokens": "abc"}, save=False))
err = B.apply_provider({"clear_key": "1"}, save=False)
check("显式 clear_key 才会清空密钥", B.CONFIG["llm_api_key"] == "", B.CONFIG["llm_api_key"])
B.CONFIG["llm_api_key"] = "sk-FAKE-NOT-REAL-0005"
check("redact 会把自家密钥也掩掉", "sk-FAKE-NOT-REAL-0005" not in B.redact(
    "key=sk-FAKE-NOT-REAL-0005 出现在代码里"), B.redact("key=sk-FAKE-NOT-REAL-0005"))
B.CONFIG.clear()
B.CONFIG.update(old_cfg)

srv.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
