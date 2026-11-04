#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可操作控制台 UI —— 本地 127.0.0.1，零第三方依赖（stdlib http.server）。

用法:
    python ui.py                 # 默认 http://127.0.0.1:8770
    python ui.py --port 8801
    python bot.py ui             # 等价入口

安全设计:
    - 只绑定 127.0.0.1，拒绝非本机 Host（防 DNS rebinding）
    - CSRF token（每次启动随机生成，所有写操作校验）
    - 邮件主题 / 代码 / 报告等外部输入一律 HTML 转义；报告预览走本地
      md2html 渲染（会转义原始 HTML），不直接吐被审代码里的 <script>
    - 报告文件下载走 attachment，HTML 不以 text/html 直接内联执行
"""
import argparse
import html
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import bot as B          # noqa: E402
import md2html           # noqa: E402

CSI = secrets.token_urlsafe(18)          # CSRF token
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
STATE_ORDER = ["SEEN", "DOWNLOADED", "REVIEWED", "AWAITING_APPROVAL", "REPLIED",
               "REJECTED", "FAILED", "IGNORED"]
STATE_COLOR = {
    "SEEN": "#8e8e93", "DOWNLOADED": "#0a84ff", "REVIEWED": "#5e5ce6",
    "AWAITING_APPROVAL": "#ff9f0a", "REPLIED": "#30d158", "REJECTED": "#ff6482",
    "FAILED": "#ff453a", "IGNORED": "#636366",
}
REPORT_DIRNAME = "report"
MAX_LOG_TAIL = 20000


# ---------------------------------------------------------------- 数据读取
def tasks() -> list:
    ts = B.all_tasks()
    ts.sort(key=lambda t: t.get("created_at_ts", 0), reverse=True)
    return ts


def counts() -> dict:
    c = {}
    for t in tasks():
        s = t.get("state", "?")
        c[s] = c.get(s, 0) + 1
    return c


def daemon_pids() -> dict:
    try:
        return json.loads((B.STATE / "bot.pids").read_text(encoding="utf-8"))
    except Exception:
        return {}


def daemon_alive() -> bool:
    pids = daemon_pids()
    if not pids:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pids.get('bot')}", "/NH"],
                                 capture_output=True, text=True, errors="replace",
                                 timeout=10, creationflags=B._no_window_flags())
            return str(pids.get("bot")) in (out.stdout or "")
        os.kill(int(pids.get("bot")), 0)
        return True
    except Exception:
        return False


def safe_report_dir(mid: str):
    """只允许访问 work/<mid>/report 下的文件"""
    if not mid or not mid.startswith("msg_"):
        return None
    d = (B.WORK / mid / REPORT_DIRNAME).resolve()
    try:
        if B.WORK.resolve() not in d.parents:
            return None
    except Exception:
        return None
    return d if d.is_dir() else None


# ---------------------------------------------------------------- 页面骨架
CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e8ee;--dim:#8b93a7;--acc:#4c8dff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.65 -apple-system,"Segoe UI",
 "PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 60px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
 border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:22px}
h1{font-size:19px;margin:0;letter-spacing:.3px}
.sub{color:var(--dim);font-size:12.5px}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
 font-weight:600;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:18px 0 26px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat .n{font-size:24px;font-weight:700}
.stat .l{color:var(--dim);font-size:12px;margin-top:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:18px 20px;margin-bottom:18px}
.card h2{font-size:15px;margin:0 0 14px;color:var(--fg)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:12px}
tr:hover td{background:#1c2029}
.mono{font-family:Consolas,"Courier New",monospace;font-size:12px;color:var(--dim)}
.btn{display:inline-block;padding:5px 11px;border-radius:7px;border:1px solid var(--line);
 background:#20242e;color:var(--fg);cursor:pointer;font-size:12.5px;text-decoration:none}
.btn:hover{background:#2a2f3b;text-decoration:none}
.btn.primary{background:#1f4fa8;border-color:#2c62c9;color:#fff}
.btn.danger{background:#4a1f22;border-color:#732a2f;color:#ff9a9a}
.btn.sm{padding:3px 8px;font-size:12px}
.btn[disabled]{opacity:.5;cursor:default}
input[type=password],input[type=number]{background:#12151b;border:1px solid var(--line);
 color:var(--fg);border-radius:7px;padding:7px 9px;font:13px/1.5 inherit;width:100%}
.out{margin-top:12px;font-size:12.5px;line-height:1.7;white-space:pre-wrap;
 border-left:3px solid var(--line);padding:8px 12px;background:#12151b;border-radius:0 8px 8px 0}
.out.bad{border-left-color:#ff453a;color:#ff9d9d}
.out.ok{border-left-color:#30d158;color:#8ff0b1}
.hint{color:var(--dim);font-size:12px;line-height:1.6}
.step{margin:16px 0 8px;font-size:13.5px;color:var(--fg);display:flex;align-items:center;gap:8px}
.stepno{display:inline-flex;width:20px;height:20px;border-radius:50%;background:#1f4fa8;
 color:#fff;align-items:center;justify-content:center;font-size:12px}
.adv{margin-top:12px;border:1px dashed var(--line);border-radius:8px;padding:8px 12px}
.adv summary{cursor:pointer;color:var(--dim);font-size:12.5px}
input[type=password]{font-family:Consolas,monospace}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input[type=text],textarea,select{background:#12151b;border:1px solid var(--line);color:var(--fg);
 border-radius:7px;padding:7px 9px;font:13px/1.5 inherit;width:100%}
textarea{min-height:74px;resize:vertical;font-family:Consolas,monospace;font-size:12.5px}
label{color:var(--dim);font-size:12px;display:block;margin-bottom:4px}
pre{background:#0b0d12;border:1px solid var(--line);border-radius:8px;padding:13px 15px;
 overflow:auto;font-size:12.5px;line-height:1.55;max-height:460px}
.flash{border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:13px}
.flash.ok{background:#12301c;border:1px solid #1e5e35;color:#8ff0b1}
.flash.err{background:#3a1416;border:1px solid #7a2228;color:#ff9d9d}
.kv{display:grid;grid-template-columns:110px 1fr;gap:4px 14px;font-size:13px}
.kv .k{color:var(--dim)}
.md{max-width:100%;overflow-wrap:anywhere}
.md h1,.md h2,.md h3{line-height:1.35}
.md h1{font-size:20px;border-bottom:2px solid var(--acc);padding-bottom:8px}
.md h2{font-size:16px;margin-top:24px;border-left:4px solid var(--acc);padding-left:10px}
.md table{font-size:12.5px}
.md td,.md th{border:1px solid var(--line)}
.md code{background:#20242e;padding:1px 5px;border-radius:4px}
.md pre{max-height:none}
footer{color:var(--dim);font-size:12px;margin-top:34px;border-top:1px solid var(--line);padding-top:14px}
"""

