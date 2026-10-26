"""bench 判据的单测: 延迟契约与数据正确性必须是两个维度。

背景: 早期版本的 bench 只看 p95 <= 500ms, 于是"10 个用例全部降级、count 全是 0"
也会打印一句 "PASS" —— 最该被看到的信号被"通过"两个字盖过去了。docs/performance.md
§2.2 要求两条判据分开报, 这里把那条要求钉成测试。
"""
from __future__ import annotations

from typing import Any

from a_source_gateway.bench import BUDGET_MS, _verdict, build_summary


def _case(name: str, p95: float, count: int = 0, degraded: bool = True,
          kind: str = "query", cached: int = 0, n: int = 3) -> dict[str, Any]:
    return {"kind": kind, "name": name, "status": 200, "p50": p95, "p95": p95,
            "max": p95, "n": n, "degraded": degraded, "cached": cached,
            "count": count, "total": count}


def _cold(name: str, ms: float, degraded: bool = True) -> dict[str, Any]:
    return {"name": name, "status": 200, "ms": ms, "degraded": degraded}


def test_all_degraded_but_within_budget_is_not_a_clean_pass():
    """延迟成立但一条数据都没拿到: pass 为真, data_ok 必须为假。"""
    results = [_case("a", 12.0), _case("b", 9.0)]
    cold = [_cold("a", 300.0), _cold("b", 280.0)]
    s = build_summary(results, cold, rounds=3)
    assert s["pass"] is True
    assert s["latency_ok"] is True
    assert s["data_ok"] is False
    assert s["data_missing"] == ["a", "b"]
    assert s["data_with_count"] == 0
    assert s["data_total"] == 2
    assert "数据正确性未达标" in s["verdict"]
    assert "2/2" in s["verdict"]


def test_data_present_makes_both_criteria_pass():
    results = [_case("a", 12.0, count=5, degraded=False),
               _case("b", 9.0, count=3, degraded=False)]
    cold = [_cold("a", 300.0, degraded=False), _cold("b", 280.0, degraded=False)]
    s = build_summary(results, cold, rounds=3)
    assert s["pass"] is True
    assert s["data_ok"] is True
    assert s["data_missing"] == []
    assert s["data_with_count"] == 2
    assert s["verdict"].startswith("PASS")


def test_over_budget_fails_the_latency_criterion():
    results = [_case("a", BUDGET_MS + 1.0, count=5, degraded=False)]
    cold = [_cold("a", 300.0, degraded=False)]
    s = build_summary(results, cold, rounds=3)
    assert s["pass"] is False
    assert s["latency_ok"] is False
    assert s["over_budget_cases"] == ["a"]
    assert s["verdict"].startswith("FAIL")


def test_cold_path_over_budget_fails_the_latency_criterion():
    """热路径命中缓存不代表契约成立: 冷路径超预算同样要判 FAIL。"""
    results = [_case("a", 1.0, count=5, degraded=False, cached=3)]
    cold = [_cold("a", BUDGET_MS + 1.0, degraded=False)]
    s = build_summary(results, cold, rounds=3)
    assert s["pass"] is False
    assert s["cold_path"]["over_budget"] == ["a"]
    assert s["verdict"].startswith("FAIL")


def test_meta_cases_are_not_counted_as_data_cases():
    """meta 用例没有 count, 不该被算进"没有数据"的名单里。"""
    results = [_case("GET /v1/sources", 0.4, kind="meta", count=0, degraded=False),
               _case("a", 5.0, count=2, degraded=False)]
    cold = [_cold("a", 100.0, degraded=False)]
    s = build_summary(results, cold, rounds=3)
    assert s["data_total"] == 1
    assert s["data_ok"] is True
    assert s["data_missing"] == []


def test_warm_p95_is_none_when_no_case_is_fully_cached():
    """没有全缓存用例时 warm p95 是 None, 打印不能变成 "Nonems"。"""
    results = [_case("a", 5.0, count=2, degraded=False, cached=0, n=3)]
    cold = [_cold("a", 100.0, degraded=False)]
    s = build_summary(results, cold, rounds=3)
    assert s["query_warm_p95_max_ms"] is None
    from a_source_gateway.bench import print_summary
    print_summary(s)  # 不抛异常即可


def test_verdict_strings_are_distinct():
    assert _verdict(False, False, ["a"], 1).startswith("FAIL")
    assert "仅延迟契约" in _verdict(True, False, ["a"], 1)
    assert _verdict(True, True, [], 1).startswith("PASS")
