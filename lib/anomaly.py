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
                     timeout: int = 120):
    """尝试直接下载 download_url 形式的附件。

    注意：QQ 邮箱的 ftn 下载域名需要会话鉴权，匿名请求通常返回 400，
    所以这里**尽力而为**，失败就如实上报，不做任何假设。

    返回 (ok: bool, path: Path|None, err: str)
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                return False, None, f"链接返回的是 HTML 页面（{ctype}），不是文件"
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
                        return False, None, f"文件超过下载上限 {human_size(max_bytes)}"
                    f.write(chunk)
        if total == 0:
            dest.unlink(missing_ok=True)
            return False, None, "链接下载到 0 字节"
        return True, dest, ""
    except urllib.error.HTTPError as e:
        return False, None, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


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
        lines.append(f"- {name}（{human_size(size)}）：{err or '无法直接取回'}")
    detail = "\n".join(lines)

    over = [x for x in items if x[1] and x[1] > MAIL_LIMIT]
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
        advise = """处理办法（任选其一）：
1. **换成普通附件重发**：用「压缩 → 发送」把 zip 作为附件发出（不要用「超大附件」方式）；
2. **给一个可直链下载的压缩包地址**：在正文里贴 https:// 直链，免登录可下；
3. **直接贴代码**：代码量不大时贴正文，并注明文件名。"""

    return f"""你好，

{head}

无法取回的附件：
{detail}

{advise}

补充说明：附件大小以邮件系统返回的信息为准（上面已列出），并不是因为超过 20MB
——本封邮件的附件实际大小见上。

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