# 模型服务面板用的前端脚本（单文件、零依赖，不拉外部 JS）
PROV_FIELDS = ["backend", "base_url", "protocol", "api_key", "model", "thinking",
               "fallback", "max_output_tokens", "max_chars_per_chunk", "review_chunk_files"]
PROV_JS = """
var PROV_FIELDS = ["backend","base_url","protocol","api_key","model","thinking",
                   "fallback","max_output_tokens","max_chars_per_chunk","review_chunk_files"];
function provParams(){
  var f = document.getElementById('provform');
  var p = new URLSearchParams();
  p.set('csrf', CSRF);
  PROV_FIELDS.forEach(function(k){ var el = f.elements[k]; if (el && el.value !== '') p.set(k, el.value); });
  return p;
}
function provOut(msg, cls){
  var o = document.getElementById('provout');
  o.className = 'out ' + (cls || '');
  o.textContent = msg;
}
async function provRun(url, btn){
  var old = btn.textContent;
  btn.disabled = true; btn.textContent = '处理中…';
  provOut('请稍候…');
  try {
    var r = await fetch(url, {method:'POST', body: provParams()});
    var j = await r.json();
    if (j.error) { provOut(j.error, 'bad'); }
    else if (j.models) {
      var sel = document.getElementById('modelpick');
      var cur = document.getElementById('model').value;
      sel.innerHTML = '';
      var o0 = document.createElement('option'); o0.value=''; o0.textContent='（从列表选模型…）';
      sel.appendChild(o0);
      j.models.forEach(function(m){
        var o = document.createElement('option'); o.value=m; o.textContent=m;
        if (m === cur) o.selected = true;
        sel.appendChild(o);
      });
      var box = document.getElementById('pickbox'); if (box) box.style.display = 'block';
      sel.style.display = 'block';
      provOut(j.message || ('共 ' + j.models.length + ' 个模型，选一个再点保存'));
    } else {
      provOut(j.message || '完成', j.ok ? 'ok' : 'bad');
    }
  } catch (e) { provOut('请求失败：' + e, 'bad'); }
  btn.disabled = false; btn.textContent = old;
}
document.addEventListener('DOMContentLoaded', function(){
  var m = document.getElementById('modelpick');
  if (m) m.addEventListener('change', function(){ if (m.value) document.getElementById('model').value = m.value; });
});
"""


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


# 自动刷新：只要页面上有表单正在输入/被修改，就暂停刷新（否则会把输到一半的内容冲掉）
REFRESH_JS = """
var INIT_VALUES = new Map();
function rememberInit(){
  document.querySelectorAll('form input, form textarea, form select').forEach(function(el){
    INIT_VALUES.set(el, el.value);
  });
}
function formBusy(){
  var a = document.activeElement;
  if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.closest && a.closest('form')) return true;
  var els = document.querySelectorAll('form input, form textarea, form select');
  for (var i = 0; i < els.length; i++) {
    if (els[i].type === 'hidden') continue;
    if (els[i].value !== INIT_VALUES.get(els[i])) return true;
  }
  return false;
}
function showPaused(on){
  var p = document.getElementById('paused');
  if (p) p.style.display = on ? 'inline' : 'none';
}
function refreshTick(){
  if (!REFRESH_SEC) return;
  if (formBusy()) { showPaused(true); setTimeout(refreshTick, 4000); return; }
  showPaused(false);
  location.reload();
}
document.addEventListener('DOMContentLoaded', function(){
  rememberInit();
  if (REFRESH_SEC) setTimeout(refreshTick, REFRESH_SEC * 1000);
});
"""


def layout(title: str, body: str, refresh: int = 0, flash: str = "", flash_err: bool = False) -> str:
    meta = ""            # 不再用 <meta refresh>：它会打断表单输入，改用下面可暂停的 JS

    f = ""
    if flash:
        f = f'<div class="flash {"err" if flash_err else "ok"}">{esc(flash)}</div>'
    alive = daemon_alive()
    dot = "#30d158" if alive else "#ff453a"
    pids = daemon_pids()
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}
<title>{esc(title)} · 代码审核机器人</title><style>{CSS}</style></head><body><div class="wrap">
<header>
  <h1>代码审核机器人</h1>
  <span class="badge" style="background:{dot}">{"运行中" if alive else "已停止"}</span>
  <span class="sub">{esc(B.CONFIG.get("mailbox"))} · PID {esc(pids.get("bot") or "-")}</span>
  <span style="flex:1"></span>
  <a class="btn" href="/">控制台</a>
  <a class="btn" href="/model">模型配置</a>
  <a class="btn" href="/api/status">JSON</a>
</header>
{f}
{body}
<footer>本地控制台 · 仅监听 127.0.0.1 · 模型 {esc(B.effective_model_desc())}
  <span id="paused" style="display:none"> · 正在输入，自动刷新已暂停</span></footer>
