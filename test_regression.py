#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多轮闭环审核（回归核对）的验证

需求：
  1. 同一个项目第二次送审时，能认出「上一轮」并把旧问题当待核对项
  2. 旧问题逐条判定「已修复 / 部分修复 / 未修复 / 无法确认」，本轮新增问题单独列
  3. 闭环数字由报告表格**确定性**解析，不采信模型自述
  4. 认不出项目 / 没有上一轮时，行为与改造前完全一致（不能把两个项目串起来）

这里验证：
  A. 项目标识与轮次解析（主题千变万化也要归一）
  B. 基线选择（同项目 + 已回复 + 报告存在 + 时间更早）
  C. 问题清单 / 上轮跟踪表的确定性解析
  D. 提示词注入（单批 / 分批 / 汇总 / pi 后端）+ 表格列规范
  E. 回信摘要与状态展示
  F. 无基线时不改变原有行为
"""
import io
import shutil
import sys
import tempfile
import types
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

TMP = Path(tempfile.mkdtemp(prefix="regr_"))
B.TASKS = TMP / "tasks"
B.WORK = TMP / "work"
B.TASKS.mkdir(parents=True, exist_ok=True)
B.WORK.mkdir(parents=True, exist_ok=True)


def mk_task(mid, subject, ts, state="REPLIED", with_report=True, report_text=None):
    t = {"message_id": mid, "subject": subject, "state": state,
         "created_at_ts": ts, "attempts": 1, "project_key": B.project_key(subject),
         "review_round": B.round_of(subject) or 1}
    if with_report:
        rpt = B.WORK / mid / "report"
        rpt.mkdir(parents=True, exist_ok=True)
        (rpt / "代码审核报告.md").write_text(
            report_text or ISSUE_REPORT, encoding="utf-8")
        t["report"] = str(rpt)
    B.save_task(t)
    return t


ISSUE_REPORT = """# 代码审核报告

## 问题清单

| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 严重 | app/main.py:120 | 下单未校验金额，负数可提交 | 校验分散在多处，入口没拦 | 可被刷负单套利 | 入口统一校验 |
| 2 | 高危 | app/api.py:40 | SQL 字符串拼接 | 缺统一的查询构造 | 注入可读全库 | 改用参数化 |
| 3 | 中危 | app/cache.py:12 | 缓存无过期时间 | 只在写入处设值 | 内存持续增长 | 加 TTL |
"""

TRACK_REPORT = ISSUE_REPORT + """
## 上轮问题跟踪

