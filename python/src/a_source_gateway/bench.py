"""
bench.py - 网关延迟基准

验收标准: **p95 < 500ms**(整个请求从进网关到返回,含上游取数)

结论分两个维度, 不能混成一句 PASS:
  pass     —— 延迟契约成立(冷/热路径 p95、max 都不超 500ms)
  data_ok  —— 数据正确性(热路径的每个用例都 count > 0, 见 docs/performance.md §2.2)
数据正确性取决于上游限流与跨境网络, 不并进 pass; 但它是一个独立字段,
"全部降级"时不会只剩一句 PASS 把信号盖掉。

三类用例:
  meta   —— 纯本地元数据查询(list_sources / get_source_meta / describe_endpoint)
  query  —— 实时取数(打上游, 受 500ms 硬预算约束)
  cache  —— 重复请求(命中缓存)

用法:
    python3 -m a_source_gateway.bench                # 自己拉起服务再测
    python3 -m a_source_gateway.bench --url http://127.0.0.1:8848   # 测已在跑的服务
    python3 -m a_source_gateway.bench --out bench-report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BUDGET_MS = 500

# 实时查询用例: 覆盖不同响应体量与不同源的延迟特征
# 需要 UA 带联系邮箱的源: 未配置 A_GATEWAY_CONTACT 时跳过(否则上游会 403)
CONTACT_REQUIRED = {"sec_edgar"}

QUERY_CASES: list[tuple[str, dict[str, Any]]] = [
    ("sec/submissions", {"source": "sec_edgar", "endpoint": "submissions",
                          "params": {"cik": "0000320193"}, "limit": 3}),
    ("sec/company_tickers", {"source": "sec_edgar", "endpoint": "company_tickers", "limit": 3}),
    ("ctgov/studies", {"source": "clinicaltrials_gov_v2", "endpoint": "studies",
                        "params": {"term": "AREA[LastUpdatePostDate]RANGE[2026-10-01,MAX]"},
                        "limit": 3}),
    ("openalex/works", {"source": "openalex", "endpoint": "works",
                         "params": {"filter": "from_publication_date:2026-09-22"}, "limit": 3}),
    ("crossref/works", {"source": "crossref", "endpoint": "works",
                        "params": {"filter": "from-index-date:2026-10-21"}, "limit": 3}),
    ("gleif/lei_records", {"source": "gleif_lei", "endpoint": "lei_records",
                            "params": {"size": 3}, "limit": 3}),
    ("eurostat/toc", {"source": "eurostat", "endpoint": "toc", "limit": 3}),
    ("worldbank/indicator", {"source": "world_bank_open_data_wdi_wgi", "endpoint": "indicator",
                              "params": {"size": 3}, "limit": 3}),
    ("datacite/dois", {"source": "datacite", "endpoint": "dois", "params": {"size": 3}, "limit": 3}),
    ("orcid/expanded", {"source": "orcid", "endpoint": "expanded_search",
                         "params": {"size": 3}, "limit": 3}),
    ("pubmed/esearch", {"source": "pubmed_e_utilities", "endpoint": "esearch",
                         "params": {"term": "short drama", "size": 3}, "limit": 3}),
    ("gbif/species", {"source": "gbif", "endpoint": "species_search",
                       "params": {"q": "Puma", "size": 3}, "limit": 3}),
]

META_CASES: list[tuple[str, str]] = [
    ("GET /v1/sources", "/v1/sources?limit=100"),
    ("GET /v1/sources/sec_edgar", "/v1/sources/sec_edgar"),
    ("GET /v1/sources/sec_edgar/endpoints/submissions",
     "/v1/sources/sec_edgar/endpoints/submissions"),
    ("GET /v1/schema", "/v1/schema"),
    ("GET /v1/stats", "/v1/stats"),
]

# 搜索用例: POST /v1/search 的关键词扇出。
#
# 搜索的墙钟由**最慢的那一个源**决定(各源并发、各自一份预算),所以它的延迟
# 天然与单源查询同一量级; 把它单列出来, 是为了让这个接口的延迟也有基线可盯 ——
# 新接口没有基线, 就等于没有人看着它变慢。
#
# 关键词只用**国内**的源: 跨境源在本机直连普遍超时, 会把整条用例的 p95 变成
# "网络有多慢", 而不是"网关有多慢"(与 CONTACT_REQUIRED 跳过 SEC 同理)。
SEARCH_CASES: list[tuple[str, dict[str, Any]]] = [
    ("search/交易所", {"q": "交易所", "max_sources": 6, "limit": 3}),
    ("search/证券监管", {"q": "证券监管", "max_sources": 6, "limit": 3}),
]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[i]


async def run_case(client: httpx.AsyncClient, method: str, path: str,
                   body: dict[str, Any] | None = None) -> tuple[int, float, dict[str, Any]]:
    t0 = time.perf_counter()
    try:
        if method == "POST":
            r = await client.post(path, json=body, timeout=5.0)
        else:
            r = await client.get(path, timeout=5.0)
        ms = (time.perf_counter() - t0) * 1000
        try:
            payload = r.json()
        except Exception:
            payload = {}
        return r.status_code, ms, payload
    except Exception as e:
        return 0, (time.perf_counter() - t0) * 1000, {"error": str(e)}


def _cases(contact: str) -> list[tuple[str, dict[str, Any]]]:
    if contact:
        return QUERY_CASES
    skipped = []
    out = []
    for name, body in QUERY_CASES:
        if body.get("source") in CONTACT_REQUIRED:
            skipped.append(name)
            continue
        out.append((name, body))
    if skipped:
        print(f"[skip] 需要 A_GATEWAY_CONTACT, 跳过: {', '.join(skipped)}")
    return out


async def bench(base: str, rounds: int, out_path: Path, cold_rounds: int = 2,
                contact: str = "") -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    cold: list[dict[str, Any]] = []
    cases = _cases(contact)
    async with httpx.AsyncClient(base_url=base, limits=httpx.Limits(max_connections=16)) as client:
        # 预热
        await run_case(client, "GET", "/v1/sources?limit=10")

        # ── 冷路径: 用 _cb 强制 cache miss, 保证每次都是真冷调用 ──
        for i in range(cold_rounds):
            for _name, body in cases:
                b2 = dict(body)
                b2["params"] = dict(body.get("params") or {})
                b2["params"]["_cb"] = f"{int(time.time() * 1000)}-{i}"
                st, ms, payload = await run_case(client, "POST", "/v1/query", b2)
                m = payload.get("meta", {})
                cold.append({"name": _name, "status": st, "ms": round(ms, 1),
                             "degraded": bool(m.get("degraded"))})

        # ── 预热: 用热路径的 key 触发一次后台补数(冷路径的 _cb 键不会填到热键上) ──
        for body in (b for _n, b in cases):
            await run_case(client, "POST", "/v1/query", body)

        # 搜索用例也预热一次: 它扇出的源与单源用例可能重合, 预热让热路径
        # 真的落在缓存上(否则测的是"每个源的首调", 与冷路径那一组重复了)。
        for _name, body in SEARCH_CASES:
            await run_case(client, "POST", "/v1/search", body)

        # 逐个等"缓存真的热了": 轮询到有数据或超过等待上限。
        # 只等 pending==0 会有竞态: 预热任务可能在预热请求返回后才被登记。
        wait_deadline = time.time() + 90
        for _name, body in cases:
            while time.time() < wait_deadline:
                st, ms, payload = await run_case(client, "POST", "/v1/query", body)
                m = payload.get("meta", {})
                if (m.get("count") or 0) > 0 or m.get("cached"):
                    break
                await asyncio.sleep(0.8)

        # ── 等后台补数全部完成(连续两次稳定才算稳, 避免竞态) ──
        deadline = time.time() + 120
        stable = 0
        last_entries = -1
        while time.time() < deadline:
            st, _, payload = await run_case(client, "GET", "/v1/stats")
            pw = payload.get("prewarm", {})
            entries = payload.get("cache", {}).get("entries", -1)
            if pw.get("done") and not pw.get("pending") and entries == last_entries and entries > 0:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last_entries = entries
            await asyncio.sleep(0.6)

        for name, path in META_CASES:
            lat = []
            for _ in range(rounds * 3):
                st, ms, _ = await run_case(client, "GET", path)
                lat.append(ms)
            results.append({"kind": "meta", "name": name, "status": st,
                            "p50": round(statistics.median(lat), 1),
                            "p95": round(_pct(lat, 95), 1),
                            "max": round(max(lat), 1), "n": len(lat)})

        for _name, body in cases:
            lat, degraded, cached = [], 0, 0
            counts, totals = [], []
            for _ in range(rounds):
                st, ms, payload = await run_case(client, "POST", "/v1/query", body)
                lat.append(ms)
                m = payload.get("meta", {})
                degraded += 1 if m.get("degraded") else 0
                cached += 1 if m.get("cached") else 0
                counts.append(m.get("count") or 0)
                totals.append(m.get("total"))
            results.append({"kind": "query", "name": _name, "status": st,
                            "p50": round(statistics.median(lat), 1),
                            "p95": round(_pct(lat, 95), 1),
                            "max": round(max(lat), 1), "n": len(lat),
                            "degraded": degraded, "cached": cached,
                            "count": max(counts), "total": totals[-1]})

        # ── 搜索用例: 扇出的墙钟由最慢的那一个源决定 ──────────────────────
        for _name, body in SEARCH_CASES:
            lat, degraded = [], 0
            counts = []
            for _ in range(rounds):
                st, ms, payload = await run_case(client, "POST", "/v1/search", body)
                lat.append(ms)
                m = payload.get("meta", {})
                degraded += 1 if m.get("degraded") else 0
                # 搜索的"取到几条"是聚合口径: 有几个源拿到了数据。
                counts.append(m.get("with_data") or 0)
            results.append({"kind": "search", "name": _name, "status": st,
                            "p50": round(statistics.median(lat), 1),
                            "p95": round(_pct(lat, 95), 1),
                            "max": round(max(lat), 1), "n": len(lat),
                            "degraded": degraded, "cached": 0,
                            "count": max(counts), "total": None})

    summary = build_summary(results, cold, rounds)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def build_summary(results: list[dict[str, Any]], cold: list[dict[str, Any]],
                  rounds: int) -> dict[str, Any]:
    """把逐次采样汇成一份总结报告。

    单独抽出来是为了能离线单测: 判据逻辑(延迟契约 / 数据正确性)是这个基准
    唯一有价值的部分, 不该只能靠"起一个真服务跑一遍"来验证。
    """
    meta_lat = [r["p95"] for r in results if r["kind"] == "meta"]
    q_lat = [r["p95"] for r in results if r["kind"] == "query"]
    worst = max(results, key=lambda r: r["p95"])
    cold_worst = max(cold, key=lambda r: r["ms"]) if cold else {"name": "-", "ms": 0}
    # 热路径(命中缓存)的 p95 只有零点几毫秒, 拿它当"实时取数延迟"是自欺:
    # 真实冷启动走的是 cold_path 那组。两个数分开报, 别让缓存把结论抹平。
    warm_lat = [r["p95"] for r in results
                if r["kind"] == "query" and r.get("cached", 0) >= r.get("n", 1)]
    cold_lat = sorted(c["ms"] for c in cold)
    cold_degraded = sum(1 for c in cold if c["degraded"])

    # ── 两条判据分开报 ─────────────────────────────────────────────────
    # 延迟契约(doc §2.2 第一行): 冷热路径的 p95/max 都不超过 500ms。
    latency_ok = (all(r["p95"] <= BUDGET_MS for r in results)
                  and all(c["ms"] <= BUDGET_MS for c in cold))
    # 数据正确性(doc §2.2 第二行): 热路径的每个用例都要 count > 0。
    # 这一条**不并进 pass**: 能不能拿到数据取决于上游限流与跨境网络, 不是网关
    # 可控的范围。但它必须是一个独立字段 —— 否则"10 个用例全部降级、count 全 0"
    # 也会打印一句 "PASS", 把最该被看到的信号盖过去。
    #
    # 搜索用例的 count 是聚合口径: 有几个源拿到了数据(with_data)。
    q_cases = [r for r in results if r["kind"] == "query"]
    s_cases = [r for r in results if r["kind"] == "search"]
    data_missing = ([r["name"] for r in q_cases if (r.get("count") or 0) <= 0]
                    + [r["name"] for r in s_cases if (r.get("count") or 0) <= 0])
    data_ok = not data_missing

    return {
        "budget_ms": BUDGET_MS,
        "rounds": rounds,
        "meta_p95_max_ms": round(max(meta_lat), 1) if meta_lat else None,
        "query_p95_max_ms": round(max(q_lat), 1) if q_lat else None,
        "query_p95_median_ms": round(statistics.median(q_lat), 1) if q_lat else None,
        "query_warm_p95_max_ms": round(max(warm_lat), 1) if warm_lat else None,
        "query_cold_p95_ms": round(_pct(cold_lat, 95), 1) if cold_lat else None,
        "worst_case": {"name": worst["name"], "p95": worst["p95"], "max": worst["max"]},
        "cold_path": {
            "worst": {"name": cold_worst["name"], "ms": cold_worst["ms"]},
            "over_budget": [c["name"] for c in cold if c["ms"] > BUDGET_MS],
            "degraded": cold_degraded,
            "degraded_ratio": round(cold_degraded / len(cold), 3) if cold else None,
            "cases": cold,
            "n": len(cold),
        },
        "over_budget_cases": [r["name"] for r in results if r["p95"] > BUDGET_MS],
        # pass 只表示延迟契约成立; 数据正确性看 data_ok。
        "pass": latency_ok,
        "latency_ok": latency_ok,
        "data_ok": data_ok,
        "data_with_count": len(q_cases) + len(s_cases) - len(data_missing),
        "data_total": len(q_cases) + len(s_cases),
        "search_total": len(s_cases),
        "search_with_count": sum(1 for r in s_cases if (r.get("count") or 0) > 0),
        "data_missing": data_missing,
        "verdict": _verdict(latency_ok, data_ok, data_missing,
                            len(q_cases) + len(s_cases)),
        "cases": results,
    }


def _verdict(latency_ok: bool, data_ok: bool, missing: list[str], total: int) -> str:
    """一句话结论: 延迟契约与数据正确性是两个维度, 不能混成一句 PASS。"""
    if not latency_ok:
        return "FAIL — 延迟契约不成立"
    if not data_ok:
        return (f"PASS(仅延迟契约) · 数据正确性未达标: "
                f"{len(missing)}/{total} 个热路径用例没有数据")
    return "PASS — 延迟契约与数据正确性都成立"


def print_summary(summary: dict[str, Any]) -> None:
    print(f"{'类型':<7}{'用例':<36}{'状态':<6}{'p50':>8}{'p95':>8}{'max':>8}{'条数':>7}{'总数':>13}")
    print("-" * 93)
    for r in summary["cases"]:
        flag = "!" if r["p95"] > BUDGET_MS else " "
        tot = r.get("total")
        print(f"{r['kind']:<7}{r['name'][:34]:<36}{str(r['status']):<6}"
              f"{r['p50']:>8.1f}{r['p95']:>8.1f}{r['max']:>8.1f}"
              f"{str(r.get('count','-')):>7}{str(tot if tot is not None else '-'):>13}{flag}")
    print("-" * 93)
    qc = [r for r in summary["cases"] if r["kind"] == "query"]
    with_data = sum(1 for r in qc if (r.get("count") or 0) > 0)
    print(f"热路径有数据用例: {with_data}/{len(qc)}")
    sc = [r for r in summary["cases"] if r["kind"] == "search"]
    if sc:
        s_with = sum(1 for r in sc if (r.get("count") or 0) > 0)
        print(f"搜索用例有数据的源: {s_with}/{len(sc)}(扇出墙钟由最慢的源决定)")
    missing = summary.get("data_missing") or []
    if missing:
        print(f"没有数据的用例: {', '.join(missing)}")
    cached_all = sum(1 for r in qc if r.get("cached", 0) >= r.get("n", 1))
    if cached_all:
        print(f"注意: {cached_all}/{len(qc)} 个 query 用例全程命中缓存, "
              f"上表的 p95 是缓存延迟, 不是实时取数延迟")
    cp = summary["cold_path"]
    print(f"冷路径(每源首调): 最差 {cp['worst']['name']} {cp['worst']['ms']}ms | p95 "
          f"{summary['query_cold_p95_ms']}ms | 降级 {cp['degraded']}/{len(cp['cases'])}"
          f" ({cp['degraded_ratio']}) | 超预算 {cp['over_budget'] or '无'}")
    warm = summary.get("query_warm_p95_max_ms")
    print(f"预算 {summary['budget_ms']}ms | meta p95 最差 {summary['meta_p95_max_ms']}ms | "
          f"query 热路径 p95 最差 {warm if warm is not None else 'n/a(本轮无全缓存用例)'}ms")
    if summary["over_budget_cases"]:
        print(f"超预算用例: {summary['over_budget_cases']}")
    print(f"结论: {summary['verdict']}")


def _wait_ready(base: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/healthz", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def run_bench(url: str = "", rounds: int = 3, cold_rounds: int = 2,
              out: Path | None = None, contact: str = "") -> dict[str, Any]:
    """跑基准: url 为空则自己拉起服务。返回汇总字典。"""
    proc = None
    base = url
    if not base:
        base = f"http://127.0.0.1:{os.environ.get('A_GATEWAY_PORT', '8848')}"
        env = dict(os.environ)
        env.setdefault("A_GATEWAY_PORT", "8848")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "a_source_gateway.api:app",
             "--host", "127.0.0.1", "--port", env["A_GATEWAY_PORT"], "--log-level", "warning"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_ready(base):
            proc.terminate()
            raise RuntimeError("服务启动失败")
        print(f"已拉起网关 {base}\n")
    try:
        return asyncio.run(bench(base, rounds, out or Path("bench-report.json"), cold_rounds,
                                 contact=contact or os.environ.get("A_GATEWAY_CONTACT", "")))
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser(description="A 类数据源网关延迟基准")
    ap.add_argument("--url", default="")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--cold-rounds", type=int, default=2,
                    help="冷路径重复轮数(每轮都用 _cb 强制 cache miss)")
    ap.add_argument("--out", default="bench-report.json")
    args = ap.parse_args()

    proc = None
    base = args.url
    if not base:
        base = f"http://127.0.0.1:{os.environ.get('A_GATEWAY_PORT', '8848')}"
        env = dict(os.environ)
        env.setdefault("A_GATEWAY_PORT", "8848")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "a_source_gateway.api:app",
             "--host", "127.0.0.1", "--port", "8848", "--log-level", "warning"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_ready(base):
            proc.terminate()
            print("服务启动失败")
            sys.exit(1)
        print(f"已拉起网关 {base}\n")

    try:
        summary = asyncio.run(bench(base, args.rounds, Path(args.out), args.cold_rounds))
        print_summary(summary)
        print(f"报告已写入 {args.out}")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    main()