</div>
<script>const CSRF = "{CSI}";var REFRESH_SEC = {int(refresh or 0)};</script>
<script>{REFRESH_JS}</script>
<script>{PROV_JS}</script>
</body></html>"""


def badge(state: str) -> str:
    return (f'<span class="badge" style="background:{STATE_COLOR.get(state, "#555")}">'
            f'{esc(state)}</span>')


def _to_local(val):
    """把各种时间表示统一解析成**本地时间** datetime。

    支持：
      - Unix 时间戳（int/float，如 1793677985）
      - ISO 字符串带 `Z`（UTC）：2026-11-03T03:53:03Z
      - ISO 字符串带偏移：2026-11-03T11:53:05+08:00
      - 无时区的 ISO 字符串：按本地时间处理
    解析失败返回 None。
    """
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val)   # fromtimestamp 默认就是本地
        except Exception:
            return None
    s = str(val).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"          # fromisoformat 不认 Z（旧版 Python）
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt                      # 无时区 -> 已是本地
    return dt.astimezone()            # 有时区 -> 转本地


def fmt_time(val) -> str:
    """统一显示为本地时间（邮箱的 UTC 字段 / 机器人的本地字段 / 时间戳都一样）"""
    dt = _to_local(val)
    if dt is not None:
        return esc(dt.strftime("%Y-%m-%d %H:%M:%S"))
    # 兜底：无法解析就原样截断，至少不炸
    return esc((str(val) if val else "").replace("T", " ")[:19])


# ---------------------------------------------------------------- 页面：控制台
def card_provider(src: str = "/") -> str:
    """模型接入卡片：填 Base URL + API Key + 模型 就能跑，不依赖任何本地 CLI"""
    st = B.provider_status()
    cfg = B.CONFIG
    levels = ("", "off", "minimal", "low", "medium", "high", "xhigh", "max")
    backends = (("auto", "auto（填了 Base URL 就直连，否则用 pi）"),
                ("http", "http：直连模型服务（推荐）"),
                ("pi", "pi：调用本机 pi CLI"))
    protos = (("", "自动识别（推荐）"),
              ("openai", "openai 兼容（/v1/chat/completions）"),
              ("anthropic", "anthropic（/v1/messages）"),
              ("ollama", "ollama 本地（/api/chat）"))
    cur_think = st["thinking"] if st["backend"] == "http" else (cfg.get("pi_thinking") or "")
    cur_model = st["model"] if st["backend"] == "http" else (cfg.get("pi_model") or "")
    key_state = (f"已保存 {st['key_masked']}（留空 = 不改动）" if st["has_key"]
                 else ("本地 ollama 可留空" if st["protocol"] == "ollama" else "尚未设置（必填）"))

    def opts(pairs, cur):
        return "".join(f'<option value="{esc(v)}"'
                       f'{" selected" if v == cur else ""}>{esc(lbl)}</option>'
                       for v, lbl in pairs)

    return f"""
<div class="card">
  <h2>模型接入（三步就能跑）</h2>
  <div class="hint">给 <b>Base URL + API Key + 模型名</b> 就能审核，不依赖本机装了什么 CLI。
    支持 DeepSeek / OpenAI / Moonshot / 智谱 / OpenRouter / vLLM·LiteLLM·OneAPI 自建网关 / 本机 Ollama。</div>

  <form method="post" action="/action" id="provform">
    <input type="hidden" name="csrf" value="{CSI}">
    <input type="hidden" name="action" value="provider">
    <input type="hidden" name="from" value="{esc(src)}">

    <div class="step"><span class="stepno">1</span><b>模型服务地址</b></div>
    <div class="row">
      <div style="width:260px"><label>后端</label>
        <select name="backend">{opts(backends, st["backend"])}</select></div>
      <div style="flex:2;min-width:300px"><label>Base URL（会自动补 /v1，粘完整接口路径也能识别）</label>
        <input type="text" name="base_url" value="{esc(cfg.get('llm_base_url') or '')}"
               placeholder="https://api.deepseek.com 或 http://127.0.0.1:11434"></div>
      <div style="width:280px"><label>协议</label>
        <select name="protocol">{opts(protos, cfg.get("llm_protocol") or "")}</select></div>
    </div>

    <div class="step"><span class="stepno">2</span><b>API Key</b>
      <span class="sub">（{esc(key_state)}；当前协议：{esc(st['protocol'])}）</span></div>
    <div class="row">
      <div style="flex:2;min-width:300px"><label>API Key</label>
        <input type="password" name="api_key" value="" autocomplete="new-password"
               placeholder="sk-... （留空表示保持已保存的密钥不变）"></div>
      <div style="width:170px"><label>清空已保存的 Key</label>
        <select name="clear_key"><option value="">否</option><option value="1">是，清空</option></select></div>
    </div>
    <div class="hint">密钥只写进 <code>config.json</code>（已在 <code>.gitignore</code> 里，不会进仓库）；
      不想落盘可用环境变量 <code>CODE_REVIEW_BOT_API_KEY</code>。页面和日志里只会看到掩码。</div>

    <div class="step"><span class="stepno">3</span><b>选模型</b>
      <span class="sub">（不确定有哪些？先点下面的「获取模型列表」，再从下拉里选）</span></div>
    <div class="row">
      <div style="flex:2;min-width:260px"><label>模型名（手填）</label>
        <input type="text" id="model" name="model" value="{esc(cur_model)}"
               placeholder="deepseek-chat / gpt-4o-mini / qwen2.5-coder:7b …"></div>
      <div style="flex:2;min-width:260px;display:none" id="pickbox"><label>从服务拉到的模型（选它会自动填到上面）</label>
        <select id="modelpick"></select></div>
      <div style="width:150px"><label>思考强度</label>
        <select name="thinking">{opts([(l, l or "默认") for l in levels], cur_think)}</select></div>
      <div style="flex:1;min-width:180px"><label>备用模型（上游繁忙自动切）</label>
        <input type="text" name="fallback" value="{esc(st['fallback_model'])}" placeholder="可留空"></div>
    </div>

    <details class="adv"><summary>高级参数（一般不用改）</summary>
      <div class="row" style="margin-top:10px">
        <div style="width:170px"><label>单次输出上限 tokens</label>
          <input type="number" name="max_output_tokens" min="512" max="200000"
                 value="{esc(cfg.get('llm_max_output_tokens') or 8000)}"></div>
        <div style="width:170px"><label>分批文件数</label>
          <input type="number" name="review_chunk_files" min="0" max="500"
                 value="{esc(cfg.get('review_chunk_files'))}"></div>
        <div style="width:190px"><label>分批字符上限</label>
          <input type="number" name="max_chars_per_chunk" min="10000" max="2000000"
                 value="{esc(cfg.get('max_chars_per_chunk') or 120000)}"></div>
      </div>
    </details>

    <div class="row" style="margin-top:14px">
      <button class="btn primary" type="submit">保存</button>
      <button class="btn" type="button"
              onclick="provRun('/api/provider/models', this)">① 获取模型列表</button>
      <button class="btn" type="button"
              onclick="provRun('/api/provider/test', this)">② 测试连通性</button>
      <span class="hint">这两个按钮不用先保存，直接用你当前填的值</span>
    </div>
  </form>
  <div class="out" id="provout"></div>
  <div class="sub" style="margin-top:10px">当前生效：{esc(B.effective_model_desc())}</div>
  <div class="hint">命令行等价：<code>python bot.py model --base-url … --api-key … --set …</code>
    · <code>python bot.py model --list</code> · <code>python bot.py model --test</code></div>
