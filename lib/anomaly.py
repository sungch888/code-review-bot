#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""异常回信文案与判定。

原则（踩过坑才定的）：
1. **不许臆断原因**。曾经把「附件没有 attachment_id」当成「附件超过 20MB」，
   把一个 363KB 的代码包误报成超大附件，用户收到完全错误的指引。
   现在只依据 `size` 字段判断大小，依据**实际下载结果**判断能否处理。
2. **每封异常回信都要可操作**：说清「发生了什么 / 为什么 / 你现在该做什么」，
   给出具体步骤，而不是只说"处理失败"。
"""
import time
import urllib.error
import urllib.request
from pathlib import Path

MAIL_LIMIT = 20 * 1024 * 1024          # 邮箱侧附件上限 20MB
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://mail.qq.com/",
    "Accept": "*/*",
}


def parse_size(att: dict) -> int:
    try:
        return int(att.get("size") or 0)
    except Exception:
        return 0


def human_size(n: int) -> str:
    if not n:
        return "未知大小"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def download_via_url(url: str, dest: Path, max_bytes: int = MAIL_LIMIT * 3,
                     timeout: int = 120, cookie: str = "", retries: int = 3):
    """尝试直接下载 download_url 形式的附件（超大附件直链）。

    真实踩过的坑：QQ 邮箱的超大附件直链 302 到 CDN 后，如果没带
    `MAIL5K` 会话 Cookie，CDN 会返回 `400 X-Error-Message: Need MAIL5K= in Cookie`。
    所以：
      - 支持传入 Cookie（config.json 的 download_cookie / download_cookie_file）
      - 把 CDN 的 X-Error-Message 翻成人话，不要只说「HTTP 400」
      - 只对瞬时故障重试，4xx 不重试（省一次无辜的等待）
      - 校验下载到的东西真的是压缩包（防止存下一个错误页/网关提示）

    返回 (ok: bool, path: Path|None, err: str)
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = "下载失败"
    for attempt in range(1, max(1, retries) + 1):
        ok, path, err, permanent = _download_once(url, dest, max_bytes, timeout, cookie)
        if ok:
            return True, path, ""
        last = err
        if permanent or attempt >= retries:
            break
        # 瞬时故障（5xx / 超时 / 连接重置）才退避重试
        time.sleep(min(2 * attempt, 6))
    return False, None, last


_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
_MAGIC = {
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".gz": (b"\x1f\x8b",), ".tgz": (b"\x1f\x8b",),
    ".bz2": (b"BZh",), ".xz": (b"\xfd7zXZ",),
    ".7z": (b"7z\xbc\xaf\x27\x1c",), ".rar": (b"Rar!",),
}


def cookie_header(cookie: str) -> str:
    """把用户给的 Cookie 规整成请求头：只给值就补成 MAIL5K=值"""
    c = (cookie or "").strip().strip(";")
    if not c:
        return ""
    first = c.split(";")[0]
    if "=" not in first:
        return f"MAIL5K={c}"
    return c


def needs_login(err: str) -> bool:
    """失败原因是不是「需要邮箱网页登录态」（超大附件直链的典型）"""
    s = (err or "").lower()
    return any(k in s for k in ("mail5k", "登录态", "need login", "cookie"))


def _download_once(url, dest: Path, max_bytes: int, timeout: int, cookie: str):
    """一次下载尝试 -> (ok, path, err, permanent)"""
    headers = dict(HEADERS)
    ck = cookie_header(cookie)
    if ck:
        headers["Cookie"] = ck
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                return False, None, f"链接返回的是 HTML 页面（{ctype}），不是文件", True
            total = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        f.close()
                        dest.unlink(missing_ok=True)
                        return False, None, f"文件超过下载上限 {human_size(max_bytes)}", True
                    f.write(chunk)
        if total == 0:
            dest.unlink(missing_ok=True)
            return False, None, "链接下载到 0 字节", True
        bad = _content_mismatch(dest)
        if bad:
            dest.unlink(missing_ok=True)
            return False, None, bad, True
        return True, dest, "", False
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", 0) or 0
        msg = e.headers.get("X-Error-Message") if getattr(e, "headers", None) else ""
        ecode = e.headers.get("X-Error-Code") if getattr(e, "headers", None) else ""
        detail = f"HTTP {code} {getattr(e, 'reason', '') or ''}".strip()
        if msg:
            detail += f"（{msg}）"
            if needs_login(msg):
                # 这句话会进回信正文，所以只说人话，**不提**内部配置项（那是给运维看日志的）
                detail += "——该附件是「超大附件」，取回需要邮箱网页登录态"
        elif ecode:
            detail += f"（X-Error-Code {ecode}）"
        return False, None, detail, code not in _TRANSIENT_HTTP
    except Exception as e:
        # 超时 / 连接重置 / DNS 等：算瞬时故障，可以重试
        return False, None, f"{type(e).__name__}: {e}", False


