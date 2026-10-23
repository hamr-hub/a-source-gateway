"""需要访问真实上游的用例。

默认不跑(``pytest`` 会跳过 ``network`` 标记), 用 ``make network-test`` 或
``pytest -m network`` 显式开启。

这些用例验证的是**与真实上游的契约**: 端点可用、总数对得上、限速与 UA 政策被遵守。
"""
from __future__ import annotations

import os

import pytest

from a_source_gateway.config import load_config
from a_source_gateway.registry import build_registry
from a_source_gateway.resolver import Resolver
from a_source_gateway.schema import QueryRequest

pytestmark = pytest.mark.network

CONTACT = os.environ.get("A_GATEWAY_CONTACT", "")


@pytest.fixture(scope="module")
async def resolver():
    cfg = load_config()
    r = Resolver(build_registry(cfg), cfg=cfg)
    await r.startup()
    yield r
    await r.shutdown()


async def _warm_and_query(r: Resolver, source: str, endpoint: str, params: dict):
    """先让后台补数把缓存填上, 再取一次(返回信封)。"""
    req = QueryRequest(source=source, endpoint=endpoint, params=params, limit=3)
    env = await r.query(req, budget_ms=r.cfg.warm_budget_ms)
    if env.meta.count == 0:
        import asyncio
        for _ in range(40):
            await asyncio.sleep(1)
            if not r._warming:
                break
        env = await r.query(req)
    return env


async def test_gleif_total_matches(resolver):
    env = await _warm_and_query(resolver, "gleif_lei", "lei_records", {"size": 3})
    assert env.meta.total and env.meta.total > 3_000_000
    assert env.meta.count == 3


async def test_crossref_filtered_total(resolver):
    env = await _warm_and_query(resolver, "crossref", "works",
                                {"filter": "from-index-date:2026-10-21"})
    assert env.meta.total and env.meta.total > 0
    assert env.meta.count == 3


async def test_openalex_total(resolver):
    env = await _warm_and_query(resolver, "openalex", "works",
                                {"filter": "from_publication_date:2026-09-22"})
    assert env.meta.total and env.meta.total > 1000


async def test_sec_requires_contact(resolver):
    """SEC 没有联系邮箱会 403; 网关应给出可执行提示而不是让用户撞墙。"""
    if not CONTACT:
        env = await resolver.query(QueryRequest(source="sec_edgar", endpoint="submissions",
                                                params={"cik": "0000320193"}, limit=1))
        assert env.meta.degraded
        assert env.meta.errors[0]["type"] == "contact_required"
        assert "A_GATEWAY_CONTACT" in env.meta.fallback.get("hint", "")
    else:
        env = await _warm_and_query(resolver, "sec_edgar", "submissions",
                                    {"cik": "0000320193"})
        assert env.meta.count == 3
        assert env.meta.total == 1002


async def test_ofac_file_endpoint_is_marked_too_large(resolver):
    """29MB 的文件型端点不该被整体透传, 而应提示直连上游。"""
    env = await resolver.query(QueryRequest(source="ofac_sdn", endpoint="sdn_xml", limit=1),
                               budget_ms=resolver.cfg.warm_budget_ms)
    assert env.meta.degraded
    assert env.meta.errors[0]["type"] in ("response_too_large", "timeout")
    assert env.meta.fallback.get("upstream_url")


async def test_budget_is_respected_even_on_slow_source(resolver):
    """500ms 契约: 即使上游很慢, 也要在预算内返回。"""
    import time
    t0 = time.perf_counter()
    await resolver.query(QueryRequest(source="eurostat", endpoint="toc", limit=3))
    ms = (time.perf_counter() - t0) * 1000
    assert ms < 500, f"超出预算: {ms:.0f}ms"
