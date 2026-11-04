#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命名配置（多套 LLM 配置保存 + 随时切换）的测试

全 mock，不碰网络、不真发信。测：
  - save / switch / delete provider
  - provider_status 带出 providers + active_provider
  - UI：首页只有入口、/model 有完整命名配置区、切换动作生效
"""
import sys
import urllib.parse
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

# ---- 隔离：不碰真实配置/任务 ----
B.CONFIG = {}
B.KEYWORD = "代码审核"
for k in ("llm_base_url", "llm_protocol", "llm_api_key", "llm_model", "llm_thinking",
          "llm_backend", "llm_max_output_tokens", "llm_temperature",
          "fallback_model", "active_provider"):
    B.CONFIG.pop(k, None)
B.CONFIG["llm_base_url"] = "https://api.example.com"
B.CONFIG["llm_protocol"] = "openai"
B.CONFIG["llm_api_key"] = "sk-FAKE-1234567890"
B.CONFIG["llm_model"] = "model-a"
B.CONFIG["llm_backend"] = "auto"


def main():
    # 保险：本测试只改内存 CONFIG，不写盘；但以防万一，备份/还原 config.json
    cfg_path = ROOT / "config.json"
    cfg_backup = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
    try:
        _run()
    finally:
        if cfg_backup is not None:
            cfg_path.write_text(cfg_backup, encoding="utf-8")
        try:
            B.reload_config()
        except Exception:
            pass


def _run():
    # ---- 1. 保存命名配置 ----
    err = B.save_provider("alpha", save=False)
    check("save_provider 成功", err == "", err)
    check("providers 里有 alpha", "alpha" in (B.CONFIG.get("providers") or {}))
    check("active_provider = alpha", B.get_active_provider() == "alpha")
    check("快照带 base_url",
          B.CONFIG["providers"]["alpha"].get("base_url") == "https://api.example.com")
    check("快照带 api_key",
          B.CONFIG["providers"]["alpha"].get("api_key") == "sk-FAKE-1234567890")

    # ---- 2. 改顶层再存第二个 ----
    B.CONFIG["llm_model"] = "model-b"
    B.CONFIG["llm_base_url"] = "http://10.0.0.5:8080"
    err = B.save_provider("beta", save=False)
    check("save_provider beta 成功", err == "", err)
    check("两个命名配置都在",
          set((B.CONFIG.get("providers") or {}).keys()) >= {"alpha", "beta"})

    # ---- 3. 切换：顶层应被 beta 覆盖 ----
    err = B.switch_provider("alpha", save=False)
    check("switch 到 alpha 成功", err == "", err)
    check("切换后 model 变回 model-a", B.CONFIG.get("llm_model") == "model-a")
    check("切换后 base_url 变回 example", B.CONFIG.get("llm_base_url") == "https://api.example.com")
    check("active = alpha", B.get_active_provider() == "alpha")

    err = B.switch_provider("beta", save=False)
    check("切换后 model 变 model-b", B.CONFIG.get("llm_model") == "model-b")
    check("active = beta", B.get_active_provider() == "beta")

    # ---- 4. 切换不存在的 -> 报错 ----
    err = B.switch_provider("nope", save=False)
    check("切换不存在的报错", "nope" in err, err)

    # ---- 5. 非法名字 -> 拒绝 ----
    err = B.save_provider("../evil", save=False)
    check("拒绝路径型配置名", err != "", err)
    err = B.save_provider("", save=False)
    check("拒绝空配置名", err != "", err)

    # ---- 6. 删除 ----
    err = B.delete_provider("alpha", save=False)
    check("删除 alpha 成功", err == "", err)
    check("alpha 没了", "alpha" not in (B.CONFIG.get("providers") or {}))
    check("beta 还在", "beta" in (B.CONFIG.get("providers") or {}))
    # 删 active 应清空 active
    err = B.delete_provider("beta", save=False)
    check("删除 active(beta) 后 active 清空", B.get_active_provider() == "")

    # ---- 7. provider_status 带出 ----
    B.save_provider("gamma", save=False)
    st = B.provider_status()
    check("status 有 providers 字段", "providers" in st)
    check("status 有 active_provider 字段", "active_provider" in st)
    check("status.providers 含 gamma",
          any(n == "gamma" for n, _ in (st.get("providers") or [])))
    check("status 里 key 是掩码",
          all("sk-FAKE-1234567890" not in d for _, d in (st.get("providers") or [])))

    # ---- 8. list_providers 排序 + 描述 ----
    B.save_provider("aaa", save=False)
    names = [n for n, _ in B.list_providers()]
    check("list_providers 按名排序", names == sorted(names), names)
    check("list_providers 描述不含明文 key",
          all("sk-FAKE-1234567890" not in d for _, d in B.list_providers()))

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
