"""查询层测试: 用 httpx.MockTransport 完全离线, 不触真实上游。"""
from __future__ import annotations

import httpx
import pytest

from a_source_gateway.config import load_config
from a_source_gateway.registry import build_registry
from a_source_gateway.resolver import Resolver
from a_source_gateway.schema import QueryRequest


def make_resolver(handler, contact: str = "test@example.com") -> Resolver:
    """默认带 contact: 部分源(SEC)要求 UA 里含联系方式, 否则会被 contact_required 拦下。"""
    cfg = load_config(warm_budget_ms=3000, contact=contact)
    reg = build_registry(cfg)
    return Resolver(reg, transport=httpx.MockTransport(handler), cfg=cfg)


def json_handler(payload, status=200):
    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)
    return _h


# ── URL 渲染 ──────────────────────────────────────────────────────────────
def test_render_substitutes_path_and_query():
    r = make_resolver(json_handler({}))
    ep = next(e for s in r.sources.values() if s["id"] == "sec_edgar"
              for e in s["endpoints"] if e["id"] == "submissions")
    url = r._render(ep, QueryRequest(source="sec_edgar", endpoint="submissions",
                                     params={"cik": "0000320193"}))
    assert url == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_render_appends_extra_params():
    r = make_resolver(json_handler({}))
    ep = next(e for s in r.sources.values() if s["id"] == "crossref"
              for e in s["endpoints"] if e["id"] == "works")
    url = r._render(ep, QueryRequest(source="crossref", endpoint="works",
                                     params={"filter": "from-index-date:2026-10-21"}))
    assert "filter=from-index-date" in url
    assert "rows=" in url


def test_render_falls_back_to_example_for_missing_required_param():
    """必填参数调用方没传时, 用端点声明的 example 兜底。

    端点 meta 里的 example 就是"这个端点怎么调"的答案; 没有它,
    调用方按文档那样 params:{} 调不动大多数端点。
    """
    r = make_resolver(json_handler({}))
    ep = next(e for s in r.sources.values() if s["id"] == "sec_edgar"
              for e in s["endpoints"] if e["id"] == "submissions")
    url = r._render(ep, QueryRequest(source="sec_edgar", endpoint="submissions", params={}))
    assert "0000320193" in url


def test_render_raises_when_required_param_has_neither_example_nor_default():
    """既没有 example 也没有 default 的必填参数仍然报错 —— 不能瞎编一个值。"""
    r = make_resolver(json_handler({}))
    ep = {
        "id": "x", "method": "GET", "url_template": "https://example.org/{needed}",
        "params": [{"name": "needed", "in": "query", "required": True}],
        "response": {"shape": "object"},
    }
    with pytest.raises(KeyError):
        r._render(ep, QueryRequest(source="x", endpoint="x", params={}))


def test_render_prefers_env_over_default_and_caller_over_env(monkeypatch):
    """凭据注入: 调用方显式传的值 > 环境变量 > 端点声明的默认值。"""
    r = make_resolver(json_handler({}))
    ep = {
        "id": "x", "method": "GET", "url_template": "https://example.org/?k={api_key}",
        "params": [{"name": "api_key", "in": "query", "default": "DEFAULT_KEY",
                    "env": "A_GATEWAY_TEST_KEY"}],
        "response": {"shape": "object"},
    }
    url = r._render(ep, QueryRequest(source="x", endpoint="x", params={}))
    assert "k=DEFAULT_KEY" in url

    monkeypatch.setenv("A_GATEWAY_TEST_KEY", "ENV_KEY")
    url = r._render(ep, QueryRequest(source="x", endpoint="x", params={}))
    assert "k=ENV_KEY" in url

    url = r._render(ep, QueryRequest(source="x", endpoint="x",
                                     params={"api_key": "CALLER_KEY"}))
    assert "k=CALLER_KEY" in url


def test_cache_buster_is_not_sent_upstream():
    r = make_resolver(json_handler({}))
    ep = next(e for s in r.sources.values() if s["id"] == "crossref"
              for e in s["endpoints"] if e["id"] == "works")
    url = r._render(ep, QueryRequest(source="crossref", endpoint="works",
                                     params={"filter": "x", "_cb": "abc"}))
    assert "_cb" not in url


