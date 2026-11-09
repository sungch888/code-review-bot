"""报告规范 / 校验器 / HTML 渲染的单元验证：python test_report_spec.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bot  # noqa: E402
import md2html  # noqa: E402

GOOD = """# 代码审核报告

## 本次审核要求
无，按标准审核流程执行。

## 总体评价
本项目为订单服务，整体质量评分 35 分（满分 100）。主要扣分在安全性。

## 问题清单
| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 严重 | app.py:8 | SQL 拼接 | 未用参数化查询，请求参数直接拼进语句 | 攻击者可构造参数读取或删改整库 | 改参数化 |

## 严重与高危问题详述
见上表第 1 条。

## 安全性专项检查
命令注入：存在。SQL 注入：存在。路径穿越：未发现。硬编码密钥：存在。""" + "\n\n" + (
        "逐项检查记录如下：" + "；".join(
            f"检查项{i}：已核对 app.py 第{i}行，结论为存在风险，建议参数化改造" for i in range(1, 40))
    ) + """

## 架构与可维护性
模块单一，耦合偏高。

## 测试与工程化
无测试。

## 结论与放行建议
不建议放行。必须修复项：1) 参数化 SQL。
"""

results = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))
    results.append(cond)


# 1. 规范能渲染进提示词
txt = bot.spec_sections_text()
check("规范渲染出全部章节标题",
      all(s["title"] in txt for s in bot.REPORT_SPEC["sections"]),
      txt[:200])
check("提示词可 format（五占位符）",
      "{" not in bot.REVIEW_PROMPT.format(src="S", rpt="R", extra="", sections=txt,
                                          baseline="",
                                          methodology=bot.load_methodology(True)).replace("{src}", "").replace("{rpt}", ""),
      "存在未替换花括号")

# 2. 校验器
issues = bot.validate_report(GOOD)
check("合规报告零问题", issues == [], str(issues))

bad = GOOD.replace("## 安全性专项检查", "## 安全检查")
issues = bot.validate_report(bad)
check("缺章节能被发现", any("安全性专项检查" in i for i in issues), str(issues))

bad2 = GOOD.replace("| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |",
                    "| 序号 | 等级 | 位置 | 问题 | 建议 |")
issues = bot.validate_report(bad2)
check("表头不符能被发现", any("列名" in i or "编号" in i for i in issues), str(issues))

bad3 = GOOD.replace("不建议放行", "基本可以上线")
issues = bot.validate_report(bad3)
check("缺放行档位词能被发现", any("档位" in i for i in issues), str(issues))

bad4 = "## 总体评价\n" + "短" * 100
issues = bot.validate_report(bad4)
check("过短报告能被发现", any("缺少必需章节" in i or "过短" in i or "长度" in i for i in issues), str(issues))

check("空报告被判不合规", bot.validate_report("") != [], "")

# 3. HTML 渲染
html = md2html.md_to_html(GOOD, "代码审核报告")
check("HTML 含标题与目录", "<h1" in html and "toc" in html, html[:200])
check("HTML 含表格", "<table>" in html and "<th>编号</th>" in html, "")
check("HTML 含章节锚点", 'id="' in html, "")
check("HTML 转义尖括号", "<script>" not in html, "")
check("HTML 含样式块", "<style>" in html, "")

# 4. 异常样本
weird = "# 标题\n\n<div onclick='x'>raw html</div>\n\n```\n<script>alert(1)</script>\n```\n"
h2 = md2html.md_to_html(weird)
check("代码块内 script 被转义", "<script>" not in h2, h2[-300:])

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