</div>
"""


def page_model(flash: str = "", err: bool = False) -> str:
    """模型配置独立页面：不自动刷新，慢慢填不会被冲掉"""
    st = B.provider_status()
    cur = f"""
<div class="card">
  <h2>当前生效</h2>
  <div class="kv">
    <div class="k">后端</div><div>{esc(st['backend'])}
      {'（未填 Base URL 时自动用 pi CLI）' if st['backend'] == 'pi' else ''}</div>
    <div class="k">协议</div><div>{esc(st['protocol'])}</div>
    <div class="k">Base URL</div><div>{esc(st['base_url'] or '（未配置）')}</div>
    <div class="k">API Key</div><div>{esc(st['key_masked'] or '（未设置）')}</div>
    <div class="k">模型</div><div>{esc(st['model'] or st['pi_model'] or '（未指定）')}</div>
    <div class="k">思考强度</div><div>{esc(st['thinking'] or st['pi_thinking'] or '默认')}</div>
    <div class="k">备用模型</div><div>{esc(st['fallback_model'] or '（不启用）')}</div>
    <div class="k">单次输出上限</div><div>{esc(st['max_output_tokens'])} tokens</div>
  </div>
  <div class="hint" style="margin-top:10px">本页<b>不会自动刷新</b>；控制台首页的自动刷新在你
    正在输入时也会自动暂停。</div>
</div>
"""
    return layout("模型配置", card_provider("/model") + cur, refresh=0,
                    flash=flash, flash_err=err)



def page_dashboard(flash: str = "", err: bool = False) -> str:
    ts = tasks()
    c = counts()
    stats = "".join(
        f'<div class="stat"><div class="n" style="color:{STATE_COLOR.get(s, "#fff")}">'
        f'{c.get(s, 0)}</div><div class="l">{s}</div></div>'
        for s in STATE_ORDER if c.get(s)
    ) or '<div class="stat"><div class="n">0</div><div class="l">暂无任务</div></div>'
    stats += (f'<div class="stat"><div class="n">{len(ts)}</div>'
              f'<div class="l">任务总数</div></div>')

    rows = []
    for t in ts[:80]:
        mid = t.get("message_id", "")
        st = t.get("state", "?")
        acts = [f'<a class="btn sm" href="/task/{esc(mid)}">详情</a>']
        if st in ("FAILED", "SEEN", "DOWNLOADED", "REVIEWED", "IGNORED", "REPLIED"):
            acts.append(
                f'<form method="post" action="/action" style="display:inline" '
                f'onsubmit="return confirm(&quot;重跑会重新审核并可能再次发送回复邮件，确定？&quot;)">'
                f'<input type="hidden" name="csrf" value="{CSI}">'
                f'<input type="hidden" name="action" value="retry">'
                f'<input type="hidden" name="id" value="{esc(mid)}">'
                f'<button class="btn sm" type="submit">重跑</button></form>')
        rows.append(f"""<tr>
<td><a href="/task/{esc(mid)}">{esc((t.get('subject') or '')[:70])}</a>
  <div class="mono">{esc(mid)}</div></td>
<td>{badge(st)}</td>
<td class="mono">{esc(t.get('attempts', 0))}</td>
<td class="mono">{fmt_time(t.get('created_at'))}</td>
<td class="mono">{esc((t.get('last_error') or '')[:80])}</td>
<td><div class="row">{''.join(acts)}</div></td></tr>""")

    table = ("<table><thead><tr><th>主题 / ID</th><th>状态</th><th>尝试</th>"
             "<th>时间</th><th>最后错误</th><th>操作</th></tr></thead><tbody>"
             + ("".join(rows) or '<tr><td colspan="6" style="color:#8b93a7">暂无任务</td></tr>')
             + "</tbody></table>")

    controls = f"""
<div class="card">
  <h2>守护进程</h2>
  <div class="row">
    {form_btn("start", "启动")}
    {form_btn("restart", "重启")}
    {form_btn("scan", "立即扫描收件箱", cls="primary")}
    {form_btn("stop", "停止", cls="danger", confirm="确定停止守护进程？")}
  </div>
</div>

<div class="card">
  <h2>全局审核要求（对所有任务生效）</h2>
  <form method="post" action="/action">
    <input type="hidden" name="csrf" value="{CSI}">
    <input type="hidden" name="action" value="global_req">
    <textarea name="value" placeholder="1. 所有对外接口必须有鉴权&#10;2. 金额一律整数分">{esc(B.CONFIG.get("review_requirements") or "")}</textarea>
    <div class="row" style="margin-top:10px"><button class="btn primary" type="submit">保存</button>
      <span class="sub">每封邮件里的【审核要求】区块优先级更高</span></div>
  </form>