def test_page_size_is_normalised():
    """调用方要 3 条和要 50 条, 必须命中同一条缓存。"""
    r = make_resolver(json_handler({}))
    ep = next(e for s in r.sources.values() if s["id"] == "crossref"
              for e in s["endpoints"] if e["id"] == "works")
    a = r._render(ep, QueryRequest(source="crossref", endpoint="works",
                                   params={"filter": "x", "rows": 3}))
    b = r._render(ep, QueryRequest(source="crossref", endpoint="works",
                                   params={"filter": "x", "rows": 50}))
    assert a == b


def test_render_keeps_bare_query_keys():
    """模板里没有 "=" 的查询键(如 ?pretty)必须原样保留, 不能抛 IndexError。

    Go 网关一直是对的(保留裸键), Python 侧曾经直接崩 —— 同一份数据文件在 Go 上能跑,
    在 MCP 的本地回落路径上却 500。
    """
    r = make_resolver(json_handler({}))
    ep = {"id": "x", "method": "GET",
          "url_template": "https://example.org/v1/items?pretty&limit={size}",
          "params": [{"name": "size", "in": "query"}], "response": {"shape": "object"}}
    url = r._render(ep, QueryRequest(source="s", endpoint="x", params={"size": 10}),
                    page_size=10)
    assert url == "https://example.org/v1/items?pretty&limit=10"


def test_render_drops_empty_query_values_but_keeps_bare_keys():
    """空值(mailto=)丢弃, 裸键(pretty)保留 —— 与 Go 侧逐条对齐。"""
    r = make_resolver(json_handler({}), contact="")   # 不填邮箱, mailto 保持空值
    ep = {"id": "x", "method": "GET",
          "url_template": "https://example.org/v1/items?pretty&mailto={email}&rows={rows}",
          "params": [{"name": "email", "in": "query"}, {"name": "rows", "in": "query"}],
          "response": {"shape": "object"}}
    url = r._render(ep, QueryRequest(source="s", endpoint="x",
                                     params={"email": "", "rows": 10}), page_size=10)
    assert url == "https://example.org/v1/items?pretty&rows=10"


def test_render_percent_encodes_the_url_placeholder():
    """{url} 必须百分号编码: 否则调用方能往查询串里注入额外参数或用 "#" 截断。

    主机名仍由模板锁定(端点模板里没有主机级占位符), 所以这不是直接 SSRF,
    但参数注入是可用的 —— scaffold_source.py 生成的镜像/代理型端点就会带 {url}。
    """
    r = make_resolver(json_handler({}))
    ep = {"id": "y", "method": "GET",
          "url_template": "https://example.org/v1/proxy?target={url}&rows=10",
          "params": [{"name": "url", "in": "query", "required": True}],
          "response": {"shape": "object"}}
    url = r._render(ep, QueryRequest(source="s", endpoint="y", params={
        "url": "http://169.254.169.254/latest/meta-data/&admin=true"}))
    assert url == ("https://example.org/v1/proxy?target=http%3A%2F%2F169.254.169.254"
                   "%2Flatest%2Fmeta-data%2F%26admin%3Dtrue&rows=10")
    # 注入的参数没有变成独立的查询参数
    assert "&admin=true" not in url
    assert "#" not in url


def test_render_keeps_slashes_in_path_params():
    """路径参数里的 "/" 不能被编码(EIA 的 v2 路由会被打成 %2F 而 404)。"""
    r = make_resolver(json_handler({}))
    ep = {"id": "z", "method": "GET",
          "url_template": "https://example.org/v2/{route}/data",
          "params": [{"name": "route", "in": "path"}], "response": {"shape": "object"}}
    url = r._render(ep, QueryRequest(source="s", endpoint="z", params={"route": "a/b/c"}))
    assert url == "https://example.org/v2/a/b/c/data"


# ── 查询与投影 ────────────────────────────────────────────────────────────
async def test_query_projects_list_and_total():
    payload = {"message": {"total-results": 1726791,
                           "items": [{"DOI": f"10.1/{i}", "title": f"t{i}"} for i in range(5)]}}
    r = make_resolver(json_handler(payload))
    await r.startup()
    env = await r.query(QueryRequest(source="crossref", endpoint="works",
                                      params={"filter": "from-index-date:2026-10-21"}, limit=2))
    assert env.meta.count == 2
    assert env.meta.total == 1726791
    assert env.meta.truncated is True
    assert len(env.data) == 2
    assert env.data[0]["DOI"] == "10.1/0"
    await r.shutdown()


