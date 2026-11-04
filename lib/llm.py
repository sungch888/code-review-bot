"""通用 LLM Provider 客户端（只用 Python 标准库）

为什么要有这个文件：
    审核能力不应该绑死某个 CLI / 某个 SDK / 某台开发机。只要给我一个
    base_url + api_key + model 就能跑：DeepSeek、OpenAI、Moonshot、智谱、
    OpenRouter、vLLM / LiteLLM / OneAPI 自建网关、本机 Ollama 都可以。

支持三种协议（大多数服务都是 OpenAI 兼容的）：
    openai    : {base}/v1/chat/completions      （DeepSeek/OpenAI/Moonshot/OpenRouter/vLLM…）
    anthropic : {base}/v1/messages
    ollama    : {base}/api/chat

配置（config.json）：
    "llm_backend":   "auto" | "http" | "pi"
    "llm_protocol":  "openai" | "anthropic" | "ollama"
    "llm_base_url":  "https://api.deepseek.com"          （会自动补 /v1）
    "llm_api_key":   "sk-..."                            （留空可用环境变量）
    "llm_model":     "deepseek-chat"
    "llm_thinking":  "" | off | minimal | low | medium | high | xhigh | max
    "llm_temperature": 0.2
    "llm_max_output_tokens": 8000
    "llm_timeout_sec": 600
    "llm_extra_body": {"thinking": {"type": "enabled"}}   （透传给服务商的任意字段）
    "llm_headers":   {"X-foo": "bar"}                      （自定义请求头）

密钥也可不写进配置文件：环境变量 CODE_REVIEW_BOT_API_KEY。
"""
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 600
DEFAULT_MAX_OUTPUT_TOKENS = 8000
ENV_KEY = "CODE_REVIEW_BOT_API_KEY"

PROTOCOLS = ("openai", "anthropic", "ollama")
THINKING_LEVELS = ("", "off", "minimal", "low", "medium", "high", "xhigh", "max")

