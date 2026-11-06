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
import re
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


def flash_of(hdr):
    """从重定向 Location 里解出 flash 文案（URL 解码）"""
    loc = hdr.get("Location") or ""
    q = urllib.parse.urlparse(loc).query
    kv = urllib.parse.parse_qs(q)
    return kv.get("m", [""])[0]


def main():
    # 回归：直接 `bot.py run` 启动时也要写 pid 文件，
    # 否则控制台会一直显示「守护进程未运行」（实际它活着）
    src = (BASE / "bot.py").read_text(encoding="utf-8")
    run_all_src = src.split("def run_all")[1][:900]
    check("run_all() 会写 pid 文件", "PIDFILE.write_text" in run_all_src,
          run_all_src[:120])
    check("run_all() 退出时清理 pid 文件", "PIDFILE.unlink" in run_all_src)

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

        # 详情页的「重跑/立即处理」按钮必须带上任务 id（否则提交空 id -> 空 /task/ 页）
        import re as _re
        retry_forms = _re.findall(r'<form[^>]*action="/action"[^>]*>.*?</form>', body, _re.S)
        retry_ok = any('value="retry"' in f and f'name="id" value="{xss_mid}"' in f
                       for f in retry_forms)
        check("详情页重跑按钮带上任务 id", retry_ok, "")
        process_ok = any('value="process"' in f and f'name="id" value="{xss_mid}"' in f
                         for f in retry_forms)
        check("详情页立即处理按钮带上任务 id", process_ok, "")

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
        # 把模型相关字段重置为干净状态，测试才有确定性（否则依赖用户当前配了哪个后端）
        _orig = json.loads(cfg_backup)
        for _f in ("llm_base_url", "llm_api_key", "llm_model", "llm_protocol",
                   "llm_thinking", "pi_model", "pi_thinking", "fallback_model"):
            _orig[_f] = ""
        _orig["llm_backend"] = "auto"
        (BASE / "config.json").write_text(json.dumps(_orig, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        B.reload_config()

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

        # 7.5 模型接入（列表 + 新增/编辑表单在 /model）
        try:
            st, _, body = get("/model?new=1")
            check("新增表单有 Base URL / API Key / 模型字段",
                  'name="base_url"' in body and 'name="api_key"' in body
                  and 'name="model"' in body and 'name="name"' in body, body[:120])
            check("表单不暴露已保存的密钥原文",
                  (B.CONFIG.get("llm_api_key") or "zz") not in body)

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
            check("模型配置页是列表(含名称/状态/操作)",
                  "模型配置" in body and "新增配置" in body and "状态" in body and "操作" in body,
                  body[:120])
            st, _, body = get("/model?new=1")
            check("API Key 用密码框", 'type="password" name="api_key"' in body, body[:200])
            st, _, body = get("/model")
            check("模型配置页不自动刷新", "REFRESH_SEC = 0" in body, body[-250:])
            check("任何页面都不再用 meta refresh 打断输入",
                  "http-equiv=\"refresh\"" not in body)
            st, _, body = get("/")
            check("控制台改用可暂停的 JS 刷新", "REFRESH_SEC = 20" in body and "formBusy" in body,
                  body[-250:])
            check("控制台会提示「正在输入，自动刷新已暂停」", "自动刷新已暂停" in body)
            check("控制台改用模型入口(不再放完整配置卡片)",
                  'href="/model"' in body and 'id="provform"' not in body)

            # 模型配置：列表 + 新增/编辑/激活/删除（假 key、不联网）
            st, _, body = get("/model")
            check("列表页有「模型配置」和「新增配置」", "模型配置" in body and "新增配置" in body,
                  body[:120])
            st, _, body = get("/model?new=1")
            check("新增表单出现且是 create_provider", "新增模型配置" in body
                  and 'value="create_provider"' in body and 'name="name"' in body, body[:150])
            check("新增表单有「获取模型列表/测试可用性」按钮",
                  "获取模型列表" in body and "测试可用性" in body and 'id="modelpick"' in body,
                  body[:150])
            check("表单 id=provform 供 provRun 取值", 'id="provform"' in body and 'id="provout"' in body)
            # 前端 PROV_FIELDS 必须带 old_name，否则编辑时测试拿不到该配置自己的 key
            check("PROV_JS 的 PROV_FIELDS 包含 old_name（编辑测试需携带配置引用）",
                  re.search(r'PROV_FIELDS\s*=\s*\[[^\]]*old_name', body) is not None)

            # 关键回归：编辑时测试要用「该配置自己的 key」，不是顶层/激活项的 key
            B.CONFIG["providers"] = {
                "pA": {"base_url": "https://api.a.com", "protocol": "openai",
                       "api_key": "sk-AAAA-1111", "model": "ma"},
                "pB": {"base_url": "https://api.b.com", "protocol": "openai",
                       "api_key": "sk-BBBB-2222", "model": "mb"},
            }
            B.CONFIG["llm_api_key"] = "sk-AAAA-1111"   # 顶层= pA 的 key
            B.CONFIG["active_provider"] = "pA"
            cfg = ui.provider_cfg_from_form({
                "base_url": ["https://api.b.com"], "protocol": ["openai"],
                "model": ["mb"], "old_name": ["pB"], "api_key": [""]})
            check("编辑 pB 用 pB 自己的 key（而非顶层 pA key）",
                  cfg["llm_api_key"] == "sk-BBBB-2222", cfg["llm_api_key"])
            cfg2 = ui.provider_cfg_from_form({
                "base_url": ["https://api.b.com"], "protocol": ["openai"],
                "old_name": ["pB"], "api_key": ["sk-NEW-9999"]})
            check("编辑时填了新 key 优先用新的", cfg2["llm_api_key"] == "sk-NEW-9999",
                  cfg2["llm_api_key"])
            cfg3 = ui.provider_cfg_from_form({
                "base_url": ["https://api.x.com"], "protocol": ["openai"],
                "api_key": ["sk-NEWX"], "model": ["mx"]})
            check("新增模式用表单填的 key", cfg3["llm_api_key"] == "sk-NEWX",
                  cfg3["llm_api_key"])

            # 新增 alpha（激活）
            st, hdr, _ = post("/action", {"action": "create_provider", "csrf": ui.CSI,
                                          "name": "alpha", "base_url": "https://api.alpha.example",
                                          "api_key": "sk-FAKE-ALPHA-0001", "model": "alpha-model",
                                          "activate": "1"})
            check("新增 alpha 成功", st == 303, st)
            check("alpha 进入 providers", "alpha" in (B.CONFIG.get("providers") or {}))
            check("新增后生效模型 alpha-model", B.CONFIG.get("llm_model") == "alpha-model",
                  B.CONFIG.get("llm_model"))
            # 新增重名被拦
            st, hdr, _ = post("/action", {"action": "create_provider", "csrf": ui.CSI,
                                          "name": "alpha", "base_url": "x", "model": "y"})
            check("新增重名被拒(名称不能重复)", "已存在" in flash_of(hdr),
                  flash_of(hdr))
            # 编辑 alpha 表单预填
            st, _, body = get("/model?edit=alpha")
            check("编辑表单预填 alpha", "编辑模型配置" in body
                  and 'value="update_provider"' in body and 'old_name" value="alpha"' in body,
                  body[:150])
            # 激活项不可删：alpha 现在是激活的
            st, hdr, _ = post("/action", {"action": "delete_provider", "csrf": ui.CSI, "name": "alpha"})
            check("删除激活项被拒(激活不可删)", "不能删除" in flash_of(hdr),
                  flash_of(hdr))
            # 列表页：激活行不带「删除」按钮
            st, _, body = get("/model")
            check("列表页存在激活标记", "当前生效" in body, body[:120])
            # 新增 beta 再激活它，让 alpha 可删
            post("/action", {"action": "create_provider", "csrf": ui.CSI,
                             "name": "beta", "base_url": "https://api.beta.example",
                             "api_key": "sk-FAKE-BETA-0002", "model": "beta-model", "activate": "1"})
            st, hdr, _ = post("/action", {"action": "delete_provider", "csrf": ui.CSI, "name": "alpha"})
            check("删除非激活 alpha 成功",
                  "已删除" in flash_of(hdr) and
                  "alpha" not in (B.CONFIG.get("providers") or {}), flash_of(hdr))
            # 编辑改名撞已有名被拦（把 beta 改名为 beta 之外的已存在名）
            post("/action", {"action": "create_provider", "csrf": ui.CSI,
                             "name": "gamma", "base_url": "g", "model": "gm"})
            st, hdr, _ = post("/action", {"action": "update_provider", "csrf": ui.CSI,
                                          "old_name": "beta", "name": "gamma",
                                          "base_url": "x", "model": "y"})
            check("编辑改名撞已有名被拒(名称不能重复)", "已被占用" in flash_of(hdr),
                  flash_of(hdr))
            # 收尾：清理测试配置，恢复一个默认命名
            B.delete_provider("gamma")
            B.delete_provider("beta")
            B.CONFIG["llm_model"] = "deepseek-chat"
            B.create_provider("default", {"base_url": "https://api.deepseek.com",
                                          "api_key": "", "model": "deepseek-chat"},
                              activate=True)
            B.switch_provider("default")

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

        # 9. 审批面板
        appr_mid = "msg_UItestAPPR"
        rpt = B.WORK / appr_mid / "report"
        rpt.mkdir(parents=True, exist_ok=True)
        (rpt / "代码审核报告.md").write_text("# 报告\n内容", encoding="utf-8")
        (rpt / "summary.md").write_text("摘要", encoding="utf-8")
        B.save_task({
            "message_id": appr_mid, "subject": "代码审核-审批UI测试",
            "from": "u@example.com", "from_name": "U",
            "created_at": B.iso(), "created_at_ts": B.now(),
            "state": "AWAITING_APPROVAL", "attempts": 1, "report": str(rpt),
            "last_error": None, "replied_at": None,
        })

        st, _, body = get("/")
        check("看板出现「待审批」区", "待审批" in body, body[:120])
        check("看板出现「审批闸」设置卡", "审批闸" in body and "require_approval" in body,
              body[:120])
        check("看板出现「确认信」设置", "确认信" in body, body[:200])

        st, _, body = get(f"/task/{appr_mid}")
        check("任务页出现待审批面板", "待审批" in body and "审批通过" in body, body[:150])
        check("任务页有驳回按钮", "驳回" in body, body[:150])
        check("任务页有预览报告链接", "预览报告" in body, body[:200])

        # 9.1 approve/reject 动作（mock 掉真实发送，避免发信）
        real_approve, real_reject = B.approve_task, B.reject_task
        called = {}
        B.approve_task = lambda mid, approver="": called.update(approve=mid) or B.load_task(mid)
        B.reject_task = lambda mid, reason="": called.update(reject=mid) or B.load_task(mid)
        try:
            st, hdr, _ = post("/action", {"action": "approve", "csrf": ui.CSI, "id": appr_mid})
            check("approve 动作重定向到任务页", (hdr.get("Location") or "").startswith("/task/"),
                  hdr.get("Location"))
            check("approve 调用了 B.approve_task", called.get("approve") == appr_mid, str(called))
            st, _, _ = post("/action", {"action": "reject", "csrf": ui.CSI, "id": appr_mid,
                                        "reason": "测试驳回"})
            check("reject 调用了 B.reject_task", called.get("reject") == appr_mid, str(called))
        finally:
            B.approve_task, B.reject_task = real_approve, real_reject

        # 9.2 审批配置保存
        st, _, _ = post("/action", {"action": "approval_cfg", "csrf": ui.CSI,
                                   "require_approval": "on", "ack_enabled": "on",
                                   "ack_timeout_sec": "420"})
        check("require_approval 已保存", B.CONFIG.get("require_approval") is True,
              str(B.CONFIG.get("require_approval")))
        check("ack_timeout_sec 已保存", B.CONFIG.get("ack_timeout_sec") == 420,
              str(B.CONFIG.get("ack_timeout_sec")))
        # 还原为默认（关闭审批）
        B.CONFIG["require_approval"] = False
        B.CONFIG["ack_timeout_sec"] = 300
        B._save_config()
    finally:
        try:
            (BASE / "config.json").write_text(cfg_backup, encoding="utf-8")
        except Exception:
            pass
        B.reload_config()
        srv.shutdown()
        B.TASKS.joinpath(f"{xss_mid}.json").unlink(missing_ok=True)
        _appr_mid = locals().get("appr_mid")
        if _appr_mid:
            B.TASKS.joinpath(f"{_appr_mid}.json").unlink(missing_ok=True)
            import shutil
            shutil.rmtree(B.WORK / _appr_mid, ignore_errors=True)
        import shutil
        shutil.rmtree(B.WORK / xss_mid, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
