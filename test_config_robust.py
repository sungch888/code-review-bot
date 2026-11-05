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
import shutil
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

        # 3) worker 循环用 .get 兜底（源码级检查，防止回退成硬下标）
        src = (ROOT / "bot.py").read_text(encoding="utf-8")
        loop_src = src.split("def worker_loop")[1][:1500]
        # 只看代码行，忽略注释
        code_lines = [ln.strip() for ln in loop_src.splitlines()
                      if ln.strip() and not ln.strip().startswith("#")]
        hard = [ln for ln in code_lines if 'CONFIG["poll_interval_sec"]' in ln
                or "CONFIG['poll_interval_sec']" in ln]
        check("worker 循环不再硬取 CONFIG['poll_interval_sec']", not hard, str(hard[:1]))
        check("worker 循环用 CONFIG.get 兜底",
              'CONFIG.get("poll_interval_sec"' in loop_src)
        check("worker 每轮先 reload_config（可自愈）",
              "reload_config()" in loop_src[:600])

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
