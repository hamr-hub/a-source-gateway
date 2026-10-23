"""HTTP API 与 MCP 工具的测试(离线)。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from a_source_gateway.api import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_root_lists_routes_and_all_mcp_tools(client):
    d = client.get("/").json()
    assert d["sources"] >= 90
    tools = d["mcp"]["tools"]
    assert set(tools) == {"list_sources", "get_source_meta", "describe_endpoint",
                          "query_source", "get_meta_schema"}, "根路由必须列全 MCP 工具"


def test_schema_endpoint_documents_the_meta_format(client):
    d = client.get("/v1/schema").json()
    for k in ("source_spec", "endpoint_spec", "request", "response_envelope"):
        assert k in d
    env = d["response_envelope"]["meta"]
    for k in ("upstream", "latency_ms", "budget_ms", "cached", "degraded",
              "fallback", "errors", "count", "total", "truncated"):
        assert k in env, f"schema 缺字段 {k}"


def test_openapi_document_exposes_the_same_paths_as_the_go_gateway(client):
    """FastAPI 自带 /openapi.json;Go 网关也挂在同一路径(从路由表生成)。
    两边列出的业务路径必须一致, 文档不能只更新一侧。"""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["openapi"].startswith("3.")
    paths = doc["paths"]
    for want in ("/v1/sources", "/v1/sources/{source_id}",
                 "/v1/sources/{source_id}/endpoints/{endpoint_id}", "/v1/query"):
        assert want in paths, f"OpenAPI 文档缺路径 {want}"


def test_metrics_exposition_has_latency_histogram(client):
    """500ms 是硬契约: /metrics 必须暴露延迟直方图, 桶边界与 Go 网关一致。"""
    from a_source_gateway.api import _r
    from a_source_gateway.schema import ResponseMeta

    # 直接记三笔观测, 不依赖网络: 80ms / 450ms / 600ms
    for ms in (80, 450, 600):
        _r()._observe(ResponseMeta(source="crossref", latency_ms=ms))
    text = client.get("/metrics").text
    assert client.get("/metrics").headers["content-type"].startswith("text/plain")
    # 防回归: FastAPI 对裸 str 会做 JSON 编码, 指标文本不能带转义换行
    assert "\\n" not in text
    assert "# TYPE asg_query_latency_ms histogram" in text
    buckets = {}
    for line in text.splitlines():
        if line.startswith("asg_query_latency_ms_bucket"):
            le = line.split('le="')[1].split('"')[0]
            buckets[le] = int(line.rsplit(" ", 1)[1])
    # 累计桶语义: <=100 有 1, <=500 有 2, +Inf 有 3
    assert buckets["100"] == 1
    assert buckets["500"] == 2
    assert buckets["+Inf"] == 3
    assert "asg_query_latency_ms_count 3" in text
    assert "asg_query_latency_ms_sum 1130" in text


def test_list_sources(client):
    d = client.get("/v1/sources?limit=5").json()
    assert d["meta"]["count"] >= 90
    assert len(d["data"]) == 5
    for s in d["data"]:
        assert "id" in s and "name" in s and "endpoint_count" in s


def test_list_sources_filter_by_family(client):
    d = client.get("/v1/sources?family=制裁").json()
    assert d["meta"]["count"] > 0
    assert all("制裁" in s["family"] for s in d["data"])


def test_get_source_meta(client):
    d = client.get("/v1/sources/sec_edgar").json()["data"]
    assert d["name"] == "SEC EDGAR"
    assert d["scale"]["total"] == 340131
    assert d["proxy_policy"] in ("never", "fallback")
    assert len(d["endpoints"]) >= 4


def test_get_source_meta_404(client):
    assert client.get("/v1/sources/压根没有这个源").status_code == 404


def test_describe_endpoint(client):
    d = client.get("/v1/sources/crossref/endpoints/works").json()["data"]
    ep = d["endpoint"]
    assert ep["method"] == "GET"
    assert ep["url_template"].startswith("https://api.crossref.org")
    assert ep["response"]["item_path"] == "message.items"
    assert ep["response"]["total_field"] == "message.total-results"
    assert ep["params"], "端点必须声明请求参数"


def test_describe_endpoint_404(client):
    assert client.get("/v1/sources/crossref/endpoints/nope").status_code == 404


def test_query_unknown_source_returns_404(client):
    r = client.post("/v1/query", json={"source": "没有这个源", "endpoint": "x"})
    assert r.status_code == 404


def test_query_envelope_shape(client):
    """真实查询会走网络; 这里只断言信封结构与错误处理是稳定的。"""
    r = client.post("/v1/query", json={"source": "sec_edgar", "endpoint": "不存在的端点"})
    d = r.json()
    assert "meta" in d and "data" in d and "fields" in d
    assert d["meta"]["budget_ms"] <= 500


def test_query_get_form(client):
    r = client.get("/v1/query?source=sec_edgar&endpoint=nope&params={}")
    assert r.status_code == 200
    assert "meta" in r.json()


def test_stats(client):
    d = client.get("/v1/stats").json()
    assert d["sources"] >= 90
    assert d["endpoints"] >= 100
    assert d["budget_ms"] <= 500
    assert "cache" in d and "prewarm" in d


async def test_mcp_tools_are_registered():
    from a_source_gateway.mcp_server import list_tools

    tools = await list_tools()
    names = {t.name for t in tools}
    assert names == {"list_sources", "get_source_meta", "describe_endpoint",
                     "query_source", "get_meta_schema"}
    for t in tools:
        assert t.description, f"{t.name} 缺描述"
        assert t.inputSchema["type"] == "object"