</div>
"""
    # 待审批区（有就置顶高亮）
    pending = [t for t in ts if t.get("state") == "AWAITING_APPROVAL"]
    approval_section = ""
    if pending:
        prow = []
        for t in pending:
            mid = t.get("message_id", "")
            has_report = bool(t.get("report") and (B.WORK / mid / "report").exists())
            prow.append(f"""<tr>
<td><a href="/task/{esc(mid)}">{esc((t.get('subject') or '')[:60])}</a>
  <div class="mono">{esc(mid)} · {fmt_time(t.get('awaiting_since'))}</div></td>
<td>{'✅ 报告已生成' if has_report else '…'}</td>
<td><div class="row">
  {form_btn("approve", "✅ 通过并发送", cls="primary", mid=mid,
             confirm="审批通过后将把完整审核报告发送给发件人，确定？")}
  {form_btn("reject", "✖ 驳回", cls="danger", mid=mid, confirm="驳回后不会发送，确定？")}
  <a class="btn sm" href="/task/{esc(mid)}">看报告</a>
</div></td></tr>""")
        approval_section = f"""
<div class="card" style="border:1px solid #ff9f0a">
  <h2 style="color:#ff9f0a">⏳ 待审批（{len(pending)}）——审批通过后才会给发件人回信</h2>
  <table><thead><tr><th>主题 / ID</th><th>报告</th><th>操作</th></tr></thead><tbody>
  {''.join(prow) or '<tr><td colspan=3>无</td></tr>'}
  </tbody></table>
  <div class="hint">逐条审批更安全（可先「看报告」再决定）；想批量放行可在任务详情页操作。</div>
</div>"""

    # 审批/确认信 设置卡
    ra = bool(B.CONFIG.get("require_approval", False))
    ack_on = bool(B.CONFIG.get("ack_enabled", True))
    ack_timeout = B.CONFIG.get("ack_timeout_sec", 300)
    approval_cfg = f"""
<div class="card">
  <h2>审批闸 &amp; 处理中确认信</h2>
  <form method="post" action="/action">
    <input type="hidden" name="csrf" value="{CSI}">
    <input type="hidden" name="action" value="approval_cfg">
    <div class="row" style="align-items:flex-start">
      <label style="margin:0 8px 0 0"><input type="checkbox" name="require_approval" {"checked" if ra else ""}> 开启人工审批</label>
      <span class="hint">开启后审核完成会停在「待审批」，<b>不会自动回信</b>，你在页面点「通过」才发送</span>
    </div>
    <div style="border-top:1px solid var(--line);margin:12px 0"></div>
    <div class="row" style="align-items:flex-start">
      <label style="margin:0 8px 0 0"><input type="checkbox" name="ack_enabled" {"checked" if ack_on else ""}> 超时未出结果时先发一封「已收到、处理中」</label>
      <span class="hint">让发件人知道任务没丢、卡在哪、大概多久；每个任务最多发一封</span>
    </div>
    <div class="row" style="margin-top:10px">
      <div style="width:180px"><label>确认信超时（秒）</label>
        <input type="number" name="ack_timeout_sec" min="60" max="86400" value="{esc(ack_timeout)}"></div>
      <span class="hint">任务创建超过此时长仍无结果即发确认信</span>
    </div>
    <div class="row" style="margin-top:12px"><button class="btn primary" type="submit">保存</button>
      <span class="hint">保存后守护进程自动生效，无需重启</span></div>
  </form>
</div>"""

    body = (f'<div class="grid">{stats}</div>{approval_section}{controls}{approval_cfg}{card_provider()}'
            f'<div class="card"><h2>任务列表</h2>{table}</div>')
    return layout("控制台", body, refresh=20, flash=flash, flash_err=err)


def form_btn(action: str, label: str, cls: str = "", confirm: str = "", mid: str = "") -> str:
    onc = f' onsubmit="return confirm(\'{confirm}\')"' if confirm else ""
    c = f"btn {cls}".strip()
    mid_field = f'<input type="hidden" name="id" value="{esc(mid)}">' if mid else ""
    return (f'<form method="post" action="/action" style="display:inline"{onc}>'
            f'<input type="hidden" name="csrf" value="{CSI}">'
            f'<input type="hidden" name="action" value="{action}">'
            f'{mid_field}'
            f'<button class="{c}" type="submit">{esc(label)}</button></form>')


# ---------------------------------------------------------------- 页面：任务详情
def page_task(mid: str, flash: str = "", err: bool = False) -> str:
    t = B.load_task(mid)
    if not t:
        return layout("任务不存在", '<div class="card">任务不存在或已被清理</div>', flash="任务不存在", flash_err=True)

    rdir = safe_report_dir(mid)
    files = []
    if rdir:
        for f in sorted(rdir.iterdir()):
            if f.is_file():
                files.append(
                    f'<tr><td>{esc(f.name)}</td><td class="mono">{f.stat().st_size:,} B</td>'
                    f'<td><a class="btn sm" href="/report/{esc(mid)}?f={urllib.parse.quote(f.name)}">预览</a> '
                    f'<a class="btn sm" href="/download/{esc(mid)}?f={urllib.parse.quote(f.name)}">下载</a></td></tr>')

    reqs = t.get("requirements_effective") or []
    req_html = "".join(
        f'<div style="margin:8px 0;padding:9px 12px;background:#12151b;border-radius:7px">'
        f'<div class="sub">{esc(r.get("source"))}</div>'
        f'<pre style="margin:6px 0 0;max-height:180px">{esc(r.get("text"))}</pre></div>'
        for r in reqs) or '<div class="sub">无额外要求</div>'

    warns = t.get("report_warnings") or []
    warn_html = ("".join(f'<div class="flash err">{esc(w)}</div>' for w in warns)) if warns else ""

    pi_log = B.LOGS / f"{mid}.pi.log"
    log_txt = ""
    if pi_log.exists():
        log_txt = pi_log.read_text(encoding="utf-8", errors="replace")[-MAX_LOG_TAIL:]

    st = (t.get("state") or "").upper()
    approval_panel = ""
    if st == "AWAITING_APPROVAL":
        has_report = bool(t.get("report") and (B.WORK / mid / "report").exists())
        report_link = (f'<a class="btn" href="/report/{esc(mid)}?f='
                       f'{urllib.parse.quote("代码审核报告.md")}">预览报告</a>') if has_report else ""
        approval_panel = f"""
