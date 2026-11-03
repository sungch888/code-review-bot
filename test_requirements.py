"""审核要求解析的单元验证：python test_requirements.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bot  # noqa: E402


def t(name, body, expect_in, expect_not_in):
    got = []
    for m in bot.REQ_SECTION_PAT.finditer(body):
        seg = m.group(1)
        em = bot.REQ_END_PAT.search(seg)
        if em:
            seg = seg[:em.start()]
        got.append(seg.strip())
    joined = "\n".join(got)
    ok = all(e in joined for e in expect_in) and all(e not in joined for e in expect_not_in)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print("   解析结果:", repr(joined))
    return ok


results = []
results.append(t(
    "显式结束标记：标记之后的内容不吸入",
    "请审核。\n\n【审核要求】\n1. 关注并发\n2. 参数化 SQL\n\n【/审核要求】\n普通说明：这段不应被吸入。\n以上。\n",
    ["关注并发", "参数化 SQL"],
    ["普通说明", "以上"],
))
results.append(t(
    "结束标记必须在区块之前才生效",
    "【审核要求】\n1. A项\n普通说明文字在区块内\n【/审核要求】\n",
    ["A项", "普通说明文字在区块内"],
    [],
))
results.append(t(
    "无结束标记时取到正文末尾",
    "【审核要求】\n1. 只看安全\n",
    ["只看安全"],
    [],
))
results.append(t(
    "支持【审核要点】别名",
    "【审核要点】\n- 关注性能\n",
    ["关注性能"],
    [],
))
results.append(t(
    "正文无要求区块时不误报",
    "这封邮件没有任何要求区块。\n",
    [],
    [],
))
results.append(t(
    "多个区块分别提取",
    "【审核要求】\nA项\n【审核要点】\nB项\n",
    ["A项", "B项"],
    [],
))
results.append(t(
    "长度截断保护",
    "【审核要求】\n" + ("x" * 9000),
    ["x" * 100],
    [],
))

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
