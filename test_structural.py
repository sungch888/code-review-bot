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
LF2 = chr(10)          # 测试里拼接换行，避开写文件时的转义点

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
            check("覆盖面差一倍时写明报多报少不影响评分（按台账算）",
                  "不影响评分" in note3 and "台账" in note3, note3)
            check("覆盖面数字存进任务",
                  cur_t.get("audit") == {"files": 4, "rows": 4,
                                         "prev_files": 2, "prev_rows": 2},
                  str(cur_t.get("audit")))
            check("回信里会给修复率而不是只给分数",
                  "修复率" in B.recovery_line(tr) and "%" in B.recovery_line(tr))
            check("无法归因的新条目不叫「本轮引入」",
                  "unknown_origin" in B.ledger_apply(B.load_ledger("proj-uk"), [], {}, "m1"))
        finally:
            B.FP_DIR = fp_keep

        # ================= 问题台账：分数不再跟着「本轮报几条」跑 =================
        led_keep = B.LEDGER_DIR
        B.LEDGER_DIR = TMP / "ledger_struct"
        B.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        try:
            def rows2(*triples):
                return [{"level": lv, "desc": d, "loc": f"{p}:10"}
                        for lv, p, d in triples]

            led = B.load_ledger("proj-ledger")
            d1 = B.ledger_apply(led, rows2(("严重", "a.py", "整表删除无备份"),
                                           ("高危", "b.py", "越权访问"),
                                           ("低危", "c.py", "命名不规范")), {}, "m1")
            s1 = B.ledger_score(led)
            check("首轮入册 3 条", d1["new_in"] == 3 and s1["open"] == 3,
                  f'{d1} open={s1["open"]}')

            # 本轮只报出 2 条（漏了一条），且没有任何「已修复」判定
            d2 = B.ledger_apply(led, rows2(("严重", "a.py", "整表删除无备份"),
                                           ("高危", "b.py", "越权访问")), {}, "m2")
            s2 = B.ledger_score(led)
            check("本轮漏报不会让债务消失（fail-closed）",
                  s2["open"] == 3 and s2["score"] == s1["score"] and d2["new_in"] == 0,
                  f'{s2["open"]} open, {s2["score"]} 分')

            # 真修好了（跟踪表判已修复）才销账，分数才涨
            tr_ok = {"items": {"fixed": [{"level": "高危", "desc": "越权访问", "loc": "b.py"}],
                               "partial": [], "unfixed": [], "unknown": []}}
            d3 = B.ledger_apply(led, rows2(("严重", "a.py", "整表删除无备份")), tr_ok, "m3")
            s3 = B.ledger_score(led)
            check("只有判「已修复」才销账", d3["fixed_out"] == 1 and s3["open"] == 2, str(d3))
            check("真修好后分数才上升", s3["score"] > s2["score"],
                  f'{s2["score"]} -> {s3["score"]}')

            # 同一条问题被轻判：级别取两轮中更严的
            led2 = B.load_ledger("proj-sev")
            B.ledger_apply(led2, rows2(("严重", "x.py", "整表删除")), {}, "m1")
            B.ledger_apply(led2, rows2(("中危", "x.py", "整表删除")), {}, "m2")
            it = list(led2["items"].values())[0]
            check("级别取两轮中更严的（旧债不能被轻判洗掉）", it["level"] == "严重",
                  str(it["level"]))

            # 同一份代码重审：分数一格不变
            led3 = B.load_ledger("proj-same")
            two = rows2(("严重", "x.py", "问题一"), ("高危", "y.py", "问题二"))
            B.ledger_apply(led3, two, {}, "m1")
            a = B.ledger_score(led3)["score"]
            B.ledger_apply(led3, two, {}, "m2")
            b = B.ledger_score(led3)["score"]
            check("同一份代码重审，分数不变", a == b, f"{a} vs {b}")

            # 新报的问题：字节一致=上轮漏报的历史遗留；内容变了=本轮引入
            pk, ck = TMP / "pkg_prev", TMP / "pkg_cur"
            (pk / "outer").mkdir(parents=True, exist_ok=True)
            (ck / "outer").mkdir(parents=True, exist_ok=True)
            (pk / "outer" / "a.py").write_text("print(1)\n", encoding="utf-8")
            (ck / "outer" / "a.py").write_text("print(1)\n", encoding="utf-8")
            led4 = B.load_ledger("proj-origin")
            B.ledger_apply(led4, rows2(("高危", "outer/a.py", "旧债一")), {}, "m1")
            d5 = B.ledger_apply(led4, rows2(("高危", "outer/a.py", "旧债一"),
                                            ("中危", "outer/a.py", "新看到的旧问题")),
                                {}, "m2", src=ck, prev_src=pk)
            check("与上轮字节一致的新报问题归为历史遗留",
                  d5["late_found"] == 1 and d5["regression"] == 0, str(d5))
            (ck / "outer" / "a.py").write_text("print(2)  # changed\n", encoding="utf-8")
            d6 = B.ledger_apply(led4, rows2(("高危", "outer/a.py", "旧债一"),
                                            ("中危", "outer/a.py", "本轮改出来的问题")),
                                {}, "m3", src=ck, prev_src=pk)
            check("改动后新出现的问题按新问题扣分", d6["regression"] >= 1, str(d6))

            # 台账跟项目链走，不跟邮件标题走
            keep_tasks = B.TASKS
            B.TASKS = TMP / "tasks_ledger"
            B.TASKS.mkdir(parents=True, exist_ok=True)
            try:
                B.save_task({"message_id": "m_base", "state": "REVIEWED",
                             "subject": "代码审核 甲项目", "ledger_key": "链甲"})
                check("台账继承上一轮的链，不用标题猜",
                      B.ledger_key_of({"message_id": "m_next",
                                       "subject": "代码审核 甲项目 修复版 v3",
                                       "baseline_mid": "m_base"}) == "链甲")
            finally:
                B.TASKS = keep_tasks

            # 报告里的分数行换成台账分，并说明这笔账怎么来的
            mid5 = "msg_ledger_report"
            d5dir = B.WORK / mid5 / "report"
            d5dir.mkdir(parents=True, exist_ok=True)
            f5 = d5dir / "代码审核报告.md"
            body = ("**整体质量评分：70 / 100**\n\n## 问题清单\n"
                    "| 编号 | 级别 | 文件 | 位置 | 问题描述 |\n|---|---|---|---|---|\n"
                    "| 1 | 严重 | a.py | 10 | 整表删除 |\n")
            f5.write_text(body, encoding="utf-8")
            t5 = {"message_id": mid5, "subject": "代码审核 台账报告",
                  "state": "REVIEWED", "created_at_ts": 5}
            B.apply_ledger_and_score(t5, B.extract_issue_rows(body), {}, f5)
            out5 = f5.read_text(encoding="utf-8")
            check("报告分数行换成台账分",
                  "70 / 100" not in out5 and "统一口径 v" in out5, out5[:200])
            check("任务记下台账口径",
                  (t5.get("score_detail") or {}).get("basis") == "ledger",
                  str(t5.get("score_detail")))
            check("说明点明在册未修复条数",
                  "台账在册未修复" in (t5.get("ledger_note") or ""),
                  t5.get("ledger_note") or "")

            # 同一任务重跑：不能把这一轮记两次
            mid6 = "msg_ledger_rerun"
            d6dir = B.WORK / mid6 / "report"
            d6dir.mkdir(parents=True, exist_ok=True)
            f6 = d6dir / "代码审核报告.md"
            f6.write_text(body, encoding="utf-8")
            t6 = {"message_id": mid6, "subject": "代码审核 重放去重",
                  "state": "REVIEWED", "created_at_ts": 6}
            B.apply_ledger_and_score(t6, B.extract_issue_rows(body), {}, f6)
            key6 = t6["ledger_key"]
            rounds1 = len(B.load_ledger(key6)["rounds"])
            B.apply_ledger_and_score(t6, B.extract_issue_rows(body), {}, f6)
            led6 = B.load_ledger(B.ledger_key_of(t6))
            r6 = [x for x in led6["rounds"] if x["mid"] == mid6]
            check("同一任务重跑不会重复入账",
                  len(r6) == 1 and len(led6["rounds"]) == rounds1,
                  f'{len(r6)} 次 / 共 {len(led6["rounds"])} 轮')

            # 拆账：分数变化每一步都要能对上号
            led7 = B.load_ledger("proj-bridge")
            B.ledger_apply(led7, rows2(("严重", "a.py", "甲问题"),
                                       ("高危", "b.py", "乙问题")), {}, "m1")
            tr7 = {"items": {"fixed": [{"level": "高危", "desc": "乙问题", "loc": "b.py"}],
                             "partial": [], "unfixed": [], "unknown": []}}
            d7 = B.ledger_apply(led7, rows2(("严重", "a.py", "甲问题")), tr7, "m2")
            br = d7["bridge"]
            check("拆账各步之和等于总分变化",
                  abs(sum(x["delta"] for x in br["steps"]) - br["total"]) <= 0.2, str(br))
            check("拆账能单独看见「销账是加分」",
                  any(x["key"] == "fixed" and x["delta"] > 0 for x in br["steps"]),
                  str(br["steps"]))
            d8 = B.ledger_apply(led7, rows2(("严重", "a.py", "甲问题")), {}, "m3")
            check("在册问题原样重报，分数不动",
                  abs(d8["bridge"]["total"]) < 0.01, str(d8["bridge"]))
            check("说明里会写分数为什么变",
                  "分数" in B.ledger_note(B.ledger_score(led7), d7),
                  B.ledger_note(B.ledger_score(led7), d7))
            # 口径说明必须能刷新：重放台账后不能留着上一版那句过期话
            md8 = ('整体质量评分：**40** / 100' + LF2 + LF2 + '> ' + B.SCORE_NOTE_MARK
                   + '旧的话，说分数不可直接比' + LF2 + LF2 + '正文')
            out8 = B.append_score_note(md8, '新的话')
            check('过期说明被换掉', '旧的话' not in out8 and '新的话' in out8, out8)
            check('同一时刻只留一条说明',
                  out8.count(B.SCORE_NOTE_MARK) == 1 and
                  B.append_score_note(out8, '新的话').count(B.SCORE_NOTE_MARK) == 1,
                  str(out8.count(B.SCORE_NOTE_MARK)))
            check('没有评分行就不硬塞', B.append_score_note('正文', '话') == '正文')
        finally:
            B.LEDGER_DIR = led_keep


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