def _content_mismatch(path: Path) -> str:
    """下载到的内容跟扩展名对不上（常见：网关拦截页、登录页）"""
    ext = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except Exception:
        return ""
    if not head:
        return ""
    if ext == ".tar":
        try:
            with open(path, "rb") as f:
                f.seek(257)
                return "" if f.read(5) == b"ustar" else "下载到的内容不是 tar 包（可能被网关拦截）"
        except Exception:
            return ""
    magics = _MAGIC.get(ext)
    if not magics:
        return ""
    if any(head.startswith(m) for m in magics):
        return ""
    low = head[:64].lstrip().lower()
    if low.startswith((b"<", b"{", b"[")) or "html" in low:
        return "下载到的不是压缩包，而是错误页/登录页（可能被网关拦截）"
    return "下载到的内容不是 %s 压缩包" % (ext.lstrip("."),)


# ------------------------------------------------------------------ 文案
def reply_no_attachment(subject: str) -> str:
    return f"""你好，

《{subject}》已收到，但**没有检测到任何附件**，无法进行代码审核。

请按下面任一方式重新发送：
1. 把项目代码打包成 zip，作为**附件**发送（最推荐）；
2. 若代码量很小（< 100 行），可直接把代码贴在邮件正文里，并注明文件名；
3. 若附件较大无法走邮件，请附上可直链下载的压缩包链接。

谢谢。"""


def reply_link_only(subject: str, items) -> str:
    """附件只有 download_url、既没有 attachment_id 也取不回。

    注意：**不能**说成「超过 20MB」——那取决于 size，不是取决于有没有直链。
    """
    lines = []
    for name, size, err in items:
        shown = err or "无法直接取回"
        if needs_login(err):
            # 服务端原始英文报错（Need MAIL5K= in Cookie）留给日志，发给发件人的只写人话
            shown = "该附件以「超大附件」方式送达，链接需要邮箱网页登录态才能打开"
        elif len(shown) > 160:
            shown = shown[:160] + "…"
        lines.append(f"- {name}（{human_size(size)}）：{shown}")
    detail = "\n".join(lines)

    over = [x for x in items if x[1] and x[1] > MAIL_LIMIT]
    login = [x for x in items if not over and needs_login(x[2])]
    if over:
        head = (f"《{subject}》的附件确实**超过了邮箱的 20MB 上限**，"
                f"邮件系统只给了一个下载直链，我这边无法直接取回。")
        advise = """处理办法（任选其一）：
1. **分卷发送**：把压缩包拆成多个 < 20MB 的分卷（zip -s 0 或 7z -v），分别发送；
2. **改用网盘链接**：在正文里给出可直链下载的压缩包地址（需免登录直链）；
3. **只发需要审核的部分**：精简掉 node_modules / 依赖目录 / 构建产物后再打包。"""
    else:
        head = (f"《{subject}》的附件是**以链接形式**送达的（邮件系统只给了下载直链，"
                f"没有给可用的附件句柄），我这边取不回该文件。")
        if login:
            head += ("\n\n具体原因：这些附件是以「**超大附件**」方式发出的，"
                     "它的下载链接需要**邮箱网页登录态**才能打开（匿名请求会被服务端拒绝），"
                     "所以自动化流程无法直接取回。")
        advise = """处理办法（任选其一）：
1. **换成普通附件重发**：用「压缩 → 发送」把 zip 作为附件发出（不要用「超大附件」方式）；
2. **给一个可直链下载的压缩包地址**：在正文里贴 https:// 直链，免登录可下；
3. **直接贴代码**：代码量不大时贴正文，并注明文件名。"""

    footer = "" if over else """
补充说明：附件大小以邮件系统返回的信息为准（上面已列出），并不是因为超过 20MB
——本封邮件的附件实际大小见上。"""

    return f"""你好，

{head}

无法取回的附件：
{detail}

{advise}
{footer}

谢谢。"""


def reply_download_failed(subject: str, items) -> str:
    detail = "\n".join(f"- {n}：{e}" for n, e in items)
    return f"""你好，

《{subject}》的附件在下载过程中失败了，暂时无法进行代码审核。

失败的附件：
{detail}

处理办法：
1. 稍后重新发送同一封邮件，我会重新下载并审核；
2. 或换一个网盘直链 / 重新打包后发送。

（我这边会按退避策略自动重试，最多 3 次；若一直失败会再邮件告知。）

谢谢。"""


