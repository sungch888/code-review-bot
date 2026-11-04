#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模型配置（列表 + 新增/编辑/激活/删除）的测试

全 mock，不碰网络、不真发信。测：
  - create_provider：新增 + 名称查重
  - update_provider：改名 + 新名称查重 + api_key 留空保持
  - switch_provider：激活展开到顶层
  - delete_provider：删非激活 OK，删激活被拦
  - get_provider / list_providers / provider_status 字段
  - 密钥只在 config 里、描述/状态只出掩码
"""
import sys
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

# 隔离：不碰真实配置（本测试只改内存 CONFIG，绝不写盘）
B.CONFIG = {}
B.CONFIG.update({
    "llm_backend": "auto", "llm_protocol": "openai",
    "llm_base_url": "", "llm_api_key": "", "llm_model": "",
})


def main():
    # 保险：备份/还原 config.json（虽然本测试 save=False 不写盘）
    cfg = ROOT / "config.json"
    backup = cfg.read_text(encoding="utf-8") if cfg.exists() else None
    try:
        _run()
    finally:
        if backup is not None:
            cfg.write_text(backup, encoding="utf-8")
        try:
            B.reload_config()
        except Exception:
            pass


def _run():
    # ---- create ----
    e1 = {"base_url": "https://api.a.com", "protocol": "openai",
          "api_key": "sk-FAKE-A-0001", "model": "model-a"}
    check("新增 alpha", B.create_provider("alpha", e1, save=False) == "")
    check("providers 含 alpha", "alpha" in (B.CONFIG.get("providers") or {}))
    check("新增同名被拒", "已存在" in B.create_provider("alpha", e1, save=False),
          B.create_provider("alpha", e1, save=False))
    check("拒绝空名", B.create_provider("", e1, save=False) != "")
    check("拒绝路径名", B.create_provider("../evil", e1, save=False) != "")
    check("缺 base_url 和 model 被拒",
          B.create_provider("z", {"base_url": "", "model": ""}, save=False) != "")

    # ---- get_provider ----
    g = B.get_provider("alpha")
    check("get_provider 返回 entry", isinstance(g, dict) and g.get("model") == "model-a")
    check("get_provider 不存在返回 None", B.get_provider("nope") is None)

    # ---- switch（激活展开到顶层）----
    B.CONFIG["llm_model"] = "SHOULD-BE-OVERWRITTEN"
    check("激活 alpha", B.switch_provider("alpha", save=False) == "")
    check("激活后顶层 model = model-a", B.CONFIG.get("llm_model") == "model-a",
          B.CONFIG.get("llm_model"))
    check("激活后顶层 base_url 同步", B.CONFIG.get("llm_base_url") == "https://api.a.com")
    check("active = alpha", B.get_active_provider() == "alpha")
    check("激活不存在的报错", "nope" in B.switch_provider("nope", save=False),
          B.switch_provider("nope", save=False))

    # ---- delete：删激活被拦 ----
    check("删激活项被拦", "不能删除" in B.delete_provider("alpha", save=False),
          B.delete_provider("alpha", save=False))
    # 激活别的再删
    e2 = {"base_url": "https://api.b.com", "protocol": "openai",
          "api_key": "sk-FAKE-B-0002", "model": "model-b"}
    B.create_provider("beta", e2, save=False)
    B.switch_provider("beta", save=False)
    check("删非激活 alpha 成功", B.delete_provider("alpha", save=False) == "")
    check("alpha 已删", "alpha" not in (B.CONFIG.get("providers") or {}))
    check("删不存在的报错", "不存在" in B.delete_provider("ghost", save=False),
          B.delete_provider("ghost", save=False))

    # ---- update：改名 + 查重 + api_key 留空保持 ----
    check("改名 beta->gamma", B.update_provider("beta", "gamma", e2, save=False) == "")
    check("gamma 存在、beta 没了",
          "gamma" in (B.CONFIG.get("providers") or {}) and "beta" not in (B.CONFIG.get("providers") or {}))
    # 改名撞到已有名
    B.create_provider("delta", {"base_url": "x", "model": "m"}, save=False)
    check("改名撞已有名被拒",
          "已被占用" in B.update_provider("gamma", "delta", e2, save=False),
          B.update_provider("gamma", "delta", e2, save=False))
    # api_key 留空 = 保持原 key
    check("编辑时 api_key 留空保持原值",
          B.update_provider("gamma", "gamma", {"base_url": "https://new.com",
                                               "model": "m2", "api_key": ""}, save=False) == "")
    check("原 key 保留",
          (B.get_provider("gamma") or {}).get("api_key") == "sk-FAKE-B-0002",
          (B.get_provider("gamma") or {}).get("api_key"))
    check("改 base_url 生效",
          (B.get_provider("gamma") or {}).get("base_url") == "https://new.com")
    # 编辑不存在的
    check("编辑不存在的报错", "不存在" in B.update_provider("ghost", "x", e2, save=False),
          B.update_provider("ghost", "x", e2, save=False))

    # ---- list / status ----
    names = [n for n, _ in B.list_providers()]
    check("list 排序", names == sorted(names), names)
    check("list 描述不含明文 key",
          all("sk-FAKE-B-0002" not in d for _, d in B.list_providers()))
    st = B.provider_status()
    check("status 有 providers + active_provider",
          "providers" in st and "active_provider" in st)
    check("status.active_provider 正确", st["active_provider"] in names,
          st["active_provider"])

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
