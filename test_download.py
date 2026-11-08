#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「超大附件直链取回」的验证

真实踩到的坑：
  QQ 邮箱的「超大附件」只给 download_url，不给 attachment_id。
  该链接 302 到 CDN 后，没有 `MAIL5K` 会话 Cookie 就返回
  `400 X-Error-Message: Need MAIL5K= in Cookie`。
  改之前我们只会说「HTTP 400 Bad Request」，既定位不了原因，
  也没法把可操作的建议告诉发件人。

这里验证：
  A. 直链下载本身：跟随 302、带 Cookie、瞬时故障重试、4xx 不重试、
     HTML/空文件/假压缩包识别、大小上限
  B. Cookie 来源：config 值 / cookies.txt 文件 / 环境变量；日志里打码
  C. 回信文案：说清「超大附件需要网页登录态」，同时不再自相矛盾
  D. 接线：process_task 真的把 Cookie 传进去了
"""
import http.server
import os
import sys
import tempfile
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "lib"))

import anomaly as A  # noqa: E402
import bot as B  # noqa: E402

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


ZIP = b"PK\x03\x04" + b"\x00" * 2048


class H(http.server.BaseHTTPRequestHandler):
    hits = {}

    def log_message(self, *a):
        pass

    def _serve(self, body, ctype="application/zip"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        H.hits[p] = H.hits.get(p, 0) + 1
        if p == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/zip")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/hop1":
            self.send_response(302)
            self.send_header("Location", "/needs-cookie")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/zip":
            return self._serve(ZIP)
        if p == "/needs-cookie":
            if "MAIL5K=" not in (self.headers.get("Cookie") or ""):
                self.send_response(400)
                self.send_header("X-Error-Message", "Need MAIL5K= in Cookie")
                self.send_header("X-Error-Code", "-520003")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._serve(ZIP)
        if p == "/html":
            return self._serve(b"<html>please login</html>", "text/html; charset=utf-8")
        if p == "/fakezip":
            return self._serve(b"<html>gateway blocked</html>", "application/zip")
        if p == "/empty":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/flaky":
            if H.hits[p] < 2:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._serve(ZIP)
        if p == "/notfound":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    U = f"http://127.0.0.1:{port}"
    tmp = Path(tempfile.mkdtemp(prefix="dl_"))
    try:
        # ---------- A. 下载行为 ----------
        ok, path, err = A.download_via_url(f"{U}/redirect", tmp / "a.zip")
        check("跟随 302 重定向能下载成功", ok and path and path.stat().st_size == len(ZIP),
              f"{ok} {err}")

        ok, path, err = A.download_via_url(f"{U}/needs-cookie", tmp / "b.zip")
        check("缺 Cookie 下载失败", not ok)
        check("失败原因保留服务端原文（X-Error-Message)",
              "Need MAIL5K= in Cookie" in err, err)
        check("失败原因被翻成人话（提超大附件/登录态）",
              "超大附件" in err and "登录态" in err, err)
        check("needs_login() 能识别这类失败", A.needs_login(err), err)

        ok, path, err = A.download_via_url(f"{U}/needs-cookie", tmp / "c.zip",
                                           cookie="MAIL5K=abc123")
        check("带上 Cookie 后能下载成功", ok and path and path.stat().st_size == len(ZIP), err)

        ok, path, err = A.download_via_url(f"{U}/needs-cookie", tmp / "c2.zip",
                                           cookie="abc123")
        check("只给 Cookie 值会自动补 MAIL5K=", ok, err)

        # 真实链路就是这样：第一跳 302 到 CDN，Cookie 必须在跳转后仍然带着
        ok, path, err = A.download_via_url(f"{U}/hop1", tmp / "c3.zip", cookie="MAIL5K=abc123")
        check("302 跳转后仍然带着 Cookie", ok, err)
        ok, path, err = A.download_via_url(f"{U}/hop1", tmp / "c4.zip")
        check("不带 Cookie 走 302 -> 失败且原因指向登录态",
              not ok and A.needs_login(err), err)

        check("cookie_header 规整：裸值补名字",
              A.cookie_header("abc") == "MAIL5K=abc", A.cookie_header("abc"))
        check("cookie_header 规整：标准串原样",
              A.cookie_header("MAIL5K=a; uin=o1") == "MAIL5K=a; uin=o1")
        check("cookie_header 规整：空值不变",
              A.cookie_header("  ") == "")
        check("needs_login 不误判普通失败",
              not A.needs_login("HTTP 404 Not Found") and not A.needs_login(""))

        H.hits.pop("/flaky", None)
        ok, path, err = A.download_via_url(f"{U}/flaky", tmp / "d.zip", retries=3)
        check("5xx 属于瞬时故障 -> 重试后成功", ok and H.hits.get("/flaky", 0) >= 2,
              f"{ok} hits={H.hits.get('/flaky')} {err}")

        H.hits.pop("/notfound", None)
        ok, path, err = A.download_via_url(f"{U}/notfound", tmp / "e.zip", retries=3)
        check("404 不重试（只请求一次）", not ok and H.hits.get("/notfound") == 1,
              f"hits={H.hits.get('/notfound')}")

        ok, path, err = A.download_via_url(f"{U}/html", tmp / "f.zip")
        check("返回 HTML 页面 -> 判失败并说明", not ok and "HTML" in err, err)

        ok, path, err = A.download_via_url(f"{U}/empty", tmp / "g.zip")
        check("0 字节 -> 判失败", not ok and "0 字节" in err, err)

        ok, path, err = A.download_via_url(f"{U}/fakezip", tmp / "h.zip")
        check("内容是错误页但叫 .zip -> 判失败", not ok and "压缩包" in err, err)
        check("假压缩包不会留在磁盘上", not (tmp / "h.zip").exists())

        ok, path, err = A.download_via_url(f"{U}/zip", tmp / "i.zip", max_bytes=100)
        check("超过大小上限 -> 判失败", not ok and "上限" in err, err)

        # ---------- B. Cookie 来源 ----------
        old_ck, old_cf = B.CONFIG.get("download_cookie"), B.CONFIG.get("download_cookie_file")
        try:
            B.CONFIG["download_cookie"] = "MAIL5K=fromcfg"
            B.CONFIG["download_cookie_file"] = ""
            check("配置里的 Cookie 优先", B.download_cookie() == "MAIL5K=fromcfg")

            B.CONFIG["download_cookie"] = ""
            ck_file = tmp / "cookies.txt"
            ck_file.write_text(
                "# Netscape HTTP Cookie File\n"
                ".mail.qq.com\tTRUE\t/\tFALSE\t1900000000\tMAIL5K\tabc123\n"
                "mail.qq.com\tFALSE\t/\tTRUE\t0\tuin\to123\n", encoding="utf-8")
            B.CONFIG["download_cookie_file"] = str(ck_file)
            got = B.download_cookie()
            check("cookies.txt 能被解析",
                  "MAIL5K=abc123" in got and "uin=o123" in got, got)

            B.CONFIG["download_cookie_file"] = str(tmp / "不存在.txt")
            os.environ.pop(B.COOKIE_ENV, None)
            check("文件读不到且无环境变量 -> 空", B.download_cookie() == "")

            os.environ[B.COOKIE_ENV] = "MAIL5K=fromenv"
            check("环境变量作为兜底", B.download_cookie() == "MAIL5K=fromenv")
        finally:
            os.environ.pop(B.COOKIE_ENV, None)
            B.CONFIG["download_cookie"] = old_ck or ""
            B.CONFIG["download_cookie_file"] = old_cf or ""

        check("日志里 Cookie 只显示名字",
              B.mask_cookie("MAIL5K=secret123; uin=o1") == "MAIL5K,uin=***",
              B.mask_cookie("MAIL5K=secret123; uin=o1"))
        check("mask_cookie 不泄露值", "secret123" not in B.mask_cookie("MAIL5K=secret123"))
        check("空 Cookie 打码为空", B.mask_cookie("") == "")

        # ---------- C. 回信文案 ----------
        login_err = A.download_via_url(f"{U}/needs-cookie", tmp / "j.zip")[2]
        body = A.reply_link_only("代码审核-websec", [("websec-main.zip", 371907, login_err)])
        check("登录态回信说清是「超大附件」", "超大附件" in body, body[:300])
        check("登录态回信说清需要网页登录态", "登录态" in body, "")
        check("登录态回信仍给可操作办法", "换成普通附件重发" in body, "")
        check("登录态回信不再误报 20MB", "20MB 上限" not in body, "")
        check("登录态回信保留「链接形式」说明", "链接形式" in body, "")
        check("登录态回信不暴露内部实现细节",
              "MAIL5K" not in body and "download_cookie" not in body, body[:300])

        over = A.reply_link_only("代码审核-X", [("big.zip", 30 * 1024 * 1024, "HTTP 400")])
        check("超 20MB 回信给分卷方案", "分卷" in over, "")
        check("超 20MB 回信不自相矛盾（不写「并不是因为超过 20MB」）",
              "并不是因为超过 20MB" not in over, "")

        # ---------- D. 接线 ----------
        src = (BASE / "bot.py").read_text(encoding="utf-8")
        check("process_task 下载直链时传了 Cookie", "cookie=download_cookie()" in src, "")
        check("失败时提示配置登录态（日志）", "needs_login(err)" in src, "")
        check("有 CLI 可配置 Cookie", 'sub.add_parser("cookie"' in src, "")
        check("配置示例里有 download_cookie", "download_cookie" in
              (BASE / "config.example.json").read_text(encoding="utf-8"), "")
    finally:
        srv.shutdown()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASS}/{PASS + FAIL} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
