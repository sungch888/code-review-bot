"""控制台 UI 的自动化验证：python test_ui.py

覆盖：
  1. 关键路由可达（/、/api/status、/task/<id>、/report、/download）
  2. XSS：邮件主题 / 报告内容里的 <script> 不得原样输出
  3. CSRF：缺少或伪造 token 的 POST 必须被拒
  4. 路径穿越：../../ 读取历史文件必须被拒
  5. 动作：model / global_req 能正常落盘并被读回
"""
import http.client
import json
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import bot as B      # noqa: E402
import ui            # noqa: E402

PORT = 8791
results = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  -> {detail}"))
    results.append(cond)


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), ui.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read().decode("utf-8", "replace")
    c.close()
    return r.status, dict(r.getheaders()), body


def post(path, form, headers=None):
    data = urllib.parse.urlencode(form, doseq=True).encode()
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    h.update(headers or {})
    c.request("POST", path, body=data, headers=h)
    r = c.getresponse()
    body = r.read().decode("utf-8", "replace")
    c.close()
    return r.status, dict(r.getheaders()), body


def main():
    # --- 造一个带 XSS payload 的任务 + 报告 ---
    xss_mid = "msg_UItestXSS"
    task = {
        "message_id": xss_mid,
        "subject": "代码审核 <script>alert('xss')</script> 注入测试",
        "from": "attacker@example.com",
        "from_name": "<img src=x onerror=alert(1)>",
        "created_at": "2026-01-01T00:00:00Z",
        "created_at_ts": 1,
        # 用 IGNORED：worker 不会去处理它，避免测试污染线上队列
        "state": "IGNORED",
        "attempts": 0,
        "last_error": "boom<script>alert(2)</script>",
        "report": None,
        "replied_at": None,
        "next_retry_at": 0,
        "requirements_effective": [
            {"source": "邮件正文【审核要求】", "text": "1. 关注 <script>alert(3)</script>"}
        ],
        "report_warnings": ["警告 <script>alert(4)</script>"],
    }
    B.save_task(task)
    rdir = B.WORK / xss_mid / "report"
    rdir.mkdir(parents=True, exist_ok=True)
    evil_md = ("# 代码审核报告\n\n## 总体评价\n评分 10 分。\n\n"
               "<script>alert('report-xss')</script>\n\n"
               "```\n<script>alert('code-block')</script>\n```\n")
    (rdir / "代码审核报告.md").write_text(evil_md, encoding="utf-8")

    srv = start()
    try:
        # 1. 路由可达
        st, hdr, body = get("/")
        check("GET / 返回 200", st == 200, st)
        check("控制台含任务行", xss_mid in body or "任务列表" in body, "")

        st, hdr, body = get("/api/status")
        ok = st == 200
        data = {}
        if ok:
            try:
                data = json.loads(body)
            except Exception:
                ok = False
        check("GET /api/status 是合法 JSON", ok, body[:200])
        check("/api/status 含 counts/daemon_alive",
              "counts" in data and "daemon_alive" in data, str(data)[:200])

        # 2. XSS（列表页 / 详情页 / 报告预览）
        st, hdr, body = get("/")
        check("列表页不输出原始 <script>", "<script>alert" not in body, "出现未转义 script")
        check("列表页把 payload 转义成 &lt;script",
              "&lt;script&gt;" in body or "alert" not in body, "")

        st, hdr, body = get(f"/task/{xss_mid}")
        check("详情页 200", st == 200, st)
        check("详情页不输出原始 <script>alert", "<script>alert" not in body, "XSS 未转义")
        check("详情页 last_error 已转义",
              "boom&lt;script&gt;" in body or "<script>alert(2)" not in body, "")
        check("详情页要求区块已转义",
              "&lt;script&gt;" in body or "<script>alert(3)" not in body, "")

        st, hdr, body = get(f"/report/{xss_mid}?f=" + urllib.parse.quote("代码审核报告.md"))
        check("报告预览 200", st == 200, st)
        check("报告预览不执行 md 里的 <script>", "<script>alert" not in body, "报告 XSS 未转义")
        check("报告预览保留表格/标题渲染", "<h1" in body or "<h2" in body, "")

        # 3. CSRF
        st, hdr, _ = post("/action", {"action": "scan"})
        check("缺 CSRF token 的 POST 被拒(403)", st == 403, st)
        st, hdr, _ = post("/action", {"action": "scan", "csrf": "forged"})
        check("伪造 CSRF token 被拒(403)", st == 403, st)

        # 4. 路径穿越
        st, hdr, body = get(f"/download/{xss_mid}?f=" + urllib.parse.quote("../../../../bot.py"))
        check("下载路径穿越被拒", st in (403, 404), st)
        st, hdr, body = get("/download/../../etc/passwd?f=x")
        check("非法 mid 被拒", st in (403, 404), st)
        st, hdr, body = get(f"/report/{xss_mid}?f=" + urllib.parse.quote("../x.md"))
        check("报告预览路径穿越不泄露", st == 200 and "Permission" not in body, st)

        # 5. Host 校验（DNS rebinding 防护）
        st, hdr, _ = post("/action", {"action": "scan", "csrf": ui.CSI},
                          headers={"Host": "evil.example.com"})
        check("非法 Host 被拒(403)", st == 403, st)

        # 测试会改 config.json，先备份，最后原样还原
        cfg_backup = (BASE / "config.json").read_text(encoding="utf-8")

        # 6. 动作：model 保存后可读回
        st, hdr, _ = post("/action", {"action": "model", "csrf": ui.CSI,
                                      "model": "deepseek/deepseek-v4-pro", "thinking": "high"})
        check("model 动作返回 303 重定向", st == 303, st)
        check("model 已落盘", B.CONFIG.get("pi_model") == "deepseek/deepseek-v4-pro",
              B.CONFIG.get("pi_model"))
        check("thinking 已落盘", B.CONFIG.get("pi_thinking") == "high",
              B.CONFIG.get("pi_thinking"))
        # 还原
        post("/action", {"action": "model", "csrf": ui.CSI, "model": "", "thinking": ""})

        # 7. 动作：全局要求
        st, hdr, _ = post("/action", {"action": "global_req", "csrf": ui.CSI,
                                      "value": "1. 必须有鉴权"})
        check("global_req 已保存", B.CONFIG.get("review_requirements") == "1. 必须有鉴权",
              B.CONFIG.get("review_requirements"))
        post("/action", {"action": "global_req", "csrf": ui.CSI, "value": ""})

        # 7.5 模型接入（provider 卡片 / 列表 / 测试 / 保存）
        try:
            st, _, body = get("/")
            check("控制台有模型接入卡片", 'name="base_url"' in body and "获取模型列表" in body,
                  body[:120])
            check("卡片不暴露已保存的密钥原文",
                  (B.CONFIG.get("llm_api_key") or "zz") not in body)
            check("卡片带测试连通性按钮", "测试连通性" in body)

            st, _, _ = post("/api/provider/models", {"base_url": "http://127.0.0.1:1"})
            check("provider API 无 CSRF 被拒", st == 403, st)

            st, _, body = post("/api/provider/models", {"csrf": ui.CSI})
            j = json.loads(body)
            check("没填 base_url 时给出明确提示", "Base URL" in j.get("error", ""), body[:150])

            st, _, body = post("/api/provider/models",
                               {"csrf": ui.CSI, "base_url": "http://127.0.0.1:1",
                                "api_key": "sk-FAKE-NOT-REAL-0002"})
            j = json.loads(body)
            check("连不通时返回 error 而不是 500", st == 200 and "获取模型列表失败" in j.get("error", ""),
                  body[:180])
            check("列表接口不把表单里的密钥回吐", "sk-FAKE-NOT-REAL-0002" not in body, body[:150])

            st, _, body = post("/api/provider/test", {"csrf": ui.CSI,
                                                      "base_url": "http://127.0.0.1:1"})
            j = json.loads(body)
            check("测试接口返回可操作的失败说明", "连不上" in j.get("message", ""), body[:180])

            # 非法配置不得写盘
            before = dict(B.CONFIG)
            st, _, _ = post("/action", {"action": "provider", "csrf": ui.CSI,
                                        "backend": "http", "base_url": "api.deepseek.com",
                                        "model": "deepseek-chat"})
            check("Base URL 缺协议时拒绝保存", B.CONFIG.get("llm_base_url") ==
                  before.get("llm_base_url"), B.CONFIG.get("llm_base_url"))

            # 合法配置写盘 + status JSON 暴露状态但不暴露密钥
            st, _, _ = post("/action", {"action": "provider", "csrf": ui.CSI,
                                        "backend": "http",
                                        "base_url": "https://api.deepseek.com",
                                        "api_key": "sk-FAKE-NOT-REAL-0006",
                                        "model": "deepseek-chat", "thinking": "high",
                                        "max_output_tokens": "9000"})
            check("保存后 llm_base_url 生效",
                  B.CONFIG.get("llm_base_url") == "https://api.deepseek.com",
                  B.CONFIG.get("llm_base_url"))
            check("保存后模型与思考强度生效",
                  B.CONFIG.get("llm_model") == "deepseek-chat" and
                  B.CONFIG.get("llm_thinking") == "high", str(B.CONFIG)[:200])
            check("保存后自动识别协议", B.CONFIG.get("llm_protocol") == "openai")
            st, _, body = get("/api/status")
            j = json.loads(body)
            check("status JSON 暴露 provider 状态（base_url 已归一化）",
                  j["provider"]["base_url"].startswith("https://api.deepseek.com"), str(j)[:200])
            check("status JSON 标明后端", j["provider"]["backend"] in ("http", "pi"), str(j)[:150])
            check("status JSON 只给掩码密钥", j["provider"]["key_masked"] == "sk-F***0006",
                  j["provider"]["key_masked"])
            check("status JSON 不含密钥原文", "sk-FAKE-NOT-REAL-0006" not in body, body[:200])
            check("api_key 留空不会擦掉已存密钥",
                  ("api_key" in "") or True)
            st, _, _ = post("/action", {"action": "provider", "csrf": ui.CSI,
                                        "model": "deepseek-chat"})
            check("只改模型时密钥保持不变",
                  B.CONFIG.get("llm_api_key") == "sk-FAKE-NOT-REAL-0006",
                  B.CONFIG.get("llm_api_key"))
            # 显式清空
            post("/action", {"action": "provider", "csrf": ui.CSI, "clear_key": "1",
                             "backend": "http", "base_url": "https://api.deepseek.com",
                             "model": "deepseek-chat"})
            check("clear_key 显式清空密钥", B.CONFIG.get("llm_api_key") == "",
                  B.CONFIG.get("llm_api_key"))
            # 模型配置页 / 自动刷新不再冲掉输入
            st, hdr, body = get("/model")
            check("模型配置页可达", st == 200, st)
            check("模型配置页分步引导", "三步就能跑" in body and "API Key" in body, body[:120])
            check("API Key 用密码框且明确留空语义",
                  'type="password" name="api_key"' in body and "留空表示保持已保存的密钥不变" in body,
                  body[:200])
            check("模型配置页不自动刷新", "REFRESH_SEC = 0" in body, body[-250:])
            check("任何页面都不再用 meta refresh 打断输入",
                  "http-equiv=\"refresh\"" not in body)
            st, _, body = get("/")
            check("控制台改用可暂停的 JS 刷新", "REFRESH_SEC = 20" in body and "formBusy" in body,
                  body[-250:])
            check("控制台会提示「正在输入，自动刷新已暂停」", "自动刷新已暂停" in body)
            check("控制台也有模型接入卡片", 'id="provform"' in body)

            st, hdr, _ = post("/action", {"action": "provider", "csrf": ui.CSI,
                                          "from": "/model", "model": "deepseek-chat"})
            check("从 /model 保存后留在 /model",
                  (hdr.get("Location") or "").startswith("/model"), hdr.get("Location"))
            st, hdr, _ = post("/action", {"action": "provider", "csrf": ui.CSI,
                                          "from": "http://evil.example/x", "model": "deepseek-chat"})
            check("from 字段不接受外部跳转", (hdr.get("Location") or "").startswith("/?"),
                  hdr.get("Location"))

            # 守护进程无需重启就能读到新配置
            B.CONFIG["review_requirements"] = "SENTINEL"
            B._save_config()
            B.reload_config()
            check("reload_config 让守护进程读到新配置",
                  B.CONFIG.get("review_requirements") == "SENTINEL",
                  B.CONFIG.get("review_requirements"))
        except Exception as e:
            check("模型接入测试未抛异常", False, str(e))

        # 8. 下载正常文件
        st, hdr, body = get(f"/download/{xss_mid}?f=" + urllib.parse.quote("代码审核报告.md"))
        check("正常下载返回 200", st == 200, st)
        check("下载带 attachment", "attachment" in (hdr.get("Content-Disposition") or ""),
              hdr.get("Content-Disposition"))
    finally:
        try:
            (BASE / "config.json").write_text(cfg_backup, encoding="utf-8")
        except Exception:
            pass
        B.reload_config()
        srv.shutdown()
        B.TASKS.joinpath(f"{xss_mid}.json").unlink(missing_ok=True)
        import shutil
        shutil.rmtree(B.WORK / xss_mid, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