| 编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 1 | 严重 | 下单未校验金额 | **已修复** | app/main.py:118 | 入口已加校验 |
| 2 | 高危 | SQL 字符串拼接 | 部分修复 | app/api.py:41 | 只改了一处 |
| 3 | 中危 | 缓存无过期时间 | 未修复 | app/cache.py:12 | 代码未变 |
"""


def rendered(tpl, **kw):
    base = dict(src="S", rpt="R", extra="", sections=B.spec_sections_text(),
                count=1, idx=1, total=2, file_count=1, chunk_reports="CH",
                files_path="P", chunks_path="P", files_content="FC", filelist="FL",
                baseline="", methodology=B.load_methodology(True),
                issue_columns=B.issue_columns_text(), cross_context="")
    base.update(kw)
    return tpl.format(**base)


def main():
    # ---------- A. 项目标识 / 轮次 ----------
    same = [
        ("代码审核 - order-service 服务端项目", "order service 服务端项目"),
        ("代码审核 - order-service 服务端项目（第2轮）", "order service 服务端项目"),
        ("Re: 代码审核 - order-service 服务端项目（第2轮）", "order service 服务端项目"),
        ("FW: 代码审核-ORDER-SERVICE 服务端项目 第3轮", "order service 服务端项目"),
        ("代码审核 - order-service 服务端项目 (round 4)", "order service 服务端项目"),
        ("代码审核 - order-service 服务端项目-v2", "order service 服务端项目"),
    ]
    for subj, want in same:
        got = B.project_key(subj)
        check(f"主题归一 -> {want!r}: {subj[:28]}", got == want, got)
    check("关键词为空时不产生项目标识", B.project_key("代码审核") == "",
          B.project_key("代码审核"))
    check("不同项目不会归一到一起",
          B.project_key("代码审核 - websec") != B.project_key("代码审核 - demo-shop"))
    check("轮次解析：阿拉伯数字", B.round_of("代码审核 - x（第2轮）") == 2)
    check("轮次解析：中文数字", B.round_of("代码审核 - x（第三轮）") == 3)
    check("轮次解析：round N", B.round_of("代码审核 - x (round 5)") == 5)
    check("轮次解析：没有标记时为 0", B.round_of("代码审核 - x") == 0)
    check("轮次解析带 Re: 前缀", B.round_of("Re: 代码审核 - x（第2轮）") == 2)

    # ---------- B. 基线选择 ----------
    mk_task("msg_r1", "代码审核 - order-service 服务端项目", 1000)
    B.CONFIG["regression_review"] = True
    cur = mk_task("msg_r2", "代码审核 - order-service 服务端项目（第2轮）", 2000,
                  state="SEEN", with_report=False)
    base = B.find_baseline(cur)
    check("同项目上一轮能被找到", base and base["message_id"] == "msg_r1",
          str(base and base["message_id"]))
    check("上一轮轮次解析正确", B.prepare_baseline(cur)["prev_round"] == 1)
    check("本轮轮次取主题里的 2", B.prepare_baseline(cur)["round"] == 2)

    mk_task("msg_o1", "代码审核 - websec", 1500)
    cur_o = mk_task("msg_o2", "代码审核 - websec（第2轮）", 2500, state="SEEN",
                    with_report=False)
    check("不同项目：只认自己的基线",
          (B.find_baseline(cur_o) or {}).get("message_id") == "msg_o1",
          str(B.find_baseline(cur_o) and B.find_baseline(cur_o)["message_id"]))

    pending = mk_task("msg_p1", "代码审核 - 项目A", 3000, state="REVIEWED")
    cur_p = mk_task("msg_p2", "代码审核 - 项目A（第2轮）", 3500, state="SEEN",
                    with_report=False)
    check("上一轮没发出去（REVIEWED）不算基线", B.find_baseline(cur_p) is None,
          str(pending["state"]))

    mk_task("msg_l1", "代码审核 - 项目B", 9000)
    cur_l = mk_task("msg_l2", "代码审核 - 项目B（第2轮）", 5000, state="SEEN",
                    with_report=False)
    check("比本轮更晚的轮次不算基线", B.find_baseline(cur_l) is None)

    norep = mk_task("msg_n1", "代码审核 - 项目C", 1000, with_report=False)
    cur_n = mk_task("msg_n2", "代码审核 - 项目C（第2轮）", 2000, state="SEEN",
                    with_report=False)
    check("报告文件不存在不算基线", B.find_baseline(cur_n) is None,
          str(norep["message_id"]))

    cur_x = mk_task("msg_x2", "代码审核 - 项目D（第2轮）", 2000, state="SEEN",
                    with_report=False)
    mk_task("msg_x1", "代码审核 - 项目D", 1000)
    cur_x["baseline_mid"] = "msg_x1"
    B.save_task(cur_x)
    check("显式 --baseline 生效", (B.find_baseline(cur_x) or {}).get("message_id") == "msg_x1")
    cur_x["regression_skip"] = True
    B.save_task(cur_x)
    check("显式关闭（regression_skip）后不核对", B.find_baseline(cur_x) is None)
    cur_x.pop("regression_skip")
    B.save_task(cur_x)

    B.CONFIG["regression_review"] = False
    check("配置 regression_review=false 时不做回归",
          B.find_baseline(cur) is None)
    B.CONFIG["regression_review"] = True

    # 多条历史 -> 取最近的那一轮
    mk_task("msg_r0", "代码审核 - cycle", 100)
    mk_task("msg_r1b", "代码审核 - cycle", 500)
    last = mk_task("msg_r2b", "代码审核 - cycle（第3轮）", 900, state="SEEN",
                   with_report=False)
    check("多轮历史取最近一轮", (B.find_baseline(last) or {}).get("message_id") == "msg_r1b")

    check("清完后只剩有任务对应的指纹",
          sorted(f.stem for f in B.FP_DIR.glob("*.json"))
          <= sorted(p.stem for p in B.TASKS.glob("*.json")),
          str([f.stem for f in B.FP_DIR.glob("*.json")]))

    # ---------- C2. 主题归一规则（复审能不能匹配上）----------
    same = [
        ("代码审核 - order-service 服务端项目", "order service 服务端项目"),
        ("代码审核 - order-service 服务端项目（第2轮）", "order service 服务端项目"),
        ("代码审核 - order-service 服务端项目 第2轮", "order service 服务端项目"),
        ("代码审核 - order-service 服务端项目（第二版）", "order service 服务端项目"),
        ("代码审核 - order-service 服务端项目 v2", "order service 服务端项目"),
        ("Re: 代码审核 - order-service 服务端项目", "order service 服务端项目"),
    ]
    for subj, want in same:
        got = B.project_key(subj)
        check(f"主题归一 -> {want!r}: {subj[:30]}", got == want, got)
    for subj in ("代码审核 - order-service 服务端项目（修复后重新送审）",
                 "代码审核 - order-service 服务端项目【已修复】",
                 "代码审核 - order-service 服务端项目 - 2026-11-10"):
        got = B.project_key(subj)
        check(f"额外文字会破坏匹配（需提示）: {subj[-14:]}",
              got != "order service 服务端项目", got)

    # ---------- C. 确定性解析 ----------
    rows = B.extract_issue_rows(ISSUE_REPORT)
    check("问题清单解析出行数", len(rows) == 3, str(len(rows)))
    check("解析出级别", rows[0]["level"] == "严重", rows[0]["level"])
    check("解析出文件:行号", rows[0]["loc"] == "app/main.py:120", rows[0]["loc"])
    check("解析出问题描述", "负数" in rows[0]["desc"], rows[0]["desc"])
    check("没有表格时返回空", B.extract_issue_rows("# 空报告\n没有表格") == [])
    esc = ("| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |\n"
           "| --- | --- | --- | --- | --- | --- | --- |\n"
           "| 1 | 中危 | a.py:1 | 管道 a\\|b 被截断 | 成因 | 后果 | 建议 |\n")
    row1 = B.extract_issue_rows(esc)
    check("转义竖线不破坏解析", row1 and "a|b" in row1[0]["desc"], str(row1))

    items = B.baseline_items(B.WORK / "msg_r1" / "report" / "代码审核报告.md")[0]
    check("旧问题转成待核对项", len(items) == 3 and items[0]["no"] == 1)
    check("待核对项带级别与位置",
          items[0]["level"] == "严重" and "main.py" in items[0]["loc"])
    B.CONFIG["regression_max_issues"] = 2
    capped = B.baseline_items(B.WORK / "msg_r1" / "report" / "代码审核报告.md")
    B.CONFIG["regression_max_issues"] = 80
    check("旧问题清单有数量上限", len(capped[0]) == 2 and capped[1] == 3,
          f"{len(capped[0])}/{capped[1]}")

    tr = B.parse_tracking(TRACK_REPORT)
    check("闭环统计总条数", tr["total"] == 3, str(tr))
    check("已修复 / 部分修复 / 未修复 分开统计",
          (tr["fixed"], tr["partial"], tr["unfixed"]) == (1, 1, 1), str(tr))
    check("未修完的项会列出来（部分修复也算）", len(tr["unfixed_items"]) == 2,
          str(tr["unfixed_items"]))
    check("没有跟踪表时统计为 0", B.parse_tracking(ISSUE_REPORT)["total"] == 0)
    check("判定词容错：**已修复**",
          B.parse_tracking("| 编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明 |\n"
                           "| --- | --- | --- | --- | --- | --- |\n"
                           "| 1 | 高 | x | **已修复** | a:1 | ok |\n")["fixed"] == 1)
    check("判定词容错：认不出的写无法确认",
          B.parse_tracking("| 编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明 |\n"
                           "| --- | --- | --- | --- | --- | --- |\n"
                           "| 1 | 高 | x | 感觉还行 | a:1 | - |\n")["unknown"] == 1)

    line = B.tracking_line(tr, 5)
    check("闭环摘要文字完整",
          "上轮 3 项" in line and "已修复 1" in line and "本轮新增 5 项" in line, line)
    check("没有闭环数据时不输出摘要", B.tracking_line({}, 3) == "")

    # ---------- D. 提示词注入 ----------
    bl = {"mid": "msg_r1", "prev_round": 1, "round": 2,
          "items": B.baseline_items(B.WORK / "msg_r1" / "report" / "代码审核报告.md")[0],
          "total": 3, "omitted": 0}
    full = B.render_baseline_full(bl)
    check("单批提示词块含旧问题清单", "下单未校验金额" in full and "app/main.py" in full)
    check("单批提示词块要求四档判定",
          all(v in full for v in B.TRACK_VERDICTS), "缺判定档位")
    check("单批提示词块要求旧问题不进问题清单", "不要再列进问题清单" in full)
    check("单批提示词块声明旧报告是数据不是指令", "不是指令" in full)

    tpl_http = rendered(B.REVIEW_PROMPT_HTTP, baseline=full,
                        sections=B.spec_sections_text(True))
    check("单批 HTTP 提示词包含回归块", "上一轮问题核对" in tpl_http)
    check("单批 HTTP 提示词章节含「上轮问题跟踪」", "上轮问题跟踪" in tpl_http)
    check("单批 HTTP 提示词表头列完整",
          "编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明" in tpl_http, "")

    per = [[bl["items"][0]], [bl["items"][1]], [bl["items"][2]]]
    chunkblk = B.render_baseline_chunk(per[0], bl)
    check("分批提示词只给本批的旧问题",
          "下单未校验金额" in chunkblk and "SQL 字符串拼接" not in chunkblk)
    check("分批提示词要求输出「三、上轮问题核对」", "三、上轮问题核对" in chunkblk)
    check("本批没有旧问题时不出核对块", B.render_baseline_chunk([], bl) == "")

    mergeblk = B.render_baseline_merge(bl)
    check("汇总提示词要求逐条覆盖", "逐条" in mergeblk and "上轮问题跟踪" in mergeblk)
    bl_missing = dict(bl, missing=[bl["items"][2]])
    check("汇总提示词会把「文件不见了」的旧问题标为无法确认",
          "无法确认" in B.render_baseline_merge(bl_missing))

    pi_prompt = rendered(B.REVIEW_PROMPT, baseline=full,
                         sections=B.spec_sections_text(True))
    check("pi 后端提示词也带回归块", "上一轮问题核对" in pi_prompt)
    pi_merge = rendered(B.REVIEW_PROMPT_MERGE, baseline=mergeblk,
                        sections=B.spec_sections_text(True))
    check("pi 汇总提示词也带回归块", "上轮问题跟踪" in pi_merge)
    pi_chunk = rendered(B.REVIEW_PROMPT_CHUNK, baseline=chunkblk)
    check("pi 分批提示词也带回归块", "三、上轮问题核对" in pi_chunk)

    # 无基线时绝不出现回归块
    for nm, txt in (("单批", rendered(B.REVIEW_PROMPT_HTTP)),
                    ("分批", rendered(B.REVIEW_PROMPT_HTTP_CHUNK)),
                    ("汇总", rendered(B.REVIEW_PROMPT_HTTP_MERGE)),
                    ("pi 单批", rendered(B.REVIEW_PROMPT))):
        check(f"{nm}提示词无基线时不出现回归块", "上一轮问题核对" not in txt
              and "上轮问题跟踪" not in txt)

    # 提示词不得泄露来源（回归块里也不能出现方法论/工具名）
    for w in B.forbidden_words():
        if w in full or w in mergeblk or w in chunkblk:
            check(f"回归提示词不含来源名「{w}」", False)
            break
    else:
        check("回归提示词不含任何来源名", True)

    # ---------- 报告规范 / 校验器 ----------
    titles = B.spec_sections_text(True)
    check("有基线时章节规范含「上轮问题跟踪」", "上轮问题跟踪" in titles)
    check("无基线时不要求「上轮问题跟踪」", "上轮问题跟踪" not in B.spec_sections_text(False))
    check("跟踪表列定义在模板里（改模板即改行为）",
          (B.REPORT_SPEC.get("sections_when_baseline") or [{}])[0].get("columns")
          == B.TRACK_COLUMNS)

    good = ("# 代码审核报告\n\n## 本次审核要求\n无，按标准审核流程执行。\n\n"
            "## 总体评价\n订单服务，Python + FastAPI，约 1200 行。整体质量评分：72 分。"
            + "评分依据为入口校验完整度与并发处理。" * 8 + "\n\n"
            "## 问题清单\n| 编号 | 级别 | 文件:行号 | 问题描述 | 成因 | 后果 | 修改建议 | 证据 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| 1 | 严重 | app/main.py:120 | 未校验金额 | 校验分散 | 可刷负单 | 入口统一校验 |\n\n"
            "## 严重与高危问题详述\n见上。\n\n## 安全性专项检查\n未发现命令注入。\n\n"
            "## 架构与可维护性\n分层清晰。\n\n## 测试与工程化\n缺回归用例。\n\n"
            "## 结论与放行建议\n修改后放行。\n")
    check("无基线：合规报告通过校验", B.validate_report(good, 1, False) == [],
          str(B.validate_report(good, 1, False)))
    issues_nb = B.validate_report(good, 1, True)
    check("有基线但缺跟踪表 -> 判不合规",
          any("上轮问题跟踪" in i for i in issues_nb), str(issues_nb))
    good2 = good.replace("\n## 问题清单", "## 上轮问题跟踪\n\n"
                         "| 编号 | 级别 | 上轮问题 | 本轮判定 | 证据 | 说明 |\n"
                         "| --- | --- | --- | --- | --- | --- |\n"
                         "| 1 | 严重 | 未校验金额 | 已修复 | app/main.py:118 | 已加校验 |\n\n"
                         "## 问题清单", 1)
    check("有基线且跟踪表齐全 -> 通过", B.validate_report(good2, 1, True) == [],
          str(B.validate_report(good2, 1, True)))
    bad_cols = good2.replace("| 本轮判定 |", "| 结果 |")
    check("跟踪表列名不对 -> 判不合规",
          any("上轮问题跟踪" in i for i in B.validate_report(bad_cols, 1, True)))

    # ---------- E. 分批归属 ----------
    src = TMP / "src"
    (src / "app").mkdir(parents=True, exist_ok=True)
    for f in ("app/main.py", "app/api.py", "app/cache.py"):
        (src / f).write_text("x = 1\n", encoding="utf-8")
    batches = [[src / "app" / "main.py"], [src / "app" / "api.py", src / "app" / "cache.py"]]
    per_b, missing = B.assign_baseline(bl["items"], batches, src)
    check("旧问题按文件分派到批次", len(per_b[0]) == 1 and len(per_b[1]) == 2,
          str([len(x) for x in per_b]))
    check("文件都在时不产生无法核对的项", missing == [], str(missing))
    gone = B.assign_baseline([{"no": 9, "level": "高", "loc": "app/gone.py:5",
                               "desc": "x"}], batches, src)[1]
    check("旧问题涉及的文件已不存在 -> 归入待确认", len(gone) == 1, str(gone))
    check("issue_file 取路径", B.issue_file("`app/main.py:120-160`") == "app/main.py",
          B.issue_file("`app/main.py:120-160`"))

    # ---------- B2. 内容匹配（不靠邮件标题）----------
    FP_BASE = TMP / "fp_case"
    def _mk_src(dst, files):
        dst.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            p = dst / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return dst

    proj = {f"backend/m{i}.py": f"def f{i}():\n    return {i}\n" for i in range(15)}
    proj.update({"README.md": "说明\n",
                 "package.json": "{}"})
    src_a = _mk_src(FP_BASE / "a", proj)

    # 同一份代码：外层目录变了 + 改了 2 个文件 + **标题完全不同**
    proj2 = dict(proj)
    proj2["backend/m0.py"] = "def f0():\n    return 99  # fixed\n"
    proj2["backend/m1.py"] = "def f1():\n    return 98  # fixed\n"
    src_b = _mk_src(FP_BASE / "b" / "outer-v2", proj2)

    fp_a = B.fingerprint_src(src_a)
    fp_b = B.fingerprint_src(src_b)
    sim, detail = B.content_similarity(fp_a, fp_b)
    check("同一份代码改两处 -> 相似度很高", sim > 0.7, f"sim={sim:.2f} {detail}")
    check("外壳目录不同不影响路径比对", detail.get("path") == 1.0, str(detail))

    other = {f"src/svc{i}.java": f"class S{i} {{}}\n" for i in range(10)}
    src_c = _mk_src(FP_BASE / "c", other)
    sim2, _ = B.content_similarity(B.fingerprint_src(src_c), fp_a)
    check("不同项目 -> 相似度接近 0", sim2 < 0.2, f"{sim2:.2f}")

    # 走完整流程：不同标题也能认出上一轮（用独立任务目录，别影响前面的用例）
    fp_tasks = TMP / "fp_tasks"
    fp_tasks.mkdir(parents=True, exist_ok=True)
    _old_tasks, _old_fp = B.TASKS, B.FP_DIR
    B.TASKS = fp_tasks
    B.FP_DIR = fp_tasks / "fp"
    B.FP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        mk_task("msg_fp1", "代码审核 - 完全不同的标题 A", 1000,
                state="REPLIED", with_report=True)
        B.save_fp("msg_fp1", fp_a)
        cur_fp = mk_task("msg_fp2", "代码审核 - 标题随便写 (第2轮) whatever", 2000,
                      state="SEEN", with_report=False)
        got = B.find_baseline_by_content(cur_fp, src_b)
        check("内容匹配能认出不同标题的上一轮", got and got[0]["message_id"] == "msg_fp1",
              str(got[0] and got[0]["message_id"]))
        check("内容匹配带相似度说明", (got[1] or {}).get("via") == "content", str(got[1]))
        got2 = B.find_baseline_by_content(cur_fp, src_c)
        check("不同项目不会被内容匹配误认", got2[0] is None, str(got2[1]))

        # prepare_baseline 优先内容、再兜底主题
        bl = B.prepare_baseline(cur_fp, src=src_b)
        m = (bl or {}).get("match") or {}
        check("prepare_baseline 走内容匹配", m.get("via") == "content", str(m))
        check("prepare_baseline 拿到上一轮清单", (bl or {}).get("total") == 3,
              str((bl or {}).get("total")))
        # 指纹不能无限堆积：任务没了，指纹也要跟着清
        # （必须在临时 FP_DIR 里验，别写到真实 state/fp）
        (B.FP_DIR / "msg_gone.json").write_text("{}", encoding="utf-8")
        check("孤儿指纹会被清掉", B.prune_orphan_fps() >= 1,
              str(sorted(f.stem for f in B.FP_DIR.glob("*.json"))))
        check("被清掉的是孤儿，不是活任务的指纹",
              not (B.FP_DIR / "msg_gone.json").exists()
              and (B.FP_DIR / "msg_fp1.json").exists(),
              str(sorted(f.stem for f in B.FP_DIR.glob("*.json"))))
    finally:
        B.TASKS, B.FP_DIR = _old_tasks, _old_fp

    # ---------- F. 回信摘要 / 行为不变 ----------
    rpt_dir = B.WORK / "msg_r1" / "report"
    (rpt_dir / "summary.md").write_text("本轮共 5 项问题。", encoding="utf-8")
    t_done = dict(B.load_task("msg_r1"))
    t_done["tracking"] = B.parse_tracking(TRACK_REPORT)
    t_done["new_issues"] = 5
    t_done["baseline_round"] = 1
    body = B.build_body(t_done, rpt_dir, [rpt_dir / "summary.md"])
    check("回信含「上轮问题跟踪」摘要", "上轮问题跟踪" in body, body[-500:])
    check("回信给出闭环数字", "已修复 1" in body and "本轮新增 5 项" in body)
    check("回信列出未修完的项", "SQL 拼接" in body or "缓存无过期" in body)
    t_plain = {"subject": "代码审核 - x", "from_name": "a"}
    check("没有闭环数据时回信不含该段",
          "上轮问题跟踪" not in B.build_body(t_plain, rpt_dir, []))

    check("prepare_baseline 提取完整", (lambda b: b["total"] == 3
                                        and b["prev_round"] == 1
                                        and b["round"] == 2)(B.prepare_baseline(cur)))
    check("没有基线时 prepare_baseline 返回 None",
          B.prepare_baseline(mk_task("msg_solo", "代码审核 - 独立项目", 10,
                                     state="SEEN", with_report=False)) is None)

    # --baseline - 关闭
    B.apply_baseline_choice("msg_r2", "-")
    check("CLI --baseline - 关闭回归核对", B.load_task("msg_r2").get("regression_skip") is True)
    B.apply_baseline_choice("msg_r2", "msg_r0")
    t2 = B.load_task("msg_r2")
    check("CLI --baseline <id> 指定基线",
          t2.get("baseline_mid") == "msg_r0" and not t2.get("regression_skip"))


    # ---------- H. 分数回填：历史报告也要能用同一把尺比 ----------
    def _quiet(fn, *a, **kw):
        """cmd_score 会打印明细，测试里别把输出搅乱"""
        buf = io.StringIO()
        try:
            with_stdout = __import__("contextlib").redirect_stdout(buf)
            with with_stdout:
                return fn(*a, **kw), buf.getvalue()
        except SystemExit:
            return None, buf.getvalue()

    mk_task("msg_sc1", "代码审核 - 评分回填", 5000)     # 报告里有 严重/高危/中危 各 1 条
    _out, printed = _quiet(B.cmd_score, types.SimpleNamespace(id="msg_sc1"))
    t1 = B.load_task("msg_sc1")
    det = t1.get("score_detail") or {}
    check("cmd_score 把分数写回任务", isinstance(t1.get("score"), int),
          str(t1.get("score")))
    check("分数带口径版本（多轮才能判断是否可比）",
          det.get("version") == B.scoring_version(), str(det))
    check("级别分布被记下来", det.get("counts", {}).get("严重") == 1, str(det.get("counts")))
    check("有严重问题 -> 档位是不建议放行", det.get("verdict") == "不建议放行",
          str(det.get("verdict")))
    check("重算结果与直接算一致",
          t1["score"] == B.compute_score(B.extract_issue_rows(
              (B.WORK / "msg_sc1" / "report" / "代码审核报告.md")
              .read_text(encoding="utf-8")))["score"], str(t1["score"]))

    # 报告格式坏了：不能因为「解析不出问题」就发满分
    mk_task("msg_sc_bad", "代码审核 - 坏报告", 5001,
            report_text="# 代码审核报告\n\n（清单缺失）\n")
    _quiet(B.cmd_score, types.SimpleNamespace(id="msg_sc_bad"))
    check("解析不出问题行时不写满分",
          B.load_task("msg_sc_bad").get("score") is None,
          str(B.load_task("msg_sc_bad").get("score")))

    # 上一轮也按同口径重算，否则「上轮分」还是模型自评分，对比照样没意义
    mk_task("msg_sc_base", "代码审核 - 基线项目", 4000)
    t2 = mk_task("msg_sc2", "代码审核 - 基线项目（第2轮）", 5100)
    t2["baseline_mid"] = "msg_sc_base"
    B.save_task(t2)
    _quiet(B.cmd_score, types.SimpleNamespace(id="msg_sc2"))
    check("回填时一并重算上一轮的分数",
          isinstance(B.load_task("msg_sc2").get("score_prev"), int),
          str(B.load_task("msg_sc2").get("score_prev")))
    check("两轮口径版本一致",
          (B.load_task("msg_sc2").get("score_detail") or {}).get("version")
          == B.scoring_version())

    # 全量模式：不给 --id 就扫所有有报告的任务，且不会因个别坏报告中断
    rc, out = _quiet(B.cmd_score, types.SimpleNamespace(id=None))
    check("全量回填返回 0（不被坏报告打断）", rc == 0, str(rc))
    check("全量回填会跳过并说明坏报告", "跳过" in out, out[-200:])
    check("全量回填后旧任务也有分数",
          isinstance(B.load_task("msg_r1").get("score"), int),
          str(B.load_task("msg_r1").get("score")))

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