# 思考强度 -> 各家服务的参数写法
_THINKING_EFFORT = {"off": None, "minimal": "minimal", "low": "low",
                    "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
_THINKING_BUDGET = {"minimal": 1024, "low": 2048, "medium": 6000,
                    "high": 12000, "xhigh": 20000, "max": 32000}


class LLMError(RuntimeError):
    """模型调用失败（已翻译成中文提示）"""

    def __init__(self, msg: str, status: int = 0, raw: str = ""):
        super().__init__(msg)
        self.status = status
        self.raw = raw


class ProviderBusy(LLMError):
    """服务商侧临时问题（过载 / 限流 / 超时 / 连接被掐）——重试或换模型可解决"""


def mask_key(key: str) -> str:
    """密钥只可能以这种形式出现在日志/UI 里"""
    k = key or ""
    if not k:
        return ""
    if len(k) <= 8:
        return "***"
    return k[:4] + "***" + k[-4:]


def suggest_protocol(url: str) -> str:
    u = (url or "").lower()
    if "anthropic.com" in u:
        return "anthropic"
    if "ollama" in u or ":11434" in u:
        return "ollama"
    return "openai"


def normalize_base(url: str, protocol: str = "openai") -> str:
    """把用户粘贴的各种写法收敛成可直接拼接的 base

    接受： https://api.deepseek.com / api.deepseek.com/v1 /
           https://api.openai.com/v1/chat/completions / localhost:11434
    """
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    u = u.rstrip("/")
    for suffix in ("/chat/completions", "/v1/messages", "/api/chat", "/api/tags",
                   "/completions", "/responses"):
        if u.lower().endswith(suffix):
            u = u[: -len(suffix)].rstrip("/")
            break
    path = urllib.parse.urlsplit(u).path.lower()
    if protocol == "ollama":
        return u
    if path.endswith("/v1") or "/v1/" in path:
        return u
    return u + "/v1"


def friendly_error(status: int, body: str) -> str:
    """把 HTTP 错误翻译成「你该怎么办」"""
    b = (body or "")[:600]
    low = b.lower()
    if status in (401,):
        return "API Key 无效或未提供（401）。请检查 config.json 里的 llm_api_key"
    if status == 403:
        return ("API Key 无权访问该模型或该 base_url（403）。"
                "请确认账号已开通该模型、额度未用完")
    if status == 404:
        return ("找不到该模型或接口路径不对（404）。请核对 llm_model 是否与"
                "「获取模型列表」里的名字一致，以及 llm_base_url 是否正确")
    if status == 422 or status == 400:
        hint = ""
        if "model" in low and ("not found" in low or "invalid" in low or "does not exist" in low):
            hint = "（多半是 llm_model 名字不对，可先点「获取模型列表」）"
        elif "api key" in low or "key" in low:
            hint = "（多半是 llm_api_key 有问题）"
        elif "context length" in low or "too long" in low or "maximum context" in low:
            hint = "（prompt 超出模型上下文，可调小 review_chunk_files / max_chars_per_chunk）"
        return f"请求被拒绝（{status}）{hint}：{b}"
    if status == 429:
        return "触发限流（429），稍后会自动退避重试"
    if status in (500, 502, 503, 504, 520, 521, 522, 524, 529):
        return f"服务商不可用/繁忙（{status}），稍后自动退避重试：{b[:200]}"
    return f"服务商返回 HTTP {status}：{b}"


def is_busy_text(text: str) -> bool:
    s = (text or "").lower()
    return any(k in s for k in (
        "service_unavailable", "service is too busy", "too busy", "overloaded",
        "rate_limit", "rate limit", "too many requests", "429", "503", "502", "529",
        "no available provider", "server_overloaded",
        "stream ended without finish_reason", "incomplete chunked read",
        "connection reset", "socket hang up", "connection aborted",
        "timed out", "timeout", "eof occurred", "premature close",
    ))


class ChatResult:
    def __init__(self, text: str, finish_reason: str = "", model: str = "",
                 usage: dict = None, elapsed: float = 0.0):
        self.text = text
        self.finish_reason = finish_reason
        self.model = model
        self.usage = usage or {}
        self.elapsed = elapsed

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


class Provider:
    def __init__(self, cfg: dict = None):
        cfg = cfg or {}
        self.protocol = (cfg.get("llm_protocol") or "openai").strip().lower()
        if self.protocol not in PROTOCOLS:
            self.protocol = "openai"
        self.base_url = normalize_base(cfg.get("llm_base_url") or "", self.protocol)
        self.base_url_raw = (cfg.get("llm_base_url") or "").strip()
        self.api_key = ((cfg.get("llm_api_key") or "").strip()
                        or os.environ.get(ENV_KEY, "").strip())
        self.model = (cfg.get("llm_model") or "").strip()
        self.thinking = (cfg.get("llm_thinking") or "").strip().lower()
        self.temperature = cfg.get("llm_temperature", 0.2)
        self.max_output_tokens = int(cfg.get("llm_max_output_tokens")
                                     or DEFAULT_MAX_OUTPUT_TOKENS)
        self.timeout = int(cfg.get("llm_timeout_sec") or DEFAULT_TIMEOUT)
        self.extra_body = cfg.get("llm_extra_body") or {}
        self.headers = cfg.get("llm_headers") or {}

    # ---------- 基本状态 ----------
    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def describe(self) -> str:
        """给日志/UI 看的描述，绝不含密钥原文"""
        if not self.configured:
            return "（未配置 llm_base_url，使用 pi 后端）"
        bits = [f"{self.protocol}://{self.base_url}",
                f"model={self.model or '（未指定）'}",
                f"key={mask_key(self.api_key) or '未设置'}"]
        if self.thinking:
            bits.append(f"thinking={self.thinking}")
        return " ".join(bits)

    # ---------- HTTP ----------
    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": "code-review-bot/1.0"}
        if self.protocol == "anthropic":
            h["x-api-key"] = self.api_key
            h["anthropic-version"] = "2023-06-01"
        elif self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        h.update(self.headers or {})
        return h

    def _request(self, method: str, path: str, payload: dict = None,
                 timeout: int = None) -> dict:
        if not self.configured:
            raise LLMError("未配置 llm_base_url，无法调用模型")
        url = self._url(path)
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers())
        tmo = timeout or self.timeout
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=tmo, context=ctx) as r:
                raw = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            msg = friendly_error(e.code, body)
            if e.code in (429, 500, 502, 503, 504, 520, 521, 522, 524, 529) or is_busy_text(body):
                raise ProviderBusy(msg, status=e.code, raw=body)
            raise LLMError(msg, status=e.code, raw=body)
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))
            msg = f"连不上 {url}：{reason}（检查 base_url、端口、网络/代理）"
            if is_busy_text(reason):
                raise ProviderBusy(msg, raw=reason)
            raise LLMError(msg, raw=reason)
        except (socket.timeout, TimeoutError):
            raise ProviderBusy(f"请求 {url} 超时({tmo}s)——上游生成太慢或过载")
        except ssl.SSLError as e:
            raise LLMError(f"TLS 握手失败：{e}（自签证书/代理问题）", raw=str(e))
        try:
            return json.loads(raw)
        except Exception:
            raise LLMError(f"{url} 返回的不是 JSON：{raw[:300]}", raw=raw)

    # ---------- 模型列表 ----------
    def list_models(self) -> list:
        if self.protocol == "ollama":
            d = self._request("GET", "/api/tags", timeout=30)
            items = d.get("models") or []
            out = [i.get("name") or i.get("model") for i in items if isinstance(i, dict)]
        else:
            d = self._request("GET", "/models", timeout=30)
            items = d.get("data") or d.get("models") or []
            out = []
            for i in items:
                if isinstance(i, str):
                    out.append(i)
                elif isinstance(i, dict):
                    out.append(i.get("id") or i.get("name") or i.get("model"))
        return sorted({x for x in out if x})

    # ---------- 对话 ----------
    def _build_payload(self, messages: list, model: str, max_tokens: int,
                       temperature) -> dict:
        m = model or self.model
        if not m:
            raise LLMError("未指定 llm_model（可先点「获取模型列表」选一个）")
        if self.protocol == "anthropic":
            sys_txt = "\n\n".join(m2["content"] for m2 in messages if m2.get("role") == "system")
            rest = [{"role": m2.get("role"), "content": m2.get("content")}
                    for m2 in messages if m2.get("role") != "system"]
            payload = {"model": m, "messages": rest, "max_tokens": max_tokens}
            if sys_txt:
                payload["system"] = sys_txt
            if temperature is not None:
                payload["temperature"] = temperature
            if self.thinking and self.thinking != "off":
                payload["thinking"] = {"type": "enabled",
                                       "budget_tokens": _THINKING_BUDGET.get(
                                           self.thinking, 8000)}
        elif self.protocol == "ollama":
            payload = {"model": m, "messages": messages, "stream": False}
            if temperature is not None:
                payload["temperature"] = temperature
            payload["options"] = {"num_predict": max_tokens}
            if self.thinking and self.thinking != "off":
                payload["think"] = True
        else:
            payload = {"model": m, "messages": messages}
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens:
                payload["max_tokens"] = max_tokens
            eff = _THINKING_EFFORT.get(self.thinking)
            if eff:
                payload["reasoning_effort"] = eff
        payload.update(self.extra_body or {})
        return payload

    @staticmethod
    def _extract(d: dict, protocol: str):
        """从各家返回里取出正文 + finish_reason"""
        if protocol == "ollama":
            msg = d.get("message") or {}
            return (msg.get("content") or d.get("response") or "",
                    d.get("done_reason") or "")
        if protocol == "anthropic":
            blocks = d.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            return text, (d.get("stop_reason") or "")
        choices = d.get("choices") or []
        if not choices:
            return "", ""
        c = choices[0]
        msg = c.get("message") or {}
        text = msg.get("content") or ""
        if not text:
            # 推理型模型有时把正文放在 reasoning_content
            text = msg.get("reasoning_content") or c.get("text") or ""
        return text, (c.get("finish_reason") or "")

    def chat(self, messages: list, model: str = "", max_tokens: int = 0,
             temperature=None, timeout: int = 0) -> ChatResult:
        mt = max_tokens or self.max_output_tokens
        temp = self.temperature if temperature is None else temperature
        payload = self._build_payload(messages, model, mt, temp)
        path = "/messages" if self.protocol == "anthropic" else (
            "/api/chat" if self.protocol == "ollama" else "/chat/completions")
        t0 = time.time()
        d = self._request("POST", path, payload, timeout=timeout or None)
        text, finish = self._extract(d, self.protocol)
        el = time.time() - t0
        if not (text or "").strip():
            raw = json.dumps(d, ensure_ascii=False)[:400]
            if is_busy_text(raw):
                raise ProviderBusy("模型返回空内容（上游可能过载或流被掐断）", raw=raw)
            raise LLMError(f"模型没有返回内容：{raw}", raw=raw)
        used = (model or self.model)
        return ChatResult(text.strip(), finish, d.get("model") or used,
                          d.get("usage") or {}, el)

    # ---------- 连通性自检（UI 的「测试」按钮）----------
    def test(self) -> dict:
        out = {"ok": False, "protocol": self.protocol, "base_url": self.base_url,
               "model": self.model, "key": mask_key(self.api_key),
               "models": [], "message": ""}
        if not self.configured:
            out["message"] = "还没填 base_url"
            return out
        notes = []
        if self.protocol != "ollama" and not self.api_key:
            notes.append("没填 api_key（ollama 本地服务可以留空）")
        try:
            out["models"] = self.list_models()
            notes.append(f"连接成功，发现 {len(out['models'])} 个模型")
            out["ok"] = True
        except LLMError as e:
            notes.append(f"获取模型列表失败：{e}")
            out["message"] = "；".join(notes)
            return out
        if self.model:
            if self.model not in out["models"] and out["models"]:
                notes.append(f"注意：{self.model} 不在返回的模型列表里")
            try:
                r = self.chat([{"role": "user", "content": "ping，只回复 pong"}],
                              max_tokens=64, timeout=60)
                out["ok"] = True
                notes.append(f"对话正常（{r.elapsed:.1f}s，回复：{r.text[:40]}）")
            except LLMError as e:
                out["ok"] = False
                notes.append(f"但对话失败：{e}")
        else:
            out["ok"] = False
            notes.append("还没选模型")
        out["message"] = "；".join(notes)
        return out
