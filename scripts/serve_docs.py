"""
serve_docs.py - 本地起文档站, 并把 /__gateway/* 反向代理到网关。

为什么需要它(而不是直接 `python3 -m http.server`):
    文档站里的「API 调试台」要在浏览器里真的向网关发请求。
    浏览器同源策略会拦掉 `http://localhost:8000` → `http://127.0.0.1:8848` 的跨域调用
    (网关没有开 CORS 头)。本脚本把网关挂在**同源**路径 `/__gateway/*` 下,
    调试台就变成普通同源请求, 不用改网关一行代码。

用法:
    python3 scripts/serve_docs.py                 # 起在 127.0.0.1:8000, 代理到 127.0.0.1:8848
    python3 scripts/serve_docs.py --port 8080
    python3 scripts/serve_docs.py --gateway http://127.0.0.1:9000
    python3 scripts/serve_docs.py --gateway https://gw.example.com   # 代理远端网关

    只想看静态页面、不要代理:
    python3 -m http.server -d site 8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITE_DIR = REPO_ROOT / "site"
DEFAULT_GATEWAY = "http://127.0.0.1:8848"

# 代理前缀: 文档站内部用它访问网关
PROXY_PREFIX = "/__gateway"

# 转发到上游的请求头(其余头一律丢弃)
FORWARD_HEADERS = (
    "Content-Type",
    "Accept",
    "X-Gateway-Trace-Id",
    "X-Request-Id",
    "User-Agent",
)

# 上游超时(秒): 网关自身有 500ms 预算, 这里给的是"网关挂了/不可达"的兜底
UPSTREAM_TIMEOUT_S = 30

# 代理转发请求体的上限(字节)。网关自己是 4MB, 这里留一点余量但必须有上限,
# 否则一个恶意(或手滑)的客户端能把内存吃满。
MAX_BODY_BYTES = 8 * 1024 * 1024


class DocsHandler(SimpleHTTPRequestHandler):
    """静态文件 + /__gateway/* 反向代理。"""

    gateway_base = DEFAULT_GATEWAY
    quiet = False

    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    # ── 路由分发 ──────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 (stdlib 命名)
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == PROXY_PREFIX or path.startswith(PROXY_PREFIX + "/"):
            self._proxy()
            return
        # 静态文件只服务 GET/HEAD: 其它方法对静态资源没有意义,
        # 直接回 405(而不是把文件内容当响应体返回)。
        if self.command not in ("GET", "HEAD"):
            self.send_error(405, "Method Not Allowed")
            return
        super().do_GET()

    # ── 反向代理 ──────────────────────────────────────────────────────────

    def _proxy(self) -> None:
        split = urllib.parse.urlsplit(self.path)
        target = split.path[len(PROXY_PREFIX):] or "/"
        if split.query:
            target += "?" + split.query
        # 归一化路径: 别把 "/../../etc/passwd" 原样透传给上游。
        # 保留结尾的斜杠语义, 只折叠 . 与 ..。
        target = _normalize_path(target)
        url = self.gateway_base.rstrip("/") + target

        body = self._read_body()
        if body is None:
            return

        req = urllib.request.Request(url, data=body, method=self.command)
        for name in FORWARD_HEADERS:
            value = self.headers.get(name)
            if value:
                req.add_header(name, value)

        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as resp:
                self._relay(resp.status, resp.headers.get("Content-Type", "application/json"),
                            resp.read())
        except urllib.error.HTTPError as exc:
            # 网关活着但返回了错误码(4xx/5xx): 原样转发, 让页面显示真实语义
            self._relay(exc.code, exc.headers.get("Content-Type", "application/json"), exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            payload = {
                "detail": f"网关不可达: {type(exc).__name__}: {exc}",
                "gateway": self.gateway_base,
                "hint": "先启动网关(make up 或 make gateway-run), 再刷新本页; "
                        "也可以在调试台里把地址改成别的网关。",
            }
            self._relay(502, "application/json; charset=utf-8",
                        json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _read_body(self) -> bytes | None:
        """读请求体。返回 None 表示已经自己回了错误响应。"""
        if self.headers.get("Transfer-Encoding", "").lower().strip() == "chunked":
            # 分块编码: urllib 不支持, 与其静默丢 body(上游收到空请求), 不如明说。
            self._relay(400, "application/json; charset=utf-8", json.dumps({
                "detail": "不支持 chunked 请求体, 请用 Content-Length。",
            }, ensure_ascii=False).encode("utf-8"))
            return None

        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return b""
        try:
            length = int(raw_len)
        except ValueError:
            self._relay(400, "application/json; charset=utf-8", json.dumps({
                "detail": f"Content-Length 不是合法整数: {raw_len!r}",
            }, ensure_ascii=False).encode("utf-8"))
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._relay(413, "application/json; charset=utf-8", json.dumps({
                "detail": f"请求体过大: {length} 字节(上限 {MAX_BODY_BYTES})",
            }, ensure_ascii=False).encode("utf-8"))
            return None
        return self.rfile.read(length) if length else b""

    def _relay(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    # ── 日志 ──────────────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args) -> None:
        if self.quiet:
            return
        sys.stderr.write(f"[docs] {self.address_string()} {fmt % args}\n")

    def log_error(self, fmt: str, *args) -> None:
        self.log_message(fmt, *args)


def _normalize_path(path: str) -> str:
    """把路径里的 . 与 .. 折叠掉(保留开头的 / 与结尾的 /)。

    转发前做一次, 别让 "/__gateway/../../etc/passwd" 这种原样打到上游。
    """
    trailing = path.endswith("/")
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    out = "/" + "/".join(parts)
    if trailing and not out.endswith("/"):
        out += "/"
    return out


def build_handler(site_dir: Path, gateway: str, quiet: bool) -> type[DocsHandler]:
    """把运行参数绑到 handler 类上(SimpleHTTPRequestHandler 需要目录参数)。"""

    class _Handler(DocsHandler):
        pass

    _Handler.gateway_base = gateway
    _Handler.quiet = quiet

    def handler_factory(*args, **kwargs):
        return _Handler(*args, directory=str(site_dir), **kwargs)

    return handler_factory


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="serve_docs.py",
        description="起本地文档站, 并把 /__gateway/* 代理到网关(用于在线调试台)。")
    ap.add_argument("--port", type=int, default=8000, help="文档站端口(默认 8000)")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址(默认 127.0.0.1)")
    ap.add_argument("--site-dir", default=str(DEFAULT_SITE_DIR), help="静态目录(默认 site/)")
    ap.add_argument("--gateway", default=os.environ.get("A_GATEWAY_URL")
                    or os.environ.get("A_GATEWAY_BASE") or DEFAULT_GATEWAY,
                    help=f"网关地址(默认 {DEFAULT_GATEWAY})")
    ap.add_argument("--quiet", action="store_true", help="不打印访问日志")
    args = ap.parse_args(argv)

    site_dir = Path(args.site_dir).resolve()
    if not site_dir.is_dir():
        print(f"文档目录不存在: {site_dir}", file=sys.stderr)
        return 2

    gateway = args.gateway.rstrip("/")
    handler = build_handler(site_dir, gateway, args.quiet)

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        print(f"起服务失败: {exc}", file=sys.stderr)
        return 2

    print(f"文档站   http://{args.host}:{args.port}/")
    print(f"网关代理 {gateway}  →  http://{args.host}:{args.port}{PROXY_PREFIX}/")
    print("调试台用的是同源代理路径, 不受浏览器跨域限制。按 Ctrl+C 停止。")
    if args.host in ("0.0.0.0", "::"):
        print()
        print("⚠ 监听在所有网卡上: 本机就成了一个**无鉴权**的网关转发口。")
        print("  网关本身没有鉴权, 确认这台机器的网络环境可控再用; "
              "否则请改用默认的 127.0.0.1。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
