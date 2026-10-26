"""HTTP API 与 MCP 工具的测试(离线)。"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from a_source_gateway.api import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def offline_upstream(client):
    """把网关的 httpx 客户端换成 MockTransport: 扇出全离线, 不触真实上游。

    返回值是"实际打了哪些上游 URL"的列表(键序是否保留靠它断言)。
    """
    from a_source_gateway import api as api_mod

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"message": {"items": [{"a": 1}, {"a": 2}],
                                                     "total-results": 42}})

    r = api_mod._r()
    old_client = r._client
    r._client = httpx.AsyncClient(timeout=httpx.Timeout(None),
                                  transport=httpx.MockTransport(handler))
    r.cache.clear()
    try:
        yield calls
    finally:
        r._client = old_client
        r.cache.clear()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_root_lists_routes_and_all_mcp_tools(client):
    d = client.get("/").json()
    assert d["sources"] >= 90
    assert "POST /v1/search" in d["routes"], "根路由必须列出新的搜索端点"
    tools = d["mcp"]["tools"]
    assert set(tools) == {"list_sources", "get_source_meta", "describe_endpoint",
                          "query_source", "get_meta_schema", "search_sources"}, \
        "根路由必须列全 MCP 工具"


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


def test_list_sources_search_covers_id_alias_and_endpoint_id(client):
    """回归: 以前 q 只匹配 name/summary/org/country/family, 搜不到 id 与别名。

    "能按别名调用、却搜不到"是最难自查的一类缺陷 —— 调用方根本不知道该
    搜什么词, 于是以为网关没有这个源。
    """
    by_id = client.get("/v1/sources?q=sec_edgar").json()
    assert by_id["meta"]["count"] > 0
    assert "sec_edgar" in [s["id"] for s in by_id["data"]]

    by_alias = client.get("/v1/sources?q=szse").json()
    assert "szse" in [s["id"] for s in by_alias["data"]]

    by_endpoint = client.get("/v1/sources?q=submissions").json()
    assert "sec_edgar" in [s["id"] for s in by_endpoint["data"]]


def test_list_sources_projection_exposes_aliases(client):
    """投影带 aliases(键始终在场, 没有别名时是空数组)。"""
    d = client.get("/v1/sources?q=szse").json()
    assert "aliases" in d["fields"]
    hit = next(s for s in d["data"] if s["id"] == "szse")
    assert hit["aliases"] == ["szse", "szse_stock"]
    for s in client.get("/v1/sources?limit=500").json()["data"]:
        assert isinstance(s["aliases"], list)


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
                     "query_source", "get_meta_schema", "search_sources"}
    for t in tools:
        assert t.description, f"{t.name} 缺描述"
        assert t.inputSchema["type"] == "object"


# ── POST /v1/search: 一次请求扇出到多个数据源 ─────────────────────────────
def test_search_explicit_fanout_returns_one_result_per_query(client, offline_upstream):
    """A 形态: 每条 query 一条 result, 顺序一致; 一个源失败不影响整体。"""
    r = client.post("/v1/search", json={
        "queries": [
            {"source": "crossref", "endpoint": "works", "params": {"query": "制裁"}},
            {"source": "没有这个源", "endpoint": "x"},
        ],
        "limit": 2,
    })
    assert r.status_code == 200
    d = r.json()
    assert len(d["results"]) == 2
    assert d["queries"] == ["crossref/works", "没有这个源/x"]
    assert [x["source"] for x in d["results"]] == ["crossref", "没有这个源"]
    # 真拿到数据的那条: 有数据、有条数、没有错误
    assert d["results"][0]["count"] == 2
    assert d["results"][0]["data"]
    assert d["results"][0]["errors"] == []
    # 失败的那条: 错误写在自己身上, 整体仍是 200
    assert d["results"][1]["errors"][0]["type"] == "not_found"
    meta = d["meta"]
    assert meta["requested"] == 2
    assert meta["ok"] == 1
    assert meta["failed"] == 1
    assert meta["with_data"] == 1
    assert meta["requested"] == meta["ok"] + meta["degraded"] + meta["failed"]
    assert meta["budget_ms"] == 500
    assert meta["latency_ms"] >= 1
    assert offline_upstream, "扇出必须真的打了上游"


def test_search_unknown_source_keeps_the_whole_request_at_200(client):
    """未知源只是这一条失败 —— 与 POST /v1/query 的 404 刻意不同。"""
    r = client.post("/v1/search", json={"queries": [{"source": "没有这个源", "endpoint": "x"}]})
    assert r.status_code == 200
    d = r.json()
    assert d["results"][0]["errors"][0]["type"] == "not_found"
    assert d["meta"]["failed"] == 1
    assert d["meta"]["ok"] == 0


def test_search_requires_queries_or_q(client):
    r = client.post("/v1/search", json={})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "queries"]
    assert detail[0]["input"] is None


def test_search_empty_q_points_at_q_itself(client):
    """q 是空串 -> 缺参数。

    loc 指向 "q" 自己(而不是 "queries"): 与 Go 网关 search.go 的
    searchRequests 逐条一致 —— q 没给时 loc 才是 ["body", "queries"]。
    """
    r = client.post("/v1/search", json={"q": ""})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "q"]
    assert detail[0]["msg"] == 'Field required: provide "queries" or "q"'


def test_search_rejects_a_body_that_is_not_json(client):
    r = client.post("/v1/search", content=b"{not json",
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "json_invalid"
    assert detail[0]["loc"] == ["body"]


def test_search_queries_must_be_a_list(client):
    r = client.post("/v1/search", json={"queries": {}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "list_type"
    assert detail[0]["loc"] == ["body", "queries"]


def test_search_params_must_be_an_object(client):
    r = client.post("/v1/search", json={"queries": [{"source": "crossref", "params": []}]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "dict_type"
    assert detail[0]["loc"] == ["body", "queries.0.params"]


def test_search_entry_source_is_required(client):
    r = client.post("/v1/search", json={"queries": [{"endpoint": "works"}]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "queries.0.source"]


def test_search_empty_source_is_reported_as_missing(client):
    """空串等价于"没给 source": 空的源 id 拿不到任何数据, 在入口就报缺参数。"""
    r = client.post("/v1/search", json={"queries": [{"source": ""}]})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "queries.0.source"]
    assert detail[0]["msg"] == "Field required"


def test_search_empty_endpoint_falls_back_to_the_generic_one(client):
    """endpoint 是空串 -> 等价于没给, 用默认的 entry(通用透传端点), 不是 422。"""
    r = client.post("/v1/search", json={"queries": [{"source": "crossref", "endpoint": ""}]})
    assert r.status_code == 200
    assert r.json()["queries"] == ["crossref/entry"]


def test_search_omits_the_fallback_key_when_there_is_no_hint(client, offline_upstream):
    """fallback 只在"有直连指引"时出现 —— 否则每条结果都带一个空的 fallback:{}。"""
    r = client.post("/v1/search", json={"queries": [{"source": "crossref", "endpoint": "works"}]})
    assert r.status_code == 200
    item = r.json()["results"][0]
    assert "fallback" not in item
    assert item["errors"] == []


def test_search_limit_must_be_an_integer(client):
    r = client.post("/v1/search", json={"q": "crossref", "limit": "3"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["type"] == "int_type"
    assert detail[0]["loc"] == ["body", "limit"]


def test_search_clamps_limit_instead_of_rejecting(client, offline_upstream):
    """limit 越界是夹到区间内(不是报错) —— 与 Go 网关一致。"""
    r = client.post("/v1/search", json={
        "queries": [{"source": "crossref", "endpoint": "works"}], "limit": 0})
    assert r.status_code == 200
    assert r.json()["results"][0]["count"] == 1, "limit=0 应当被夹到 1"


def test_search_keyword_form_selects_sources(client, offline_upstream):
    """B 形态: 用关键词选源, 每个源取第一个非通用端点。"""
    r = client.post("/v1/search", json={"q": "crossref", "limit": 2, "max_sources": 3})
    assert r.status_code == 200
    d = r.json()
    assert "crossref/works" in d["queries"]
    assert d["meta"]["requested"] == len(d["queries"]) == len(d["results"])
    assert d["results"][0]["count"] == 2
    assert offline_upstream, "关键词形态必须真的打了上游"


def test_search_max_sources_counts_only_queryable_sources(client, offline_upstream):
    """max_sources 是"最多实际查询 N 个源"。

    只有通用端点(entry)的源不占名额 —— 否则它会白占一个, 让调用方少查一个
    真正可用的源。期望值直接从注册表算, 不写死(清单会变)。
    """
    from a_source_gateway import api as api_mod

    resolver = api_mod._r()
    matched = resolver.list_sources(family="", q="sec")
    queryable = [s for s in matched
                 if any(e["id"] != "entry"
                        for e in resolver.get_source(s["id"]).get("endpoints", []))]
    expected = min(2, len(queryable))
    assert expected >= 1, "测试前提: 关键词应当命中至少一个可查询的源"

    r = client.post("/v1/search", json={"q": "sec", "limit": 1, "max_sources": 2})
    assert r.status_code == 200
    d = r.json()
    assert d["meta"]["requested"] == expected
    assert len(d["queries"]) == expected
    assert all(not q.endswith("/entry") for q in d["queries"]), \
        "只有通用端点的源不该被查"


def test_search_keeps_the_param_key_order(client, offline_upstream):
    """params 的键序必须原样保留: 渲染出的 URL 会进缓存键, 重排键会让缓存不命中。"""
    client.post("/v1/search", json={"queries": [
        {"source": "crossref", "endpoint": "works", "params": {"a": "1", "b": "2"}}]})
    first = offline_upstream[-1]
    client.post("/v1/search", json={"queries": [
        {"source": "crossref", "endpoint": "works", "params": {"b": "2", "a": "1"}}]})
    assert offline_upstream[-1] != first, "params 的键序不能被重排"


def test_openapi_declares_the_search_request_body(client):
    doc = client.get("/openapi.json").json()
    op = doc["paths"]["/v1/search"]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"queries", "q", "limit", "max_sources"}
    item = schema["properties"]["queries"]["items"]
    assert item["required"] == ["source"]
    assert set(item["properties"]) == {"source", "endpoint", "params", "limit", "fields"}
