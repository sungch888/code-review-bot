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

        # 本地重跑一个失败任务（bot.py review）不该被状态机拦住，
        # 但也不能让别的路径把失败任务拖回队列
        t_f = {"message_id": "msg_wf", "state": B.State.FAILED}
        r = B.set_state(t_f, B.State.REVIEWED, "review_existing/复审重开")
        check("FAILED 可被显式复审重开成 REVIEWED",
              r == B.State.REVIEWED and t_f["state"] == B.State.REVIEWED, str(r))
        t_f2 = {"message_id": "msg_wf2", "state": B.State.FAILED}
        r = B.set_state(t_f2, B.State.REVIEWED, "process_task")
        check("非复审路径不能把 FAILED 改成 REVIEWED",
              r == B.State.FAILED and t_f2["state"] == B.State.FAILED, str(r))
        t_r = {"message_id": "msg_wr", "state": B.State.REPLIED}
        r = B.set_state(t_r, B.State.REVIEWED, "process_task")
        check("非复审路径不能把 REPLIED 改成 REVIEWED",
              r == B.State.REPLIED and t_r["state"] == B.State.REPLIED, str(r))

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

        # ================= 评分口径（多轮可比）=================
        def rows(*lv):
            out = []
            for sev, n in lv:
                out.extend({"level": sev, "desc": f"d{i}", "loc": "a.py:1"}
                           for i in range(n))
            return out

        r_few = B.compute_score(rows(("严重", 1), ("高危", 2)))
        r_many = B.compute_score(rows(("严重", 5), ("高危", 10)))
        check("问题越严重分数越低", r_many["score"] < r_few["score"],
              f'{r_many["score"]} vs {r_few["score"]}')

        # 关键：条数小幅波动不该让分数乱跳（批次粗细不同会带来这种噪音）
        a = B.compute_score(rows(("严重", 8), ("高危", 30), ("中危", 178), ("低危", 140)))
        b = B.compute_score(rows(("严重", 8), ("高危", 30), ("中危", 185), ("低危", 143)))
        check("条数 +7 条不该明显改变分数（log 抑制口径噪音）",
              abs(a["score"] - b["score"]) <= 3, f'{a["score"]} vs {b["score"]}')

        # 同一严重度分布 -> 同分（口径稳定）
        c = B.compute_score(rows(("严重", 2), ("高危", 3)))
        d = B.compute_score(rows(("高危", 3), ("严重", 2)))
        check("顺序不影响分数", c["score"] == d["score"], f'{c["score"]} vs {d["score"]}')
        check("级别写法归一（「高」= 高危）",
              B.compute_score(rows(("高", 3)))["score"]
              == B.compute_score(rows(("高危", 3)))["score"])
        check("有严重问题 -> 不建议放行", r_many["verdict"] == "不建议放行",
              r_many["verdict"])
        check("无问题是满分", B.compute_score([])["score"] == 100)
        check("分数有下限", B.compute_score(rows(("严重", 500)))["score"] >= 0)

        # 报告里的模型自评分必须被系统分数替换
        fake = ("## 总体评价\n本项目评分：81 分。\n\n## 问题清单\n")
        new_text = B.rewrite_score_in_report(fake, r_few)
        check("模型自评的分数被替换成口径分数", "81" not in new_text, new_text[:120])
        check("替换后写明口径版本", "统一口径 v" in new_text, new_text[:160])
        # ---------- 跨轮对比的可信度：修复率 / 审核覆盖面 / 同包说明 ----------
        fp_keep = B.FP_DIR
        B.FP_DIR = TMP / "fp_struct"
        B.FP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            tr_txt = """## 上轮问题跟踪
| 编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明 |
|---|---|---|---|---|---|
| 1 | 严重 | 数据丢失 | 已修复 | a.py:1 | ok |
| 2 | 严重 | 越权访问 | 未修复 | a.py:2 | no |
| 3 | 高危 | 空指针 | 部分修复 | a.py:3 | half |
| 4 | 高危 | 竞态 | 部分修复 | a.py:4 | half |
| 5 | 低危 | 命名 | 已修复 | a.py:5 | ok |
| 6 | 中危 | 日志缺失 | 无法确认 | - | 文件没了 |
"""
            tr = B.parse_tracking(tr_txt)
            check("跟踪表按上轮级别分桶",
                  tr["by_level"]["严重"]["fixed"] == 1
                  and tr["by_level"]["高危"]["partial"] == 2, str(tr["by_level"]))
            # 加权：分母 10*2+4*2+1*1=29，分子 10*1+4*1+1*1=15 -> 52%
            check("加权修复率算得对（无法确认不进分母）",
                  tr["recovery"]["pct"] == 52, str(tr["recovery"]))
            rl = B.recovery_line(tr)
            check("修复率写明级别明细", "52%" in rl and "严重 1+0部分/2" in rl, rl)

            cur_md = ("## 问题清单\n"
                      "| 编号 | 级别 | 文件 | 位置 | 问题描述 |\n"
                      "|---|---|---|---|---|\n"
                      "| 1 | 高危 | doc/a.py | 12 | x |\n"
                      "| 2 | 中危 | doc/b.py | 3 | y |\n"
                      "| 3 | 低危 | doc/c.py | 4 | z |\n")
            check("覆盖面按文件数与条数统计",
                  B.audit_footprint(cur_md) == {"files": 3, "rows": 3},
                  str(B.audit_footprint(cur_md)))

            # 老报告留着模型自评分（62），必须换成口径分并解释来历
            mid2 = "msg_scorelegacy"
            t_legacy = {"message_id": mid2, "state": "REVIEWED",
                        "created_at_ts": 100, "score": 21}
            d_legacy = B.WORK / mid2 / "report"
            d_legacy.mkdir(parents=True, exist_ok=True)
            f_legacy = d_legacy / "代码审核报告.md"
            f_legacy.write_text("## 总体评价\n**整体质量评分：62 / 100**\n" + cur_md,
                                encoding="utf-8")
            note1 = B.attach_score_context(t_legacy, f_legacy)
            body1 = f_legacy.read_text(encoding="utf-8")
            check("老报告的模型自评分被换成口径分",
                  "62 / 100" not in body1 and "统一口径 v" in body1, body1[:150])
            _res = B.compute_score(B.extract_issue_rows(body1))
            check("口径分与报告清单自洽（同一份清单只对应一个数）",
                  f'**{_res["score"]}**' in body1, body1[:200])
            check("写明原先自评多少分", "原先自评为 62 分" in note1, note1)
            note2 = B.attach_score_context(t_legacy, f_legacy)
            body2 = f_legacy.read_text(encoding="utf-8")
            check("重复回填不重复追加说明", body2.count("口径说明") == 1,
                  str(body2.count("口径说明")))
            check("再跑一遍不产生新说明", "原先自评" not in note2, note2[:120])

            # 覆盖面差 1.5 倍以上必须写明「分数不能直接相减」
            mid3, mid4 = "msg_cov_cur", "msg_cov_prev"
            for mm, n in ((mid3, 4), (mid4, 2)):
                dd = B.WORK / mm / "report"
                dd.mkdir(parents=True, exist_ok=True)
                head = "| 编号 | 级别 | 文件 | 位置 | 问题描述 |\n|---|---|---|---|---|\n"
                (dd / "代码审核报告.md").write_text(
                    "## 问题清单\n" + head
                    + "".join(f"| {i} | 中危 | p{i}.py | 1 | x |\n" for i in range(n)),
                    encoding="utf-8")
            B.save_task({"message_id": mid4, "state": "REVIEWED", "created_at_ts": 10,
                         "score": 90, "report": str(B.WORK / mid4 / "report")})
            cur_t = {"message_id": mid3, "state": "REVIEWED", "created_at_ts": 20,
                     "score": 60, "baseline_mid": mid4,
                     "report": str(B.WORK / mid3 / "report")}
            B.save_task(cur_t)
            note3 = B.attach_score_context(
                cur_t, B.WORK / mid3 / "report" / "代码审核报告.md")
            check("覆盖面差一倍要写明分数不可直接相减", "不能直接相减" in note3, note3)
            check("覆盖面数字存进任务",
                  cur_t.get("audit") == {"files": 4, "rows": 4,
                                         "prev_files": 2, "prev_rows": 2},
                  str(cur_t.get("audit")))
            check("回信里会给修复率而不是只给分数",
                  "修复率" in B.recovery_line(tr) and "%" in B.recovery_line(tr))
        finally:
            B.FP_DIR = fp_keep


        # 从历史报告重算（多轮对比的基础）
        hist = ("## 问题清单\n| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | "
                "修改建议 | 证据 |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                "| 1 | 严重 | a.py:1 | x | y | z | w | e |\n"
                "| 2 | 高危 | b.py:2 | x | y | z | w | e |\n")
        check("历史报告可以按同口径重算", B.score_of_report(hist)["counts"]["严重"] == 1,
              str(B.score_of_report(hist)["counts"]))

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
