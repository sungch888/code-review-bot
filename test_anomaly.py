"""异常回信的回归测试：python test_anomaly.py

锁住真实踩过的坑：
  **把「附件没有 attachment_id」当成「附件超过 20MB」** ->
  把一个 363KB 的代码包误报成超大附件，给了完全错误的指引。
  现在只依据 size 字段判断大小，并且每封异常回信都必须给出可操作的处理办法。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import anomaly as A  # noqa: E402

results = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))
    results.append(cond)


# ---------- 1. size 解析与展示 ----------
check("size 解析正常", A.parse_size({"size": "371907"}) == 371907)
check("size 缺失不炸", A.parse_size({}) == 0)
check("size 非法不炸", A.parse_size({"size": "abc"}) == 0)
check("363KB 展示为 KB", "KB" in A.human_size(371907), A.human_size(371907))
check("25MB 展示为 MB", "MB" in A.human_size(25 * 1024 * 1024), A.human_size(25 * 1024 * 1024))
check("0 大小显示未知", A.human_size(0) == "未知大小")

# ---------- 2. 关键：363KB 的「仅直链」附件不能被说成超过 20MB ----------
small = [("websec-main.zip", 371907, "HTTP 400 Bad Request")]
body = A.reply_link_only("代码审核-websec", small)
check("363KB 附件不被说成超过 20MB", "超过了邮箱的 20MB 上限" not in body, body[:200])
check("363KB 附件不被说成 20MB 字样", "20MB 上限" not in body, body[:200])
check("说明是链接形式送达", "链接形式" in body, body[:200])
check("列出实际文件名与大小", "websec-main.zip" in body and "KB" in body, "")
check("给出可操作办法（换普通附件）", "换成普通附件重发" in body, "")
check("给出可操作办法（网盘直链）", "直链" in body, "")
check("给出可操作办法（贴代码）", "直接贴代码" in body, "")

# ---------- 3. 真超过 20MB 时才这么说 ----------
big = [("huge.zip", 30 * 1024 * 1024, "HTTP 400")]
bbody = A.reply_link_only("代码审核-大包", big)
check("真超 20MB 时才说超限", "超过了邮箱的 20MB 上限" in bbody, bbody[:200])
check("超限时给出分卷方案", "分卷发送" in bbody, "")
check("超限时给出网盘方案", "网盘链接" in bbody, "")
check("超限时建议精简依赖", "node_modules" in bbody, "")

# ---------- 4. 其它异常场景都要有可操作建议 ----------
nb = A.reply_no_attachment("代码审核-X")
check("无附件 -> 有处理办法", "重新发送" in nb and "附件" in nb, "")
check("无附件 -> 提到打包", "zip" in nb, "")

df = A.reply_download_failed("代码审核-X", [("a.zip", "network error")])
check("下载失败 -> 列出失败项", "a.zip" in df, "")
check("下载失败 -> 有处理办法", "重新发送" in df, "")

ef = A.reply_extract_failed("代码审核-X", [("b.zip", "File is not a zip file")])
check("解压失败 -> 列出失败项", "b.zip" in ef, "")
check("解压失败 -> 提到加密", "密码" in ef, "")
check("解压失败 -> 提到未加密 zip", "未加密" in ef, "")

ns = A.reply_no_source("代码审核-X")
check("无源码 -> 有处理办法", "源码文件" in ns, "")
check("无源码 -> 提到排除依赖目录", "node_modules" in ns, "")

# ---------- 5. 审核彻底失败的回信（发件人不能石沉大海）----------
rf = A.reply_review_failed("代码审核-X", "503 service_unavailable", 3)
check("失败回信 -> 区分上游繁忙", "大模型服务当前过载" in rf, rf[:200])
check("失败回信 -> 说明不是代码问题", "不是你的代码问题" in rf, "")
check("失败回信 -> 给出处理办法", "处理办法" in rf, "")
check("失败回信 -> 说明已重试次数", "3 次" in rf, "")
check("失败回信 -> 告知保留代码包", "我会保留" in rf, "")

rt = A.reply_review_failed("代码审核-X", "审核超时(2400s)", 3)
check("超时回信 -> 区分超时", "超时" in rt, rt[:160])
check("超时回信 -> 建议精简或分批", "精简" in rt or "分批" in rt, "")

rg = A.reply_review_failed("代码审核-X", "some other error", 1)
check("其它错误回信 -> 给出通用处理办法", "处理办法" in rg, "")
check("其它错误回信 -> 包含原始错误", "some other error" in rg, "")

# ---------- 6. 报告瑕疵说明 ----------
rw = A.reply_report_warning("代码审核-X", ["报告结构仍不完整：缺少章节 X"])
check("瑕疵说明 -> 列出瑕疵", "缺少章节 X" in rw, "")
check("瑕疵说明 -> 建议对照代码复核", "复核" in rw, "")

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
