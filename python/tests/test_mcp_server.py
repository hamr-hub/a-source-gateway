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
async def test_six_tools_with_expected_names():
    tools = await mcp_server.list_tools()
    assert [t.name for t in tools] == [
        "list_sources", "get_source_meta", "describe_endpoint",
        "query_source", "get_meta_schema", "search_sources",
    ]
    search = next(t for t in tools if t.name == "search_sources")
    assert search.inputSchema["required"] == ["q"]


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


async def test_search_sources_posts_to_gateway(fake_gateway):
    out = _payload_of(await mcp_server.call_tool(
        "search_sources", {"q": "sec", "limit": 3}))
    assert out["meta"]["gateway"] == "go"
    method, path, body = [c for c in _FakeGateway.calls if c[0] == "POST"][0]
    assert path == "/v1/search"
    assert json.loads(body) == {"q": "sec", "limit": 3, "max_sources": 12}


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


async def test_search_sources_falls_back_to_local_resolver(monkeypatch):
    """网关不可达 -> 回落本地 resolver(关键词命中不到任何源时不触网)。"""
    monkeypatch.setenv("A_GATEWAY_URL", f"http://127.0.0.1:{_free_port()}")
    out = _payload_of(await mcp_server.call_tool(
        "search_sources", {"q": "zzz-没有这个源-zzz"}))
    assert out["meta"]["gateway"] == "python-fallback"
    assert out["queries"] == []
    assert out["results"] == []


async def test_search_sources_fallback_fans_out_with_the_shared_logic(monkeypatch):
    """本地回落用的是与 HTTP 路由同一份扇出逻辑(search.py)。"""
    import httpx

    from a_source_gateway.config import load_config
    from a_source_gateway.registry import build_registry
    from a_source_gateway.resolver import Resolver

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"items": [{"a": 1}],
                                                     "total-results": 1}})

    cfg = load_config(contact="test@example.com")
    resolver = Resolver(build_registry(cfg), transport=httpx.MockTransport(handler), cfg=cfg)
    await resolver.startup()
    monkeypatch.setattr(mcp_server, "_resolver", resolver)
    monkeypatch.setenv("A_GATEWAY_URL", f"http://127.0.0.1:{_free_port()}")
    try:
        out = _payload_of(await mcp_server.call_tool(
            "search_sources", {"q": "crossref", "limit": 2}))
    finally:
        await resolver.shutdown()
    assert out["meta"]["gateway"] == "python-fallback"
    assert "crossref/works" in out["queries"]
    assert out["results"][0]["count"] == 1
    assert out["results"][0]["source"] == "crossref"
    # 紧凑版: 只留概要, 不带完整 data
    assert set(out["results"][0]) == {"source", "endpoint", "count", "total",
                                      "degraded", "errors", "data"}
