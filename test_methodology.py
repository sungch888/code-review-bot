#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审核方法论注入 + 报告不泄露来源的验证

需求：
  1. 审核要按方法论做（六类结构性风险扫描 + 每条必须 现象/成因/后果/建议）
  2. 但报告里**不得**出现方法论的名称、代号、来源、工具/书籍名
  3. 不得声称「已用工具修复代码」——审核只给建议

这里验证三件事：
  A. 方法论文件存在、被注入到每个提示词（单批/分批/汇总/pi 后端）
  B. 提示词里有「不要写进报告」的禁令
  C. 校验器能发现泄露、擦除函数能清掉泄露、且不误伤正常中文
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {extra}")


import bot as B  # noqa: E402


def main():
    meth = B.load_methodology(full=True)
    meth_slim = B.load_methodology(full=False)

    # ---------- A. 方法论内容 ----------
    check("方法论文件存在且非空", len(meth) > 800, f"{len(meth)} 字符")
    for kw in ("认知负担", "变更扩散", "知识重复", "过度设计", "依赖失序", "领域失真"):
        check(f"方法论含风险维度「{kw}」", kw in meth)
    for kw in ("现象", "成因", "后果", "修改建议"):
        check(f"方法论含四段格式「{kw}」", kw in meth)
    check("方法论含「不要报什么」（防误报）", "不要报什么" in meth or "不该报" in meth)
    check("方法论含工程判据（幂等/边界/单一决策点）",
          "幂等" in meth and "边界处校验" in meth and "一个决策只存在一处" in meth)

    # 精简版必须比全量短，且仍带禁令
    check("精简版可用且更短", 0 < len(meth_slim) < len(meth),
          f"{len(meth_slim)} vs {len(meth)}")
    check("精简版仍含「不要报什么」", "不要报什么" in meth_slim or "不该报" in meth_slim)
    check("精简版仍含输出禁令", "禁令" in meth_slim or "严禁" in meth_slim)

    # ---------- A2. 方法论本身不带外部来源名 ----------
    for w in ("Brooks", "brooks", "ub-mode", "ub mode", "Iron Law",
              "Fowler", "McConnell", "Evans", "Ousterhout", "Hunt", "Thomas",
              "Martin", "Winters", "Feathers", "Osherove", "Meszaros"):
        check(f"方法论文本不含来源名「{w}」", w not in meth)

    # ---------- B. 注入到提示词 ----------
    def rendered(tpl, **kw):
        base = dict(src="S", rpt="R", extra="", sections="SEC", count=1,
                    idx=1, total=2, file_count=1, chunk_reports="CH",
                    files_content="FC", filelist="FL", baseline="", methodology=meth,
                    issue_columns=B.issue_columns_text(), cross_context="")
        base.update(kw)
        return tpl.format(**base)

    single = rendered(B.REVIEW_PROMPT_HTTP)
    check("单批提示词注入全量方法论", "认知负担" in single and "领域失真" in single)
    check("单批提示词要求 成因/后果 列",
          "成因" in single and "后果" in single, single[:200])

    chunk = rendered(B.REVIEW_PROMPT_HTTP_CHUNK, methodology=meth_slim)
    check("分批提示词注入精简方法论",
          ("认知负担" in chunk) or ("不要报什么" in chunk or "不该报" in chunk))
    check("分批提示词表头含 成因/后果/证据",
          "编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据" in chunk,
          chunk[:300])

    merge = rendered(B.REVIEW_PROMPT_HTTP_MERGE, methodology=meth_slim)
    check("汇总提示词注入方法论", "认知负担" in merge or "不要报什么" in merge)
    check("汇总提示词要求补全 成因/后果", "成因" in merge and "后果" in merge)
    check("汇总提示词要求去误报", "去误报" in merge or "宁少勿滥" in merge)

    pi_prompt = rendered(B.REVIEW_PROMPT)
    check("pi 后端提示词注入方法论", "认知负担" in pi_prompt)

    # ---------- B2. 禁令必须处处存在 ----------
    check("system 提示词含禁令（不写方法论名/不声称已改代码）",
          "方法论" in B.REVIEW_SYSTEM_HTTP and "不要声称已修改代码" in B.REVIEW_SYSTEM_HTTP)
    for nm, txt in (("单批", single), ("分批", chunk), ("汇总", merge)):
        check(f"{nm}提示词含「不要写进报告」禁令",
              "不要写进报告" in txt or "严禁" in txt, txt[-200:])
    check("修复规则含禁令（修复重跑也不会泄露）",
          "不得出现" in B.fix_rules_text() and "方法论" in B.fix_rules_text())

    # ---------- C. 校验器与擦除 ----------
    # 正常报告不该被判泄露
    normal = ("## 问题清单\n| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |\n"
              "| --- | --- | --- | --- | --- | --- | --- |\n"
              "| 1 | 严重 | a.py:1 | SQL 拼接 | 直接拼参数 | 可被注入读全库 | 参数化 | 代码事实 a.py:1 |\n")
    clean, hits = B.scrub_attribution(normal)
    check("正常中文报告不被误擦", hits == [] and clean == normal, str(hits))

    leaked = ("## 问题清单\n依据 Brooks-Lint 的 R1 定义，"
              "Symptom: SQL 拼接\nSource: Refactoring\n"
              "| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |\n")
    clean2, hits2 = B.scrub_attribution(leaked)
    check("擦除函数能识别泄露", len(hits2) >= 2, str(hits2))
    check("擦除后不再含 Brooks", "Brooks" not in clean2, clean2[:120])
    check("擦除后不再含 Symptom:/Source:",
          "Symptom:" not in clean2 and "Source:" not in clean2, clean2[:120])
    check("擦除后正文仍在（没把内容吃光）",
          "SQL 拼接" in clean2 and "问题清单" in clean2, clean2[:200])

    # 校验器能把泄露判成不合规
    bad = normal + "\n本报告依据 Iron Law 输出，Source: Refactoring\n" + "补充说明。" * 80
    issues = B.validate_report(bad, n_files=1)
    check("校验器把泄露判为不合规",
          any("来源名称" in i for i in issues), str(issues))

    # ---------- D. 报告规范里表格列已更新 ----------
    tbl = next((s for s in B.REPORT_SPEC["sections"] if s.get("type") == "table"), {})
    cols = tbl.get("columns") or []
    check("规范列含 成因/后果",
          "成因" in cols and "后果" in cols, str(cols))
    check("规范列顺序正确",
          cols == ["编号", "级别", "文件:行号", "问题描述", "成因", "后果", "修改建议", "证据"],
          str(cols))
    check("规范声明了禁泄露检查",
          any("forbidden" in gc for gc in B.REPORT_SPEC.get("global_checks", [])))

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
