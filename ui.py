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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import bot as B          # noqa: E402
import md2html           # noqa: E402

CSI = secrets.token_urlsafe(18)          # CSRF token
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
STATE_ORDER = ["SEEN", "DOWNLOADED", "REVIEWED", "REPLIED", "FAILED", "IGNORED"]
STATE_COLOR = {
    "SEEN": "#8e8e93", "DOWNLOADED": "#0a84ff", "REVIEWED": "#5e5ce6",
    "REPLIED": "#30d158", "FAILED": "#ff453a", "IGNORED": "#636366",
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


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def layout(title: str, body: str, refresh: int = 0, flash: str = "", flash_err: bool = False) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
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
  <a class="btn" href="/api/status">JSON</a>
</header>
{f}
{body}
<footer>本地控制台 · 仅监听 127.0.0.1 · 模型 {esc(B.effective_model_desc())}</footer>
</div></body></html>"""


def badge(state: str) -> str:
    return (f'<span class="badge" style="background:{STATE_COLOR.get(state, "#555")}">'
            f'{esc(state)}</span>')


def fmt_time(iso: str) -> str:
    return esc((iso or "").replace("T", " ")[:19])


# ---------------------------------------------------------------- 页面：控制台
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
  <h2>审核模型</h2>
  <form method="post" action="/action" class="row" style="align-items:flex-end">
    <input type="hidden" name="csrf" value="{CSI}">
    <input type="hidden" name="action" value="model">
    <div style="flex:1;min-width:260px"><label>模型（留空 = 用 pi 默认）</label>
      <input type="text" name="model" value="{esc(B.CONFIG.get('pi_model') or '')}"
             placeholder="deepseek/deepseek-v4-pro"></div>
    <div style="width:150px"><label>thinking</label>
      <select name="thinking">{"".join(
        f'<option value="{lvl}"{" selected" if (B.CONFIG.get("pi_thinking") or "") == lvl else ""}>'
        f'{lvl or "（默认）"}</option>'
        for lvl in ("", "off", "minimal", "low", "medium", "high", "xhigh", "max"))}</select></div>
    <button class="btn primary" type="submit">保存</button>
  </form>
  <div class="sub" style="margin-top:10px">当前生效：{esc(B.effective_model_desc())}</div>
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
    body = f'<div class="grid">{stats}</div>{controls}<div class="card"><h2>任务列表</h2>{table}</div>'
    return layout("控制台", body, refresh=20, flash=flash, flash_err=err)


def form_btn(action: str, label: str, cls: str = "", confirm: str = "") -> str:
    onc = f' onsubmit="return confirm(\'{confirm}\')"' if confirm else ""
    c = f"btn {cls}".strip()
    return (f'<form method="post" action="/action" style="display:inline"{onc}>'
            f'<input type="hidden" name="csrf" value="{CSI}">'
            f'<input type="hidden" name="action" value="{action}">'
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

    body = f"""
{warn_html}
<div class="card">
  <h2>{esc(t.get('subject'))}</h2>
  <div class="kv">
    <div class="k">状态</div><div>{badge(t.get('state'))}</div>
    <div class="k">发件人</div><div>{esc(t.get('from_name'))} &lt;{esc(t.get('from'))}&gt;</div>
    <div class="k">时间</div><div>{fmt_time(t.get('created_at'))}</div>
    <div class="k">尝试次数</div><div>{esc(t.get('attempts', 0))}</div>
    <div class="k">已回复</div><div>{fmt_time(t.get('replied_at'))}</div>
    <div class="k">最后错误</div><div class="mono">{esc(t.get('last_error') or '-')}</div>
    <div class="k">message_id</div><div class="mono">{esc(mid)}</div>
  </div>
  <div class="row" style="margin-top:14px">
    {form_btn("retry", "重跑（会重新审核并发送回复）", "primary",
              "重跑会重新审核并可能再次发送回复邮件，确定？")}
    {form_btn("process", "立即处理（后台）", "", "")}
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


def do_action(form: dict) -> tuple:
    """返回 (flash_message, is_error, redirect)"""
    a = (form.get("action") or [""])[0]
    mid = (form.get("id") or [""])[0]

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

    if a == "model":
        model = (form.get("model") or [""])[0].strip()
        thinking = (form.get("thinking") or [""])[0].strip()
        rc, out = run_bot_cli("model", "--set", model, "--thinking", thinking)
        reload_config()
        return ("模型配置已更新", False, "/") if rc == 0 else (f"更新失败: {out[:300]}", True, "/")

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
            if path == "/api/status":
                return self._send(200, json.dumps({
                    "daemon_alive": daemon_alive(),
                    "pids": daemon_pids(),
                    "config_source": B.CONFIG.get("_source"),
                    "model": B.effective_model_desc(),
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
