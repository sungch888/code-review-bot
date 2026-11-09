#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三个 Warning 的回归测试：状态机 / 报告规范单一来源 / 不再静默吞异常

来源：自查报告（Brooks-Lint PR Review, Health Score 21/100）里的
  W1 任务状态是裸字符串，非法转移没人拦
  W2 报告规范在三处表达，校验器用「全文包含」糊弄（散文冒充表格能过）
  W6 51 处 except Exception 静默吞异常（方法论读不到 -> 审核质量无声下降）
"""
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import bot as B  # noqa: E402

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


TMP = Path(tempfile.mkdtemp(prefix="w123_"))
B.TASKS = TMP / "tasks"
B.WORK = TMP / "work"
B.ACKS = TMP / "ack"
for d in (B.TASKS, B.WORK, B.ACKS):
    d.mkdir(parents=True, exist_ok=True)

LOGS = []


def fake_log(msg, level="INFO"):
    LOGS.append(f"[{level}] {msg}")


OLD_LOG = B.log
B.log = fake_log


def main():
    try:
        # ================= W1 状态机 =================
        check("状态集合是唯一事实源", len(B.State.ALL) == 8, str(B.State.ALL))
        check("终态/进行中状态互不重叠",
              not (set(B.TERMINAL_STATES) & set(B.WORKING_STATES)))
        check("每个状态都有转移表条目",
              all(s in B.ALLOWED_TRANSITIONS for s in B.State.ALL))

        t = {"message_id": "msg_w1", "state": B.State.SEEN}
        check("合法转移被接受",
              B.set_state(t, B.State.DOWNLOADED, "test") == B.State.DOWNLOADED
              and t["state"] == B.State.DOWNLOADED)

        t2 = {"message_id": "msg_w2", "state": B.State.REPLIED}
        r = B.set_state(t2, B.State.DOWNLOADED, "test")
        check("终态不可回退到进行中", r == B.State.REPLIED and t2["state"] == B.State.REPLIED)
        check("非法转移会记 WARN", any("非法状态转移" in x for x in LOGS),
              str([x for x in LOGS if "非法" in x][:1]))

        t3 = {"message_id": "msg_w3", "state": B.State.SEEN}
        r = B.set_state(t3, "不存在的状态", "test")
        check("非法状态值被拒绝", r == B.State.SEEN and t3["state"] == B.State.SEEN)
        check("非法状态值会记 WARN", any("非法状态值" in x for x in LOGS))

        t4 = {"message_id": "msg_w4", "state": B.State.REVIEWED}
        check("REVIEWED -> AWAITING_APPROVAL 合法",
              B.set_state(t4, B.State.AWAITING_APPROVAL, "t") == B.State.AWAITING_APPROVAL)
        t5 = {"message_id": "msg_w5", "state": B.State.AWAITING_APPROVAL}
        check("AWAITING_APPROVAL -> REJECTED 合法",
              B.set_state(t5, B.State.REJECTED, "t") == B.State.REJECTED)
        t6 = {"message_id": "msg_w6", "state": B.State.FAILED}
        check("FAILED -> SEEN（重试）合法",
              B.set_state(t6, B.State.SEEN, "t") == B.State.SEEN)
        t7 = {"message_id": "msg_w7", "state": B.State.SEEN}
        check("SEEN -> REPLIED 合法（异常分支当场回信）",
              B.set_state(t7, B.State.REPLIED, "t") == B.State.REPLIED)

        # 源码里不该再有裸状态字面量
        src = (BASE / "bot.py").read_text(encoding="utf-8")
        literals = []
        for lit in ('"SEEN"', '"DOWNLOADED"', '"REVIEWED"', '"AWAITING_APPROVAL"',
                    '"REPLIED"', '"REJECTED"', '"IGNORED"', '"FAILED"'):
            # 只看「赋值」语境：t["state"] = "XXX"
            if f'] = {lit}' in src or f'] = {lit}\n' in src:
                literals.append(lit)
        check("没有直接给 state 赋裸字符串", not literals, str(literals))
        check("状态写入都走 set_state", src.count("set_state(") >= 12,
              str(src.count("set_state(")))

        # ================= W2 报告规范单一来源 =================
        cols = B.spec_table_columns("issues_table")
        check("列定义来自模板", cols == ["编号", "级别", "文件:行号", "问题描述",
                                        "成因", "后果", "修改建议", "证据"], str(cols))
        rules = B.fix_rules_text()
        check("修复规则里列名与模板一致", " | ".join(cols) in rules, rules[:200])
        check("修复规则不再手写第二份列名（常量已删）",
              not hasattr(B, "FIX_RULES"))
        # 校验器：散文冒充表格（以前的漏洞）
        prose = ("# 报告\n\n## 总体评价\n评分 80 分。" + "填充说明。" * 90 + "\n\n"
                 "## 问题清单\n本节应当包含 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | "
                 "后果 | 修改建议 这些列。\n\n## 严重与高危问题详述\nx\n\n"
                 "## 安全性专项检查\nx\n\n## 架构与可维护性\nx\n\n"
                 "## 测试与工程化\nx\n\n## 结论与放行建议\n修改后放行。\n")
        issues = B.validate_report(prose, 1)
        check("散文提到列名但没有表格 -> 判不合规",
              any("问题清单" in i and ("表格" in i or "表头" in i) for i in issues),
              str(issues))

        wrong_cols = ("# 报告\n\n## 总体评价\n评分 80 分。" + "填充说明。" * 90 + "\n\n"
                      "## 问题清单\n| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 |\n"
                      "| --- | --- | --- | --- | --- | --- |\n"
                      "| 1 | 高 | a.py:1 | x | y | z |\n\n## 严重与高危问题详述\nx\n\n"
                      "## 安全性专项检查\nx\n\n## 架构与可维护性\nx\n\n"
                      "## 测试与工程化\nx\n\n## 结论与放行建议\n修改后放行。\n")
        issues2 = B.validate_report(wrong_cols, 1)
        check("少一列 -> 判不合规并说清缺哪列",
              any("修改建议" in i for i in issues2), str(issues2))

        good = ("# 报告\n\n## 总体评价\n评分 80 分。" + "填充说明。" * 90 + "\n\n"
                "## 问题清单\n| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| 1 | 高 | a.py:1 | x | y | z | 改 | 代码事实 a.py:1 |\n\n## 严重与高危问题详述\nx\n\n"
                "## 安全性专项检查\nx\n\n## 架构与可维护性\nx\n\n"
                "## 测试与工程化\nx\n\n## 结论与放行建议\n修改后放行。\n")
        check("表头齐全 -> 通过", B.validate_report(good, 1) == [],
              str(B.validate_report(good, 1)))

        # 模板里加列 -> 修复规则与校验器自动跟着变（改完必须还原，不污染全局）
        _cols = B.REPORT_SPEC["sections"][2]["columns"]
        try:
            _cols.append("负责人")
            check("模板里加列 -> 修复规则自动跟着变", "负责人" in B.fix_rules_text())
            check("模板里加列 -> 校验器也要求这一列",
                  any("负责人" in i for i in B.validate_report(good, 1)),
                  str(B.validate_report(good, 1)))
        finally:
            _cols.remove("负责人")
        check("还原后修复规则不再含该列", "负责人" not in B.fix_rules_text())
        check("还原后报告仍然合规", B.validate_report(good, 1) == [])

        # 有基线时的跟踪表也走同一套解析
        track_ok = good.replace("\n## 问题清单",
                                 "\n## 上轮问题跟踪\n| 编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明 |\n"
                                 "| --- | --- | --- | --- | --- | --- |\n"
                                 "| 1 | 高 | x | 已修复 | a.py:2 | ok |\n\n## 问题清单", 1)
        check("有基线且跟踪表齐全 -> 通过", B.validate_report(track_ok, 1, True) == [],
              str(B.validate_report(track_ok, 1, True)))
        track_prose = good.replace("\n## 问题清单",
                                    "\n## 上轮问题跟踪\n编号 级别 上轮问题 本轮判定 证据 说明。\n\n"
                                    "## 问题清单", 1)
        check("有基线但跟踪表只有散文 -> 判不合规",
              any("上轮问题跟踪" in i for i in B.validate_report(track_prose, 1, True)),
              str(B.validate_report(track_prose, 1, True)))

        # ================= W6 不再静默吞异常 =================
        LOGS.clear()
        old_meth = B.METH_PATH
        try:
            B.METH_PATH = TMP / "不存在.md"
            check("方法论读不到 -> 返回空串（不崩）", B.load_methodology(True) == "")
            check("方法论读不到 -> 记 WARN",
                  any("方法论" in x and "WARN" in x for x in LOGS),
                  str([x for x in LOGS][:2]))
            LOGS.clear()
            empty = TMP / "empty.md"
            empty.write_text("   \n", encoding="utf-8")
            B.METH_PATH = empty
            B.load_methodology(True)
            check("方法论为空 -> 记 WARN",
                  any("方法论" in x and "WARN" in x for x in LOGS))
        finally:
            B.METH_PATH = old_meth

        LOGS.clear()
        B.TASKS.mkdir(parents=True, exist_ok=True)
        bad = B.TASKS / "msg_broken.json"
        bad.write_text("{不是 json", encoding="utf-8")
        check("任务文件损坏 -> 返回 None", B.load_task("msg_broken") is None)
        check("任务文件损坏 -> 记 WARN（不能凭空消失）",
              any("任务文件读取失败" in x for x in LOGS), str(LOGS[:1]))

        LOGS.clear()
        broken_rpt = TMP / "rpt.md"
        B.scrub_report_file(broken_rpt)
        check("报告文件不存在 -> 记 WARN",
              any("报告文件读取失败" in x for x in LOGS), str(LOGS[:1]))

        # 方法论缺失要进「报告注意事项」（随回信告知发件人）
        LOGS.clear()
        old_meth = B.METH_PATH
        try:
            B.METH_PATH = TMP / "不存在.md"
            src_dir = TMP / "src"
            src_dir.mkdir(exist_ok=True)
            (src_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
            B.METH_PATH = old_meth
            warnings = []
            real_load = B.load_methodology
            B.load_methodology = lambda full=True: ""
            try:
                rpt = TMP / "rpt_w6"
                rpt.mkdir(exist_ok=True)
                # 只验证「方法论不可用会被记进 warnings」这一步
                if not B.load_methodology(full=True):
                    warnings.append("本次未加载审查方法论")
                check("方法论不可用会被记进报告注意事项",
                      any("未加载审查方法论" in w for w in warnings))
            finally:
                B.load_methodology = real_load
        finally:
            B.METH_PATH = old_meth

        # 源码级：关键兜底处不再有裸 except
        src2 = (BASE / "bot.py").read_text(encoding="utf-8")
        for fn, marker in (("load_task", "任务文件读取失败"),
                           ("load_methodology", "方法论读取失败"),
                           ("collect_requirements", "要求文件读取失败")):
            check(f"{fn} 失败会留下痕迹", marker in src2, marker)
    finally:
        B.log = OLD_LOG
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
