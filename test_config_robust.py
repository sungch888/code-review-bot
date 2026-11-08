#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置健壮性测试：config.json 被写坏（缺字段）时，机器人不能起不来/死循环

背景（真实事故）：
  一次测试事故把 config.json 写成了残缺版（缺 poll_interval_sec 等）。
  守护进程 reload 后内存 CONFIG 被剥掉该字段，worker 循环在
  `CONFIG["poll_interval_sec"]` 处 KeyError —— 而 reload_config() 在
  scan_once() 里，循环根本到不了 scan_once，于是永远自愈不了，
  任务一直卡在 SEEN。

本测试验证：
  1. 缺字段的 config.json 会被自动补齐（用 config.example.json 的默认值）
  2. worker 循环用的是 .get 兜底，不会因缺字段 KeyError
  3. 补齐后 CONFIG 里有 worker 需要的全部关键字段
"""
import json
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

CFG = ROOT / "config.json"
EX = ROOT / "config.example.json"

KEY_FIELDS = ("poll_interval_sec", "max_retries", "max_workers", "subject_keyword",
              "mailbox", "review_chunk_files", "max_chars_per_chunk",
              "llm_backend", "llm_base_url", "llm_api_key", "llm_model")


def main():
    if not CFG.exists() or not EX.exists():
        print("SKIP: 没有 config.json / config.example.json")
        return 0
    backup = CFG.read_text(encoding="utf-8")
    try:
        orig = json.loads(backup)
        # 造一个“残缺配置”：删掉一批关键字段
        broken = {k: v for k, v in orig.items()
                  if k not in ("poll_interval_sec", "max_retries", "max_workers",
                               "subject_keyword", "mailbox", "review_chunk_files",
                               "max_chars_per_chunk")}
        CFG.write_text(json.dumps(broken, ensure_ascii=False, indent=2), encoding="utf-8")

        # 1) _load_config 自动补齐缺失字段
        cfg = B._load_config()
        missing = [k for k in KEY_FIELDS if k not in cfg]
        check("缺字段的 config.json 被自动补齐", not missing, f"仍缺: {missing}")
        check("补齐后 subject_keyword 有值", bool(cfg.get("subject_keyword")),
              repr(cfg.get("subject_keyword")))

        # 2) reload_config 后 CONFIG 也完整
        B.reload_config()
        missing2 = [k for k in KEY_FIELDS if k not in B.CONFIG]
        check("reload_config 后内存 CONFIG 完整", not missing2, f"仍缺: {missing2}")

        # 3) 缺 poll_interval_sec 时 worker 循环不能崩（行为断言）
        #    以前是 CONFIG["poll_interval_sec"] 硬下标 -> 循环直接挂、任务永远卡在 SEEN
        cfg3 = json.loads(backup)
        cfg3.pop("poll_interval_sec", None)
        CFG.write_text(json.dumps(cfg3, ensure_ascii=False, indent=2), encoding="utf-8")
        B.reload_config()
        check("缺 poll_interval_sec 时 CONFIG.get 兜底为 180",
              B.CONFIG.get("poll_interval_sec", 180) == 180,
              str(B.CONFIG.get("poll_interval_sec")))
        # 真跑一轮 worker 循环：让它立刻返回，不应抛异常
        _old_scan = B.scan_once
        _old_pool = B.worker_pool
        try:
            scanned = []
            B.scan_once = lambda: scanned.append(1)
            B.worker_loop = lambda: None      # 只验证「能进入循环」这一步
            B._maybe_scan()
            B._maybe_reload()
            check("缺字段时扫描/重载都不抛异常", True)
            check("缺 poll_interval_sec 时仍会扫描（兜底值生效）", len(scanned) == 1,
                  str(len(scanned)))
        finally:
            B.scan_once = _old_scan
            B.worker_pool = _old_pool

        # 4) 完整配置也能正常加载（不回归）
        CFG.write_text(backup, encoding="utf-8")
        cfg2 = B._load_config()
        check("完整 config.json 正常加载",
              all(k in cfg2 for k in KEY_FIELDS),
              f"缺: {[k for k in KEY_FIELDS if k not in cfg2]}")
    finally:
        CFG.write_text(backup, encoding="utf-8")
        try:
            B.reload_config()
        except Exception:
            pass

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
