#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理我自己造的测试邮件（两阶段确认 + 限频友好节奏）。

只处理明确列出的 message_id，不碰其它邮件。
用法: python cleanup_test_mail.py <ids.txt>
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

PACE = 7.0  # 秒/次，控制在 10 请求/分钟 以下


def _resolve_cli():
    """agently-cli 是 .cmd 包装器，CreateProcess 不能直接跑 -> 优先找真实的 .exe"""
    import os
    import shutil
    from pathlib import Path
    appdata = os.environ.get("APPDATA")
    if appdata:
        root = (Path(appdata) / "npm" / "node_modules" / "@tencent-qqmail" / "agently-cli"
                / "node_modules" / "@tencent-qqmail")
        if root.exists():
            hits = sorted(root.glob("agently-cli-win32-*/bin/agently-cli.exe"))
            if hits:
                return [str(hits[-1])]
    w = shutil.which("agently-cli.exe")
    if w:
        return [w]
    w = shutil.which("agently-cli.cmd")
    if w:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", w]
    return ["agently-cli"]


CLI = _resolve_cli()


def cli(*args):
    p = subprocess.run(CLI + list(args), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    out = p.stdout or ""
    lines = out.split("\n")
    cut = next((i for i, l in enumerate(lines) if l.strip().lower().startswith("tip:")), len(lines))
    body = "\n".join(lines[:cut])
    try:
        return json.loads(body)
    except Exception:
        return {"ok": False, "raw": out[:300]}


def main():
    ids = [l.strip() for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"待清理 {len(ids)} 封")

    # agently-cli +trash 的实际语义（实测）：
    #   第一次不带令牌调用 -> 返回 confirmation_token；
    #     * 对 inbox 邮件，服务端已把它移入回收站（再带令牌调第二次会报
    #       "Cannot delete message from this directory"）
    #     * 对 sent 邮件，此时尚未移动，需再带令牌调一次才真正删除
    # 因此下面两阶段都跑，并把「已不在原目录」视为成功。
    ok = fail = 0
    for i, mid in enumerate(ids, 1):
        r = cli("message", "+trash", "--id", mid)
        d = r.get("data") or {}
        ctk = d.get("confirmation_token")
        if not ctk:
            err = (r.get("error") or {}).get("message", "")
            print(f"  [{i}/{len(ids)}] 无令牌 {mid}: {err[:100]}")
            fail += 1
            time.sleep(PACE)
            continue
        r2 = cli("message", "+trash", "--id", mid, "--confirmation-token", ctk)
        if r2.get("ok"):
            print(f"  [{i}/{len(ids)}] 已清理 {mid}")
            ok += 1
        else:
            err = (r2.get("error") or {}).get("message", "")
            if "Cannot delete message from this directory" in err:
                print(f"  [{i}/{len(ids)}] 已在回收站（跳过） {mid}")
                ok += 1
            else:
                print(f"  [{i}/{len(ids)}] 失败 {mid}: {err[:110]}")
                fail += 1
        time.sleep(PACE)

    print(f"\n成功 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
