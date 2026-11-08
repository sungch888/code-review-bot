"""假 pi CLI：让 pi 后端在没有 pi/模型的环境里也能被端到端验证

真实 pi 的行为：读提示词 -> 自己把报告**写进文件**（不是打到 stdout）。
这里照做：从提示词里解析出【报告输出目录】，再按提示词类型写不同文件。

环境变量：
  PI_STUB_TEXT   罐装报告正文所在文件（由测试写入）
  PI_STUB_MODE   ok（默认）/ busy（只往 stderr 吐 503，不写文件）
用法：python pi_stub.py <pi 的普通参数...> <prompt 或 "已写入文件" 指令>
"""
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("PI_STUB_MODE", "ok")
if mode == "busy":
    sys.stderr.write("503 service_unavailable: model is overloaded\n")
    sys.exit(0)

prompt = args[-1] if args else ""
m = re.search(r"完整任务说明已写入文件：(.+)", prompt)
if m:                                  # 长 prompt 走文件传递
    f = Path(m.group(1).strip())
    if f.exists():
        prompt = f.read_text(encoding="utf-8", errors="replace")

d = re.search(r"【报告输出目录】\s*(.+)", prompt)
if not d:
    sys.stderr.write("stub: 提示词里找不到报告输出目录\n")
    sys.exit(2)
rpt = Path(d.group(1).strip())
rpt.mkdir(parents=True, exist_ok=True)

text = ""
tp = os.environ.get("PI_STUB_TEXT")
if tp and Path(tp).exists():
    text = Path(tp).read_text(encoding="utf-8")

chunk = re.search(r"chunk_(\d+)_报告\.md", prompt)
if chunk:
    (rpt / f"chunk_{chunk.group(1)}_报告.md").write_text(text, encoding="utf-8")
else:
    (rpt / "代码审核报告.md").write_text(text, encoding="utf-8")
    (rpt / "summary.md").write_text("pi 后端审核完成。", encoding="utf-8")
print("DONE")
