"""统一测试入口：python run_tests.py

包含:
  1) pyflakes 静态检查（抓未定义名等——曾经真的漏过一个 task/t 拼写 bug）
  2) 审核要求解析单测
  3) 报告规范/校验器/HTML 渲染单测
"""
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent


def run(cmd, label):
    print(f"\n===== {label} =====")
    p = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    print(out.strip()[-4000:])
    return p.returncode


rc = 0
rc |= run([sys.executable, "-m", "pyflakes", "bot.py", "ui.py", "lib/md2html.py", "lib/llm.py", "lib/anomaly.py",
           "test_requirements.py", "test_report_spec.py", "test_ui.py",
           "test_retry.py", "test_chunk.py", "test_lock.py",
           "test_anomaly.py", "test_llm.py",
           "test_approval.py"],
         "1. 静态检查 pyflakes")
rc |= run([sys.executable, "test_requirements.py"], "2. 审核要求解析")
rc |= run([sys.executable, "test_report_spec.py"], "3. 报告规范/校验/渲染")
rc |= run([sys.executable, "test_retry.py"], "4. 重试/退避/上游繁忙")
rc |= run([sys.executable, "test_chunk.py"], "5. 分块审核")
rc |= run([sys.executable, "test_lock.py"], "6. 僵尸锁检测")
rc |= run([sys.executable, "test_anomaly.py"], "7. 异常回信（不误报/可操作）")
rc |= run([sys.executable, "test_llm.py"], "8. 通用模型接入（base_url/模型列表/错误翻译）")
rc |= run([sys.executable, "test_approval.py"], "9. 审批闸 + 处理中确认信")
rc |= run([sys.executable, "test_ui.py"], "10. 控制台 UI（路由/XSS/CSRF/路径穿越/模型接入/审批面板）")
rc |= run([sys.executable, "test_providers.py"], "11. 命名配置（多套 LLM 配置保存 + 随时切换）")
rc |= run([sys.executable, "test_no_window.py"], "12. 黑窗防护（CREATE_NO_WINDOW）")

print("\n" + "=" * 40)
print("全部通过" if rc == 0 else "存在失败项")
sys.exit(rc)