<div class="card" style="border:1px solid #ff9f0a">
  <h2 style="color:#ff9f0a">⏳ 待审批——通过后才给发件人回信</h2>
  <div class="hint">审核已完成。建议先「预览报告」核对内容，再决定通过或驳回。</div>
        <div class="row" style="margin-top:12px">
          {form_btn("approve", "✅ 审批通过并发送报告", "primary", mid=mid,
                     confirm="审批通过后将把完整审核报告发送给发件人，确定？")}
          {form_btn("reject", "✖ 驳回（不发送）", "danger", mid=mid,
                     confirm="驳回后不会发送回复，确定？")}
          {report_link}
        </div>
  <form method="post" action="/action" style="margin-top:10px">
    <input type="hidden" name="csrf" value="{CSI}">
    <input type="hidden" name="action" value="reject_reason">
    <input type="hidden" name="id" value="{esc(mid)}">
    <div class="row">
      <input type="text" name="reason" placeholder="驳回原因（可选，仅记录，不发给发件人）" style="flex:1;min-width:200px">
      <button class="btn danger" type="submit">保存驳回原因</button>
    </div>
  </form>
</div>"""
    elif st == "REJECTED":
        approval_panel = f"""
<div class="card" style="border:1px solid #ff6482">
  <h2 style="color:#ff6482">✖ 已驳回（未发送）</h2>
  <div class="kv">
    <div class="k">驳回时间</div><div>{fmt_time(t.get('rejected_at'))}</div>
    <div class="k">驳回原因</div><div>{esc(t.get('reject_reason') or '（未填写）')}</div>
  </div>
  <div class="row" style="margin-top:12px">
    {form_btn("retry", "重新审核", "primary", mid=mid,
               confirm="重新审核会重新走一遍流程，确定？")}
  </div>
</div>"""
    elif st == "REPLIED" and t.get("approved_at"):
        approval_panel = f"""
<div class="card" style="border:1px solid #30d158">
  <h2 style="color:#30d158">✅ 已审批通过并发送</h2>
  <div class="kv">
    <div class="k">审批时间</div><div>{fmt_time(t.get('approved_at'))}</div>
    <div class="k">审批人</div><div>{esc(t.get('approved_by') or '（未记录）')}</div>
    <div class="k">发送时间</div><div>{fmt_time(t.get('replied_at'))}</div>
  </div>
</div>"""

    ack_line = ""
    if t.get("ack_sent_at"):
        ack_line = (f'<div class="k">处理中确认信</div>'
                    f'<div>已于 {fmt_time(t.get("ack_sent_at"))} 发送</div>')

    body = f"""
{approval_panel}
{warn_html}
<div class="card">
  <h2>{esc(t.get('subject'))}</h2>
  <div class="kv">
    <div class="k">状态</div><div>{badge(t.get('state'))}</div>
    <div class="k">发件人</div><div>{esc(t.get('from_name'))} &lt;{esc(t.get('from'))}&gt;</div>
    <div class="k">时间</div><div>{fmt_time(t.get('created_at'))}</div>
    <div class="k">尝试次数</div><div>{esc(t.get('attempts', 0))}</div>
    <div class="k">已回复</div><div>{fmt_time(t.get('replied_at'))}</div>
    {ack_line}
    <div class="k">最后错误</div><div class="mono">{esc(t.get('last_error') or '-')}</div>
    <div class="k">message_id</div><div class="mono">{esc(mid)}</div>
  </div>
  <div class="row" style="margin-top:14px">
    {form_btn("retry", "重跑（会重新审核并发送回复）", "primary", mid=mid,
              confirm="重跑会重新审核并可能再次发送回复邮件，确定？")}
    {form_btn("process", "立即处理（后台）", "", "", mid=mid)}
    <a class="btn" href="/">返回</a>
  </div>
</div>

<div class="card">
  <h2>本次生效的审核要求</h2>
  {req_html}
  <form method="post" action="/action" style="margin-top:12px">
    <input type="hidden" name="csrf" value="{CSI}">
    <input type="hidden" name="action" value="task_req">
    <input type="hidden" name="id" value="{esc(mid)}">
    <label>为这个任务追加要求（保存后需重跑才生效）</label>
    <textarea name="value">{esc(t.get("requirements") or "")}</textarea>
    <div class="row" style="margin-top:8px"><button class="btn primary" type="submit">保存</button></div>
  </form>
</div>

<div class="card">
  <h2>报告文件</h2>
  <table><thead><tr><th>文件</th><th>大小</th><th>操作</th></tr></thead><tbody>
  {''.join(files) or '<tr><td colspan="3" class="sub">暂无报告</td></tr>'}
  </tbody></table>
</div>

<div class="card">
  <h2>AI 审核日志（尾部）</h2>
  <pre>{esc(log_txt) or "无日志"}</pre>
