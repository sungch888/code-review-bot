"""分块审核的单元验证：python test_chunk.py

验证：
  1. 超过阈值会切成多块，低于阈值不切
  2. 分块提示词里的文件清单正确（相对路径、逐行）
  3. 合并提示词包含全部批次报告内容
  4. 分块提示词同样要求逐字使用章节标题
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import bot as B  # noqa: E402

results = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))
    results.append(cond)


def split(files, size):
    if size and len(files) > size:
        return [files[i:i + size] for i in range(0, len(files), size)]
    return []


# 1. 分块逻辑
files = [f"/src/f{i}.py" for i in range(102)]
c = split(files, 40)
check("102 个文件 / 阈值 40 -> 切成 3 块", len(c) == 3, str([len(x) for x in c]))
check("块大小不超过阈值", all(len(x) <= 40 for x in c), str([len(x) for x in c]))
check("文件总数不丢失", sum(len(x) for x in c) == 102, "")
check("40 个文件 / 阈值 40 -> 不分块", split(files[:40], 40) == [])
check("阈值 0（关闭分块）-> 不分块", split(files, 0) == [])
check("阈值 40 时 41 个文件 -> 2 块", len(split(files[:41], 40)) == 2)

# 2. 分块提示词
class F:
    """模拟 collect_source_files 返回的 Path"""

    def __init__(self, p):
        self.p = Path(p)

    def relative_to(self, base):
        return self.p.relative_to(Path(base))

    def as_posix(self):
        return self.p.as_posix()


fl = "\n".join(f"- {f.relative_to('/x').as_posix()}"
               for f in [F("/x/src/a.py"), F("/x/src/b.py")])
check("文件清单用相对路径", "- src/a.py" in fl, fl)
cp = B.REVIEW_PROMPT_CHUNK.format(src="/x/src", rpt="/x/rpt", extra="", idx=2, total=3,
                                  baseline="", filelist=fl, sections=B.spec_sections_text())
check("分块提示词含批次序号", "第 2/3 批" in cp, "")
check("分块提示词含文件清单", "- src/a.py" in cp and "- src/b.py" in cp, "")
check("分块提示词要求逐字用章节标题", "逐字使用" in cp, "")
check("分块提示词限定只审本批", "只审上面列出的文件" in cp, "")
check("分块提示词要求写 chunk 文件", "chunk_2_报告.md" in cp, "")
check("分块提示词注入的 extra 生效", cp.count("附加要求") == 0, "空 extra 不应出现附加要求块")

cp2 = B.REVIEW_PROMPT_CHUNK.format(src="/x/src", rpt="/x/rpt", extra="## 附加要求\n关注并发",
                                   idx=1, total=1, baseline="",
                                   filelist=fl, sections=B.spec_sections_text())
check("分块提示词带上了额外要求", "关注并发" in cp2, "")

# 3. 合并提示词
mp = B.REVIEW_PROMPT_MERGE.format(src="/x/src", rpt="/x/rpt",
                                  chunks_path="/x/rpt/_all_chunks.md", baseline="",
                                  sections=B.spec_sections_text())
check("合并提示词给出分批报告文件路径", "_all_chunks.md" in mp, "")
check("合并提示词要求先 read 该文件", "read 工具" in mp, "")
check("合并提示词要求汇总问题清单并重新编号", "重新连续编号" in mp, "")
check("合并提示词要求合并同根因", "合并为一条" in mp, "")
check("合并提示词要求输出最终两个文件",
      "代码审核报告.md" in mp and "summary.md" in mp, "")
check("合并提示词给出放行档位词", "不建议放行" in mp, "")

# 4. 配置项存在
check("config 有 review_chunk_files", "review_chunk_files" in B.CONFIG,
      str(list(B.CONFIG.keys())[:5]))
check("config 有 fallback_model", "fallback_model" in B.CONFIG, "")
check("config 有 provider_busy_backoff_sec", "provider_busy_backoff_sec" in B.CONFIG, "")

# 5. 长 prompt 必须落文件（Windows 命令行 32767 字符上限 -> WinError 206）
from pathlib import Path as _P
tmp = _P("/tmp/_prompt_test"); tmp.mkdir(parents=True, exist_ok=True)
short = "x" * 100
check("短 prompt 直接内联", B._prompt_arg(short, tmp, "t1") == short, "")
longp = "y" * 50000
arg = B._prompt_arg(longp, tmp, "t2")
check("长 prompt 改为文件传递", len(arg) < 1000, len(arg))
pf = tmp / "_prompt_t2.md"
check("长 prompt 文件已生成", pf.exists(), str(pf))
check("长 prompt 文件内容完整", pf.read_text(encoding="utf-8") == longp, "")
check("长 prompt 提示里带文件路径", str(pf) in arg, arg[:120])
import shutil as _sh; _sh.rmtree(tmp, ignore_errors=True)

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