async def test_query_projects_fields():
    payload = {"message": {"total-results": 1, "items": [{"a": 1, "b": 2, "c": 3}]}}
    r = make_resolver(json_handler(payload))
    await r.startup()
    env = await r.query(QueryRequest(source="crossref", endpoint="works",
                                      params={"filter": "x"}, fields=["a"], limit=5))
    assert env.data[0] == {"a": 1}
    await r.shutdown()


async def test_query_uses_cache_on_second_call():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"message": {"total-results": 7, "items": [{"a": 1}]}})

    r = make_resolver(handler)
    await r.startup()
    req = QueryRequest(source="crossref", endpoint="works", params={"filter": "x"}, limit=1)
    e1 = await r.query(req)
    e2 = await r.query(req)
    assert calls["n"] == 1, "第二次必须命中缓存"
    assert e1.meta.cached is False and e2.meta.cached is True
    assert e2.meta.total == 7
    await r.shutdown()


async def test_map_item_shape():
    """SEC company_tickers 顶层是 {序号: 记录}, 要按值域投影。"""
    payload = {"0": {"ticker": "AAPL", "cik_str": 1}, "1": {"ticker": "MSFT", "cik_str": 2}}
    r = make_resolver(json_handler(payload))
    await r.startup()
    env = await r.query(QueryRequest(source="sec_edgar", endpoint="company_tickers", limit=5))
    assert env.meta.count == 2
    assert {d["ticker"] for d in env.data} == {"AAPL", "MSFT"}
    await r.shutdown()


async def test_upstream_error_is_not_cached():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    r = make_resolver(handler)
    await r.startup()
    req = QueryRequest(source="crossref", endpoint="works", params={"filter": "x"}, limit=1)
    await r.query(req)
    await r.query(req)
    assert calls["n"] == 2, "错误响应不应被缓存"
    await r.shutdown()


async def test_contact_required_short_circuits():
    """要求 UA 带邮箱的源, 未配置 contact 时应 0ms 返回可执行提示, 不去打上游。"""
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    r = make_resolver(handler, contact="")
    await r.startup()
    env = await r.query(QueryRequest(source="sec_edgar", endpoint="submissions",
                                      params={"cik": "0000320193"}, limit=1))
    assert env.meta.degraded is True
    assert env.meta.errors[0]["type"] == "contact_required"
    assert "A_GATEWAY_CONTACT" in env.meta.fallback.get("hint", "")
    assert called["n"] == 0, "不应打上游"
    await r.shutdown()


async def test_unknown_source_returns_error_envelope():
    r = make_resolver(json_handler({}))
    await r.startup()
    env = await r.query(QueryRequest(source="根本没有这个源", endpoint="x"))
    assert env.meta.errors and env.meta.errors[0]["type"] == "not_found"
    await r.shutdown()


async def test_timeout_degrades_with_fallback_url():
    """上游慢 -> 必须降级, 并给出直连 URL, 而不是挂死。"""
    import asyncio as _a

    async def slow(request):
        await _a.sleep(3)
        return httpx.Response(200, json={})

    r = make_resolver(slow)
    await r.startup()
    env = await r.query(QueryRequest(source="crossref", endpoint="works",
                                      params={"filter": "x"}, limit=1, timeout_ms=300))
    assert env.meta.degraded is True
    assert env.meta.errors[0]["type"] == "timeout"
    assert env.meta.fallback.get("upstream_url", "").startswith("https://api.crossref.org")
    assert env.meta.latency_ms < 500
    await r.shutdown()


async def test_offline_mode_never_hits_network():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    reg = build_registry(load_config())
    r = Resolver(reg, offline=True, transport=httpx.MockTransport(handler))
    await r.startup()
    env = await r.query(QueryRequest(source="crossref", endpoint="works",
                                      params={"filter": "x"}, limit=1))
    assert env.meta.offline is True
    assert called["n"] == 0
    await r.shutdown()
