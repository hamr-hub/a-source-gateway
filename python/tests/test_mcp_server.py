"""MCP server 的工具清单与"Go 网关 / 本地回落"选择(纯离线, 不打真实上游)。"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("mcp")

from a_source_gateway import mcp_server  # noqa: E402


def _free_port() -> int:
    """拿一个当前空闲的端口(随后连它必然被拒 -> 触发回落)。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _payload_of(result) -> dict:
    return json.loads(result[0].text)


class _FakeGateway(BaseHTTPRequestHandler):
    """假 Go 网关: 只回固定信封, 并记录收到的请求。"""

    calls: list[tuple[str, str, bytes]] = []

    def _reply(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        type(self).calls.append(("GET", self.path, b""))
        if self.path.startswith("/v1/sources"):
            self._reply({"meta": {"count": 1, "schema_version": "1.0"},
                         "data": [{"id": "fake", "name": "假源"}],
                         "fields": ["id", "name"]})
        elif self.path.startswith("/v1/schema"):
            self._reply({"schema_version": "1.0", "fake": True})
        else:
            self._reply({"detail": "not found"}, status=404)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).calls.append(("POST", self.path, body))
        self._reply({"meta": {"source": "fake", "count": 1}, "data": [{"a": 1}],
                     "fields": ["a"]})

    def log_message(self, *args) -> None:      # 静音
        return


@pytest.fixture()
def fake_gateway(monkeypatch):
    _FakeGateway.calls = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeGateway)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("A_GATEWAY_URL", f"http://127.0.0.1:{srv.server_port}")
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


# ── 工具清单 ─────────────────────────────────────────────────────────────
async def test_five_tools_with_expected_names():
    tools = await mcp_server.list_tools()
    assert [t.name for t in tools] == [
        "list_sources", "get_source_meta", "describe_endpoint",
        "query_source", "get_meta_schema",
    ]


# ── 走 Go 网关 ───────────────────────────────────────────────────────────
async def test_gateway_is_used_when_reachable(fake_gateway):
    out = _payload_of(await mcp_server.call_tool("list_sources", {"limit": 5}))
    assert out["meta"]["gateway"] == "go"
    assert out["data"][0]["id"] == "fake"
    assert ("GET", "/v1/sources?family=&q=&limit=5", b"") in _FakeGateway.calls


async def test_query_source_posts_to_gateway(fake_gateway):
    out = _payload_of(await mcp_server.call_tool(
        "query_source", {"source": "fake", "endpoint": "e1", "limit": 3}))
    assert out["meta"]["gateway"] == "go"
    method, path, body = [c for c in _FakeGateway.calls if c[0] == "POST"][0]
    assert path == "/v1/query"
    assert json.loads(body)["source"] == "fake"
    assert json.loads(body)["limit"] == 3


async def test_get_meta_schema_uses_gateway(fake_gateway):
    out = _payload_of(await mcp_server.call_tool("get_meta_schema", {}))
    assert out["fake"] is True


# ── 网关不可达 -> 本地回落 ───────────────────────────────────────────────
async def test_fallback_when_gateway_unreachable(monkeypatch):
    monkeypatch.setenv("A_GATEWAY_URL", f"http://127.0.0.1:{_free_port()}")
    out = _payload_of(await mcp_server.call_tool("list_sources", {"limit": 3}))
    assert out["meta"]["gateway"] == "python-fallback"
    assert isinstance(out["data"], list)
    assert len(out["data"]) <= 3


async def test_fallback_still_returns_real_source_meta(monkeypatch):
    monkeypatch.setenv("A_GATEWAY_URL", f"http://127.0.0.1:{_free_port()}")
    out = _payload_of(await mcp_server.call_tool("get_source_meta", {"source": "sec"}))
    assert out["meta"]["gateway"] == "python-fallback"
    # "sec" 是别名, 解析到 SEC EDGAR 这个源 id
    assert out["data"]["id"] == "sec_edgar"
    assert "sec" in out["data"]["aliases"]


async def test_fallback_for_unknown_source_keeps_envelope_shape(monkeypatch):
    monkeypatch.setenv("A_GATEWAY_URL", f"http://127.0.0.1:{_free_port()}")
    out = _payload_of(await mcp_server.call_tool("get_source_meta", {"source": "no-such-src"}))
    assert out["meta"]["gateway"] == "python-fallback"
    assert out["data"] is None
    assert "error" in out["meta"]
