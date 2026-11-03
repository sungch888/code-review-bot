"""Markdown -> HTML 转换（用于报告的 HTML 版）。

目的：HTML 由本地统一生成，而不是让模型再写一遍——
这样「Markdown 与 HTML 内容一致」才是可验证的事实，而非承诺。

用法: python lib/md2html.py <in.md> <out.html> [title]
"""
import html
import re
import sys
from pathlib import Path

INLINE_PATTERNS = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<em>\1</em>"),
    (re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)"), r'<a href="\2" rel="noopener">\1</a>'),
]


def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    for pat, rep in INLINE_PATTERNS:
        s = pat.sub(rep, s)
    return s


def _slug(text: str, used: dict) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text.strip().lower()).strip("-") or "sec"
    if s in used:
        used[s] += 1
        s = f"{s}-{used[s]}"
    else:
        used[s] = 0
    return s


def md_to_html(md: str, title: str = "代码审核报告",
               accent: str = "#c0392b", max_width: str = "980px") -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out = []
    used = {}
    toc = []
    i = 0
    in_code = False
    code_buf = []
    para = []

    def flush_para():
        if para:
            out.append("<p>" + _inline(" ".join(x.strip() for x in para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]

        # 代码块
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                flush_para()
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        # 表格
        if line.strip().startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            flush_para()
            def cells(row):
                row = row.strip()
                if row.startswith("|"):
                    row = row[1:]
                if row.endswith("|"):
                    row = row[:-1]
                return [c.strip() for c in row.split("|")]
            head = cells(line)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>" +
                       "".join(f"<th>{_inline(h)}</th>" for h in head) +
                       "</tr></thead><tbody>" +
                       "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                               for r in body) +
                       "</tbody></table>")
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para()
            lvl = len(m.group(1))
            txt = m.group(2).strip()
            sid = _slug(txt, used)
            if lvl <= 2:
                toc.append((lvl, txt, sid))
            out.append(f'<h{lvl} id="{sid}">{_inline(txt)}</h{lvl}>')
            i += 1
            continue

        # 分隔线
        if re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        # 引用
        if line.strip().startswith(">"):
            flush_para()
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + _inline(" ".join(buf)) + "</blockquote>")
            continue

        # 列表
        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if m:
            flush_para()
            items = []
            tag = "ul"
            while i < len(lines):
                mm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
                if not mm:
                    if lines[i].strip() == "" and i + 1 < len(lines) and \
                            re.match(r"^(\s*)([-*+]|\d+\.)\s+", lines[i + 1]):
                        i += 1
                        continue
                    break
                ordered = bool(re.match(r"\d+\.", mm.group(2)))
                items.append((tag, mm.group(3)))
                if ordered:
                    tag = "ol"
                i += 1
            out.append(f"<{tag}>" +
                       "".join(f"<li>{_inline(t)}</li>" for _, t in items) +
                       f"</{tag}>")
            continue

        # 空行
        if not line.strip():
            flush_para()
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    if in_code and code_buf:
        out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")

    toc_html = ""
    if len(toc) > 1:
        toc_html = ("<nav class='toc'><div class='toc-title'>目录</div><ul>" +
                    "".join(f"<li class='l{lvl}'><a href='#{sid}'>{html.escape(t)}</a></li>"
                            for lvl, t, sid in toc) +
                    "</ul></nav>")

    css = f"""
    :root {{ color-scheme: light; }}
    body {{ font-family: -apple-system,"Segoe UI",Roboto,"Helvetica Neue","PingFang SC",
           "Microsoft YaHei",Arial,sans-serif; line-height:1.75; color:#222; background:#f7f8fa;
           margin:0; padding:28px 16px; }}
    .wrap {{ max-width:{max_width}px; margin:0 auto; background:#fff; padding:36px 44px;
             border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
    h1 {{ color:{accent}; border-bottom:3px solid {accent}; padding-bottom:12px; margin-top:0; }}
    h2 {{ color:{accent}; margin-top:36px; border-left:5px solid {accent}; padding-left:12px; }}
    h3 {{ margin-top:26px; color:#333; }}
    h4 {{ color:#555; }}
    table {{ border-collapse:collapse; width:100%; margin:18px 0; font-size:14px; }}
    th,td {{ border:1px solid #dfe3e8; padding:9px 11px; text-align:left; vertical-align:top; }}
    th {{ background:#f2f4f7; font-weight:600; }}
    tr:nth-child(even) td {{ background:#fafbfc; }}
    code {{ background:#f2f4f7; padding:2px 6px; border-radius:4px; font-size:13px;
            font-family:Consolas,"Courier New",monospace; }}
    pre {{ background:#1f2430; color:#e6e6e6; padding:16px 18px; border-radius:8px;
           overflow-x:auto; line-height:1.55; }}
    pre code {{ background:none; color:inherit; padding:0; }}
    blockquote {{ margin:16px 0; padding:10px 18px; border-left:4px solid #c9d2dc;
                  background:#f7f9fb; color:#555; }}
    hr {{ border:none; border-top:1px solid #e3e6ea; margin:28px 0; }}
    .toc {{ background:#f7f9fb; border:1px solid #e3e6ea; border-radius:8px;
            padding:14px 20px; margin:22px 0 30px; }}
    .toc-title {{ font-weight:700; margin-bottom:8px; color:{accent}; }}
    .toc ul {{ margin:0; padding-left:20px; }}
    .toc li {{ margin:3px 0; }}
    .toc li.l2 {{ margin-left:14px; }}
    .meta {{ color:#888; font-size:13px; border-bottom:1px solid #eee; padding-bottom:16px;
             margin-bottom:24px; }}
    """
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{css}</style></head>
<body><div class="wrap">
{toc_html}
{chr(10).join(out)}
</div></body></html>"""


def convert_file(src: Path, dst: Path, title: str = "代码审核报告",
                 accent: str = "#c0392b", max_width: str = "980px") -> str:
    md = src.read_text(encoding="utf-8", errors="replace")
    dst.write_text(md_to_html(md, title, accent, max_width), encoding="utf-8")
    return str(dst)


if __name__ == "__main__":
    s, d = Path(sys.argv[1]), Path(sys.argv[2])
    t = sys.argv[3] if len(sys.argv) > 3 else "代码审核报告"
    print(convert_file(s, d, t))