def reply_extract_failed(subject: str, items) -> str:
    detail = "\n".join(f"- {n}：{e}" for n, e in items)
    return f"""你好，

《{subject}》的附件**解压失败**，无法进行代码审核。

失败的附件：
{detail}

常见原因与处理办法：
1. 压缩包已损坏 → 重新压缩后发送；
2. 压缩包设了密码 → 导出为**未加密**的 zip/tar.gz；
3. 压缩格式不常见（如 rar/7z 分卷）→ 改用标准 zip 或 tar.gz。

谢谢。"""


def reply_no_source(subject: str) -> str:
    return f"""你好，

《{subject}》的附件已收到并解压成功，但其中**没有找到可审核的源码文件**
（常见于：只发了文档/二进制/依赖目录，或源码在子目录里但没有代码扩展名）。

处理办法：
1. 确认压缩包里包含真正的源码文件（.py/.js/.ts/.java/.go/.c/.cpp 等）；
2. 排除 node_modules / venv / dist / build 等依赖与构建产物后再打包；
3. 如果项目是纯文档、配置或数据文件，那它不适合做代码审核。

谢谢。"""


def reply_review_failed(subject: str, reason: str, attempts: int) -> str:
    """审核最终失败（重试用尽）时的回信——发件人必须知道结果，不能石沉大海。"""
    busy = ("service_unavailable" in reason.lower() or "503" in reason
            or "too busy" in reason.lower() or "finish_reason" in reason.lower())
    if busy:
        cause = ("审核所用的**大模型服务当前过载**（上游返回 503 / 流被中断），"
                 "这不是你的代码问题。")
        advice = """处理办法（任选其一）：
1. **稍后重发同一封邮件**，我这边会自动重新审核；
2. **换用其他模型供应商**后再发（我可以帮你切，你也可以直接重发）；
3. 如果你着急，也可以先自己跑一遍静态检查，把结果发我，我再针对性复核。"""
    elif "超时" in reason or "timeout" in reason.lower():
        cause = "审核过程**超时**（代码包较大或模型较慢，超过了单次审核的时间上限）。"
        advice = """处理办法（任选其一）：
1. **精简代码包**（去掉依赖目录、构建产物、测试数据）后重发；
2. 拆成多个小包分批发（例如按模块分），我会逐包审核并分别回信；
3. 稍后重发，我可以调高超时上限再试。"""
    else:
        cause = f"审核过程中出现异常：{reason[:300]}"
        advice = """处理办法（任选其一）：
1. **重新发送**同一封邮件，我会重新处理；
2. 附件较大时建议**精简或分批**发送；
3. 若持续失败，可换一家模型供应商后重试。"""

    return f"""你好，

《{subject}》的代码包已收到，但**本轮自动审核未能完成**。

原因：{cause}
（已自动重试 {attempts} 次仍未成功。）

{advice}

代码包我会保留，重新发送同一封邮件即可，无需重复整理。

谢谢。"""


def reply_report_warning(subject: str, warnings) -> str:
    """报告生成了但有瑕疵——照常附报告，同时说明瑕疵"""
    detail = "\n".join(f"- {w}" for w in warnings)
    return f"""你好，

《{subject}》的代码审核已完成，报告见附件。

需要说明的是报告存在以下瑕疵，建议对照原始代码复核：
{detail}

谢谢。"""


# ---------------------------------------------------------------- 确认信（任务已收到，处理中）
def describe_state(state: str, attempts: int = 0) -> str:
    """把内部状态翻译成发件人能看懂的「现在在干嘛」"""
    s = (state or "").upper()
    if s == "AWAITING_APPROVAL":
        return "代码审核已完成，正在等待人工审批（审批通过即发送报告）"
    if s == "REVIEWED":
        return "代码审核已完成，正在整理报告准备发送"
    if s == "DOWNLOADED":
        return "代码包已下载，AI 正在逐文件审核（代码较多时会分多批进行）"
    if s == "SEEN":
        return "已进入审核队列，正在排队等待处理"
    if s == "FAILED":
        return (f"审核遇到临时问题（如模型服务繁忙/超时），正在自动重试（已重试 {attempts} 次）")
    return "正在处理中"


def reply_ack(subject: str, state_desc: str, eta_hint: str) -> str:
    """超过一定时间还没出结果时，先回一封「已收到、处理中」的确认信。

    目的：让发件人知道任务没丢、现在卡在哪、大概还要多久，避免干等。
    """
    return f"""你好，

《{subject}》的代码审核请求**已收到**，正在处理中，先给你同步一下进度：

当前状态：{state_desc}

预计完成时间：{eta_hint}

处理完成后，我会**立即**把完整的审核报告发给你。在此期间**无需重复发送**，
我会一直跟进；如果最终没能完成，也会明确告诉你原因和下一步怎么办。

谢谢。"""
