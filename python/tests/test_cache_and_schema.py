"""缓存与响应信封的测试。"""
from __future__ import annotations

import time

from a_source_gateway.cache import BoundedCache
from a_source_gateway.schema import QueryRequest, ResponseEnvelope, ResponseMeta


# ── 缓存 ──────────────────────────────────────────────────────────────────
def test_cache_hit_and_ttl():
    c = BoundedCache()
    c.put("k", {"v": 1}, ttl_s=60)
    val, fresh, age = c.get("k")
    assert val == {"v": 1} and fresh is True and age == 0


def test_cache_expiry_returns_stale():
    c = BoundedCache()
    c.put("k", "v", ttl_s=0)
    time.sleep(0.01)
    val, fresh, age = c.get("k")
    assert val == "v", "过期后仍应能拿到陈旧值用于降级"
    assert fresh is False
    assert age >= 0


def test_cache_miss():
    c = BoundedCache()
    assert c.get("nope") == (None, False, 0)


def test_cache_respects_max_entries():
    c = BoundedCache(max_entries=3)
    for i in range(10):
        c.put(f"k{i}", i, ttl_s=60)
    assert c.stats()["entries"] == 3
    assert c.get("k9")[0] == 9          # 最新的还在
    assert c.get("k0")[0] is None      # 最老的被淘汰


def test_cache_respects_max_bytes():
    c = BoundedCache(max_bytes=400)
    for i in range(50):
        c.put(f"k{i}", "x" * 50, ttl_s=60)
    assert c.stats()["bytes"] <= 400


def test_cache_skips_oversized_single_entry():
    c = BoundedCache(max_bytes=100)
    c.put("big", "y" * 500, ttl_s=60)
    assert c.get("big")[0] is None


def test_cache_stats():
    c = BoundedCache()
    c.put("k", 1, ttl_s=60)
    c.get("k")
    c.get("miss")
    s = c.stats()
    assert s["hits"] == 1 and s["misses"] == 1
    assert 0 < s["hit_rate"] < 1


# ── 信封 ──────────────────────────────────────────────────────────────────
def test_envelope_contains_documented_keys():
    d = ResponseEnvelope(meta=ResponseMeta(source="s", endpoint="e"), data=[]).to_dict()
    for k in ("meta", "data", "fields"):
        assert k in d
    for k in ("request_id", "schema_version", "source", "endpoint", "upstream",
              "latency_ms", "budget_ms", "cached", "cache_age_s", "degraded",
              "offline", "count", "total", "truncated", "next_cursor",
              "rate_limit", "fallback", "errors"):
        assert k in d["meta"], f"信封 meta 缺字段 {k}"


def test_envelope_fallback_is_always_present():
    """fallback 是对外契约的一部分, 即使为空也必须在。"""
    d = ResponseMeta().to_dict()
    assert "fallback" in d and d["fallback"] == {}


def test_request_serialises_with_schema_version():
    d = QueryRequest(source="s", endpoint="e").to_dict()
    assert d["schema_version"] == "1.2"
    assert d["source"] == "s" and d["endpoint"] == "e"