</div>
"""
    return layout(f"任务 {mid[-8:]}", body, flash=flash, flash_err=err)


def page_report(mid: str, fname: str) -> str:
    d = safe_report_dir(mid)
    if not d:
        return layout("不存在", '<div class="card">报告不存在</div>', flash="报告不存在", flash_err=True)
    p = (d / fname).resolve()
    if d not in p.parents or not p.is_file():
        return layout("非法路径", '<div class="card">非法文件路径</div>', flash="非法文件路径", flash_err=True)
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() in (".md", ".markdown"):
        rendered = md2html.md_to_html(raw, fname, "#4c8dff", "100%")
        inner = rendered.split("<body>", 1)[-1].rsplit("</body>", 1)[0]
        inner = inner.replace('<div class="wrap">', '<div class="md">', 1)
    else:
        inner = f'<pre>{esc(raw[:200000])}</pre>'
    body = (f'<div class="card"><div class="row" style="margin-bottom:12px">'
            f'<a class="btn" href="/task/{esc(mid)}">返回任务</a>'
            f'<a class="btn" href="/download/{esc(mid)}?f={urllib.parse.quote(fname)}">下载</a>'
            f'<span class="sub">{esc(fname)}</span></div>{inner}</div>')
    return layout(fname, body)


# ---------------------------------------------------------------- 动作
def run_bot_cli(*args, timeout=120):
    cmd = [sys.executable, str(BASE / "bot.py")] + [str(a) for a in args]
    p = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       creationflags=B._no_window_flags(), startupinfo=B._hidden_startupinfo())
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def reload_config() -> None:
    """修改配置后重新读盘，让本进程内存里的 CONFIG 与文件一致

    model/全局要求等动作是调 bot.py 子进程完成的，子进程只改了文件；
    不重载就会出现「已经保存了，但页面/API 还是旧值」。
    """
    try:
        fresh = B._load_config()
        B.CONFIG.clear()
        B.CONFIG.update(fresh)
        B.KEYWORD = fresh.get("subject_keyword", B.KEYWORD)
    except Exception:
        pass


def spawn_detached(*args):
    cmd = [sys.executable, str(BASE / "bot.py")] + [str(a) for a in args]
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
    lf = (B.LOGS / "ui_spawn.log").open("a", encoding="utf-8")
    subprocess.Popen(cmd, cwd=str(BASE), stdout=lf, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, creationflags=flags)


def form_flat(form: dict) -> dict:
    """parse_qs 的 {k: [v]} 拉成 {k: v}"""
    return {k: v[0] for k, v in form.items() if v}


def provider_cfg_from_form(form: dict) -> dict:
    """用表单里（可能还没保存的）值临时搭一份 provider 配置，用于“先测再说”"""
    cfg = dict(B.CONFIG)
    f = form_flat(form)
    base = (f.get("base_url") or "").strip()
    proto = (f.get("protocol") or "").strip()
    if base:
        cfg["llm_base_url"] = base
        cfg["llm_protocol"] = proto or (B.LLM.suggest_protocol(base) if B.LLM else "openai")
    elif proto:
        cfg["llm_protocol"] = proto
    key = (f.get("api_key") or "").strip()
    if key:
        cfg["llm_api_key"] = key
    if f.get("clear_key"):
        cfg["llm_api_key"] = ""
    if f.get("model"):
        cfg["llm_model"] = f["model"].strip()
    if f.get("thinking"):
        cfg["llm_thinking"] = f["thinking"].strip()
    if f.get("max_output_tokens"):
        try:
            cfg["llm_max_output_tokens"] = int(f["max_output_tokens"])
        except ValueError:
            pass
    cfg["llm_backend"] = "http"
    return cfg


def api_provider(op: str, form: dict) -> dict:
    if B.LLM is None:
        return {"error": "lib/llm.py 缺失"}
    cfg = provider_cfg_from_form(form)
    if not (cfg.get("llm_base_url") or "").strip():
        return {"error": "先填 Base URL（例如 https://api.deepseek.com）"}
    p = B.LLM.Provider(cfg)
    if op == "models":
        try:
            ms = p.list_models()
        except Exception as e:
            return {"error": f"获取模型列表失败：{e}\n当前配置：{p.describe()}"}
        if not ms:
            return {"error": "该服务没返回模型列表（有些自建网关不提供 /models，直接手填模型名）"}
        return {"models": ms,
                "message": f"{p.base_url} 返回 {len(ms)} 个模型；选中后点「保存」"}
    if op == "test":
        r = p.test()
        return {"ok": r["ok"], "message": r["message"], "base_url": r["base_url"],
                "model": r["model"], "count": len(r["models"])}
    return {"error": f"未知操作: {op}"}


def do_action(form: dict) -> tuple:
    """返回 (flash_message, is_error, redirect)"""
    a = (form.get("action") or [""])[0]
    mid = (form.get("id") or [""])[0]
    # 需要任务 id 的动作：id 为空就直接报错，别重定向到空 /task/ 页面
    if a in ("retry", "process", "task_req", "approve", "reject", "reject_reason") and not mid:
        return ("操作缺少任务 ID（按钮未带上任务，请刷新后重试）", True, "/")

    if a in ("start", "stop", "restart", "scan"):
        rc, out = run_bot_cli(a, timeout=180)
        return (f"{a} 执行完成", False, "/") if rc == 0 else (f"{a} 失败: {out[:300]}", True, "/")

    if a == "retry":
        rc, out = run_bot_cli("retry", "--id", mid)
        return (f"已重置 {mid[-8:]}，worker 会重新处理", False, f"/task/{mid}") if rc == 0 \
            else (f"重跑失败: {out[:300]}", True, f"/task/{mid}")

    if a == "process":
        spawn_detached("process", "--id", mid)
        return (f"已后台启动处理 {mid[-8:]}（约 3~5 分钟）", False, f"/task/{mid}")

    if a == "task_req":
        val = (form.get("value") or [""])[0]
        rc, out = run_bot_cli("req", "--id", mid, val if val else "-")
        return (f"已保存任务要求 {mid[-8:]}", False, f"/task/{mid}") if rc == 0 \
            else (f"保存失败: {out[:300]}", True, f"/task/{mid}")

    if a == "approve":
        try:
            B.approve_task(mid, approver="ui")
            return ("✅ 审批通过，报告已发送给发件人", False, f"/task/{mid}")
        except Exception as e:
            return (f"审批失败：{e}", True, f"/task/{mid}")

    if a == "reject":
        reason = (form.get("reason") or [""])[0]
        try:
            B.reject_task(mid, reason)
            return ("✖ 已驳回（未发送）", False, f"/task/{mid}")
        except Exception as e:
            return (f"驳回失败：{e}", True, f"/task/{mid}")

    if a == "reject_reason":
        reason = (form.get("reason") or [""])[0]
        t = B.load_task(mid)
        if t and t.get("state") in ("AWAITING_APPROVAL", "REJECTED"):
            if reason:
                t["reject_reason"] = reason[:1000]
                B.save_task(t)
            return ("已保存驳回原因", False, f"/task/{mid}")
        return (f"无法保存驳回原因（当前状态 {t.get('state') if t else '不存在'}）", True, f"/task/{mid}")

    if a == "approval_cfg":
        ra = (form.get("require_approval") or [""])[0] == "on"
        ack_on = (form.get("ack_enabled") or [""])[0] == "on"
        timeout_s = (form.get("ack_timeout_sec") or [""])[0].strip()
        B.CONFIG["require_approval"] = ra
        B.CONFIG["ack_enabled"] = ack_on
        if timeout_s:
            try:
                B.CONFIG["ack_timeout_sec"] = int(timeout_s)
            except ValueError:
                return ("确认信超时必须是整数", True, "/")
        B._save_config()
        B.reload_config()
        reload_config()
        return ("审批 / 确认信设置已保存", False, "/")

    if a in ("provider", "model"):
        err = B.apply_provider(form_flat(form))
        if err:
            return (f"配置未保存：{err}", True, "/")
        B.reload_config()
        reload_config()
        to = (form.get("from") or [""])[0]
        if to not in ("/", "/model"):      # 白名单，不接受任意外部跳转
            to = "/"
        return (f"模型配置已保存：{B.effective_model_desc()}", False, to)

    if a == "global_req":
        val = (form.get("value") or [""])[0]
        B.CONFIG["review_requirements"] = val
        B._save_config()
        reload_config()
        return ("全局审核要求已保存", False, "/")

    return (f"未知操作: {a}", True, "/")


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ReviewBotUI/1.0"

    def log_message(self, fmt, *args):        # 静音默认 access log
        pass

    # --- 基础 ---
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in {"127.0.0.1", "localhost", "::1", ""}

    def _send(self, code: int, body: str, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str, flash: str = "", err: bool = False):
        q = urllib.parse.urlencode({"m": flash, "e": "1" if err else ""})
        self.send_response(303)
        self.send_header("Location", f"{to}?{q}")
        self.end_headers()

    def _flash_from_query(self, qs: dict):
        return (qs.get("m", [""])[0], qs.get("e", [""])[0] == "1")

    # --- GET ---
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden host", "text/plain; charset=utf-8")
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        qs = urllib.parse.parse_qs(u.query)

        try:
            if path in ("/", "/index.html"):
                flash, err = self._flash_from_query(qs)
                return self._send(200, page_dashboard(flash, err))
            if path in ("/model", "/model.html"):
                flash, err = self._flash_from_query(qs)
                return self._send(200, page_model(flash, err))
            if path == "/api/status":
                return self._send(200, json.dumps({
                    "daemon_alive": daemon_alive(),
                    "pids": daemon_pids(),
                    "config_source": B.CONFIG.get("_source"),
                    "model": B.effective_model_desc(),
                    "provider": B.provider_status(),
                    "counts": counts(),
                    "tasks": [
                        {k: t.get(k) for k in
                         ("message_id", "subject", "state", "attempts", "created_at",
                          "replied_at", "last_error")}
                        for t in tasks()
                    ],
                }, ensure_ascii=False, indent=2), "application/json; charset=utf-8")
            if path.startswith("/task/"):
                mid = path.split("/task/", 1)[1]
                flash, err = self._flash_from_query(qs)
                return self._send(200, page_task(mid, flash, err))
            if path.startswith("/report/"):
                mid = path.split("/report/", 1)[1]
                fname = qs.get("f", [""])[0]
                return self._send(200, page_report(mid, fname))
            if path.startswith("/download/"):
                mid = path.split("/download/", 1)[1]
                fname = qs.get("f", [""])[0]
                d = safe_report_dir(mid)
                if not d:
                    return self._send(404, "not found", "text/plain; charset=utf-8")
                p = (d / fname).resolve()
                if d not in p.parents or not p.is_file():
                    return self._send(403, "forbidden", "text/plain; charset=utf-8")
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{urllib.parse.quote(p.name)}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._send(404, layout("404", '<div class="card">页面不存在</div>'), )
        except Exception as e:
            return self._send(500, layout("错误", f'<div class="card">内部错误：{esc(e)}</div>'))

    # --- POST ---
    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "forbidden host", "text/plain; charset=utf-8")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self._send(413, "payload too large", "text/plain; charset=utf-8")
        raw = self.rfile.read(n).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)

        if (form.get("csrf") or [""])[0] != CSI:
            return self._send(403, layout("CSRF", '<div class="card">CSRF 校验失败，请刷新页面重试</div>',
                                          flash="CSRF 校验失败", flash_err=True))
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path.startswith("/api/provider/"):
            op = path.split("/api/provider/", 1)[1]
            try:
                out = api_provider(op, form)
            except Exception as e:
                out = {"error": f"执行异常：{e}"}
            return self._send(200, json.dumps(out, ensure_ascii=False),
                              "application/json; charset=utf-8")
        try:
            msg, is_err, to = do_action(form)
        except Exception as e:
            msg, is_err, to = f"执行异常：{e}", True, "/"
        return self._redirect(to, msg, is_err)


def port_free(port: int) -> bool:
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="代码审核机器人 · 可操作控制台")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args()

    port = a.port
    for cand in range(port, port + 10):
        if port_free(cand):
            port = cand
            break
    else:
        print(f"端口 {port}~{port + 9} 都被占用", file=sys.stderr)
        return 2

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"控制台已启动: {url}", flush=True)
    print(f"邮箱: {B.CONFIG.get('mailbox')} · 模型: {B.effective_model_desc()}", flush=True)
    print("Ctrl+C 停止", flush=True)
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
