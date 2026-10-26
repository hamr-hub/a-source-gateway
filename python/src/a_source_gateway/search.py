"""
search.py - POST /v1/search 的共享实现(一次请求扇出到多个数据源)

这个模块是 **HTTP 路由** 与 **MCP 本地回落** 共用的唯一一份扇出逻辑:
两边必须走同一段代码, 否则两个平面的语义会漂移。

与 Go 网关(gateway/internal/server/search.go)逐条对齐的要点:

1. **两种形态**, `queries` 优先:
   - A) 显式扇出: ``{"queries": [{source, endpoint, params, limit, fields}], "limit": N}``
   - B) 关键词扇出: ``{"q": "keyword", "limit": N, "max_sources": M}``
2. **单源失败不影响整体**: 未知源 / 未知端点 / 上游超时都只写进该条结果的
   ``errors[]``, 整体仍然返回 200。这与 ``POST /v1/query``(未知源返回 404)
   **刻意不同** —— 搜索是"广度优先"的活, 一条源挂了不该拖垮其余的源;
   调用方从 ``meta.failed`` / ``results[i].errors`` 就能分清"没命中"和"坏了"。
3. **并发扇出**: 每个源各自 ≤500ms 预算, 整体墙钟 ≈ 最慢的那个源
   (``asyncio.gather``), 而不是所有源耗时之和。
4. **参数键序原样保留**: 渲染出的 URL 会进缓存键, 重排键会让缓存不命中
   (同一个查询在 ``/v1/query`` 缓存过的条目就白缓存了)。
5. **校验错误手工构造**: 422 的响应体是 FastAPI 的 detail 形状
   (type / loc / msg / input), ``loc`` 里的嵌套字段写成**单段字符串**
   (``"body.queries.0.source"``), 与 Go 网关逐字节一致 —— 所以这里不能
   依赖 pydantic 的自动校验(pydantic 生成的 loc 是分段数组, 形状对不上)。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .schema import DEFAULT_BUDGET_MS, SCHEMA_VERSION, QueryRequest

# 关键词形态默认扇出多少个源: 一个宽泛的关键词能命中几十个源, 全查一遍等于
# 把一次请求变成一波上游流量 —— 而限速正是那些官方站授权我们访问的条件。
DEFAULT_MAX_SOURCES = 12
MAX_MAX_SOURCES = 50
# 单次查询默认返回多少条, 以及硬上限(超出夹到区间内, 不报错)。
DEFAULT_LIMIT = 5
MAX_LIMIT = 100
# 通用透传端点 id(见 registry.generic_endpoint): 源没声明专用端点时的兜底。
GENERIC_ENDPOINT_ID = "entry"

# MCP 紧凑版保留的 meta 字段(其余是单个源的明细, 由 results 承载)。
COMPACT_META_KEYS = ("requested", "ok", "degraded", "failed", "with_data",
                     "budget_ms", "latency_ms", "schema_version")


# ── 校验 ──────────────────────────────────────────────────────────────────
def _detail(typ: str, loc: list[str], msg: str, value: Any = None) -> dict[str, Any]:
    """一条 FastAPI 形状的校验错误(type / loc / msg / input)。"""
    return {"type": typ, "loc": loc, "msg": msg, "input": value}


def json_invalid_detail() -> list[dict[str, Any]]:
    """请求体不是合法 JSON。"""
    return [_detail("json_invalid", ["body"], "Invalid JSON body", None)]


def _to_int(value: Any) -> int | None:
    """JSON 值 -> 整数; 不是数字返回 None。

    与 Go 侧 ``parser.ToInt`` 对齐: 布尔**不算**数字(Python 里 bool 是 int
    的子类, 必须显式排除, 否则 ``true`` 会被当成 1); 浮点按截断取整。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _clamped_int(body: dict[str, Any], key: str, default: int,
                 lo: int, hi: int) -> tuple[int, list[dict[str, Any]]]:
    """读一个可选整数字段: 不是整数报错, 越界则夹到区间内(不报错)。"""
    raw = body.get(key)
    if raw is None:
        return default, []
    n = _to_int(raw)
    if n is None:
        return default, [_detail("int_type", ["body", key],
                                  "Input should be a valid integer", raw)]
    return max(lo, min(hi, n)), []


@dataclass
class SearchEntry:
    """A 形态里的一条查询(还没变成 QueryRequest)。"""
    source: str
    endpoint: str = GENERIC_ENDPOINT_ID
    params: dict[str, Any] = field(default_factory=dict)
    limit: int = 0
    fields: list[str] = field(default_factory=list)


def _parse_entries(body: dict[str, Any]) -> tuple[list[SearchEntry], list[dict[str, Any]]]:
    """解析 ``queries`` 数组; 有错就返回一条 detail。"""
    raw = body.get("queries")
    if raw is None:                       # 没给 queries -> 走关键词形态
        return [], []
    if not isinstance(raw, list):
        return [], [_detail("list_type", ["body", "queries"],
                            "Input should be a valid list", raw)]
    entries: list[SearchEntry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], [_detail("dict_type", ["body", f"queries.{i}"],
                                "Input should be a valid dictionary", item)]
        loc = f"queries.{i}"
        raw_source = item.get("source")
        if raw_source is None:
            return [], [_detail("missing", ["body", f"{loc}.source"],
                                "Field required", None)]
        if not isinstance(raw_source, str):
            return [], [_detail("string_type", ["body", f"{loc}.source"],
                                "Input should be a valid string", raw_source)]
        if not raw_source:
            # 空串等价于没给: 一个空的源 id 拿不到任何数据, 与其让它在结果里
            # 变成一条 not_found, 不如在入口就报"缺参数"。
            return [], [_detail("missing", ["body", f"{loc}.source"],
                                "Field required", None)]
        entry = SearchEntry(source=raw_source)

        raw_endpoint = item.get("endpoint", None)
        if raw_endpoint is not None:
            if not isinstance(raw_endpoint, str):
                return [], [_detail("string_type", ["body", f"{loc}.endpoint"],
                                    "Input should be a valid string", raw_endpoint)]
            entry.endpoint = raw_endpoint or GENERIC_ENDPOINT_ID

        raw_params = item.get("params", None)
        if raw_params is not None:
            if not isinstance(raw_params, dict):
                return [], [_detail("dict_type", ["body", f"{loc}.params"],
                                    "Input should be a valid dictionary", raw_params)]
            # 键序原样保留: 渲染出的 URL 会进缓存键, 重排键会让缓存不命中。
            entry.params = dict(raw_params)

        raw_fields = item.get("fields", None)
        if raw_fields is not None:
            if not isinstance(raw_fields, list):
                return [], [_detail("list_type", ["body", f"{loc}.fields"],
                                    "Input should be a valid list", raw_fields)]
            for j, f in enumerate(raw_fields):
                if not isinstance(f, str):
                    return [], [_detail("string_type", ["body", f"{loc}.fields.{j}"],
                                        "Input should be a valid string", f)]
                entry.fields.append(f)

        raw_limit = item.get("limit", None)
        if raw_limit is not None:
            n = _to_int(raw_limit)
            if n is None:
                return [], [_detail("int_type", ["body", f"{loc}.limit"],
                                    "Input should be a valid integer", raw_limit)]
            entry.limit = n

        entries.append(entry)
    return entries, []


def validate_search_body(body: Any) -> list[dict[str, Any]]:
    """校验请求体; 返回 FastAPI 形状的 detail 列表(空列表 = 通过)。

    校验顺序与 Go 网关一致: limit -> max_sources -> queries[i] -> q,
    这样同一个请求在两个平面上报的是同一条错误。
    """
    if not isinstance(body, dict):
        return [_detail("model_attributes_type", ["body"],
                        "Input should be a valid dictionary or object to extract fields from",
                        None)]
    _, err = _clamped_int(body, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    if err:
        return err
    _, err = _clamped_int(body, "max_sources", DEFAULT_MAX_SOURCES, 1, MAX_MAX_SOURCES)
    if err:
        return err
    entries, err = _parse_entries(body)
    if err:
        return err
    if entries:
        return []
    # 没有显式查询 -> 关键词形态: q 缺失或为空串都不行。
    keyword = body.get("q")
    if keyword is None:
        return [_detail("missing", ["body", "queries"],
                        'Field required: provide "queries" or "q"', None)]
    if not isinstance(keyword, str):
        return [_detail("string_type", ["body", "q"],
                        "Input should be a valid string", keyword)]
    if not keyword:
        return [_detail("missing", ["body", "q"],
                        'Field required: provide "queries" or "q"', None)]
    return []


# ── 计划(请求体 -> 查询列表) ──────────────────────────────────────────────
def build_search_plan(resolver: Any, body: dict[str, Any]
                      ) -> tuple[list[QueryRequest], list[str]]:
    """把请求体变成 (查询列表, 名字列表); 名字是 ``"<source>/<endpoint>"``。

    要求 body 已经通过 :func:`validate_search_body`。
    """
    limit, _ = _clamped_int(body, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    max_sources, _ = _clamped_int(body, "max_sources", DEFAULT_MAX_SOURCES, 1, MAX_MAX_SOURCES)
    entries, _ = _parse_entries(body)

    reqs: list[QueryRequest] = []
    names: list[str] = []
    if entries:
        for e in entries:
            ep = e.endpoint or GENERIC_ENDPOINT_ID
            row_limit = e.limit if e.limit > 0 else limit
            row_limit = min(row_limit, MAX_LIMIT)
            reqs.append(QueryRequest(source=e.source, endpoint=ep,
                                     params=dict(e.params), fields=list(e.fields),
                                     limit=row_limit, timeout_ms=DEFAULT_BUDGET_MS))
            names.append(f"{e.source}/{ep}")
        return reqs, names

    # 关键词形态: 用与 GET /v1/sources?q= 完全相同的搜索索引选源,
    # 每个源取声明顺序里第一个 id != "entry" 的端点。
    #
    # 顺序很重要: **先滤掉"没有真实端点"的源, 再按 max_sources 截断** ——
    # 否则一个只有通用端点(entry)的源会白占一个名额, 让调用方少查一个
    # 真正可用的源(max_sources 的语义是"最多实际查询 N 个源")。
    keyword = str(body.get("q") or "")
    if not keyword:
        # 空关键词会让索引匹配到**所有**源(等于一次请求打遍全网) ——
        # 入口已经拦下了, 这里再挡一道, 防止绕过校验直接调本函数。
        return reqs, names
    for item in resolver.list_sources(family="", q=keyword):
        if len(reqs) >= max_sources:
            break
        sid = str(item.get("id") or "")
        if not sid:
            continue
        src = resolver.get_source(sid)
        if not src:
            continue
        first_ep = next((x for x in src.get("endpoints", [])
                         if x.get("id") != GENERIC_ENDPOINT_ID), None)
        if first_ep is None:
            continue
        reqs.append(QueryRequest(source=sid, endpoint=first_ep["id"],
                                 params=resolver._default_params(first_ep),  # noqa: SLF001
                                 fields=[], limit=limit, timeout_ms=DEFAULT_BUDGET_MS))
        names.append(f"{sid}/{first_ep['id']}")
    return reqs, names


# ── 执行 ──────────────────────────────────────────────────────────────────
def new_trace_id() -> str:
    """生成一个 trace id(32 位十六进制, 与 Go 侧 envelope.NewTraceID 同形)。"""
    return uuid.uuid4().hex


async def _run_one(resolver: Any, req: QueryRequest) -> Any:
    """跑一个源的信封。异常不让它冒到整批搜索上(单源失败不影响整体)。"""
    return await resolver.query(req)


async def run_search(resolver: Any, body: dict[str, Any], *, trace_id: str = "",
                     started_at: float | None = None) -> dict[str, Any]:
    """执行一次搜索, 返回响应体。

    每个源各自 ≤500ms 预算、并发执行; 单源失败只写进该条结果的 errors[]。
    """
    t0 = time.perf_counter() if started_at is None else started_at
    reqs, names = build_search_plan(resolver, body)
    envelopes = await asyncio.gather(*[_run_one(resolver, r) for r in reqs],
                                     return_exceptions=True)

    results: list[dict[str, Any]] = []
    ok = degraded = failed = with_data = 0
    for env in envelopes:
        if isinstance(env, BaseException):
            # resolver 内部已经把上游异常兜成降级信封了, 走到这里说明是网关自己
            # 的问题 —— 一条源炸了不能拖垮整个搜索, 如实记成失败即可。
            results.append({
                "source": "", "endpoint": "", "degraded": False, "cached": False,
                "latency_ms": 0, "count": 0, "total": None, "data": None,
                "fields": [],
                "errors": [{"type": "internal_error", "message": str(env)[:300]}],
            })
            failed += 1
            continue
        m = env.meta
        item: dict[str, Any] = {
            "source": m.source,
            "endpoint": m.endpoint,
            "degraded": m.degraded,
            "cached": m.cached,
            "latency_ms": m.latency_ms,
            "count": m.count,
            "total": m.total,
            "data": env.data,
            "fields": env.fields,
        }
        if m.fallback:
            # 只有降级且给了直连指引时才有这个键(与 Go 侧一致)。
            item["fallback"] = m.fallback
        # errors 始终在场(没有错误就是空数组), 调用方不用 .get 兜底。
        item["errors"] = [{"type": e.get("type", ""), "message": e.get("message", "")}
                          for e in m.errors]
        results.append(item)
        if m.degraded:
            degraded += 1
        elif m.errors:
            # 没降级但有错误: 未知源 / 未知端点这类硬失败。
            failed += 1
        else:
            ok += 1
        if m.count > 0:
            with_data += 1

    elapsed_ms = (time.perf_counter() - t0) * 1000
    latency_ms = int(round(elapsed_ms))
    if latency_ms < 1:
        latency_ms = 1
    budget_ms = int(getattr(getattr(resolver, "cfg", None), "budget_ms", None)
                    or DEFAULT_BUDGET_MS)
    meta = {
        # requested = ok + degraded + failed: 三类互斥, 调用方一眼能看出
        # "多少条真拿到了 / 多少条降级了 / 多少条直接失败了"。
        "requested": len(reqs),
        "ok": ok,
        "degraded": degraded,
        "failed": failed,
        "with_data": with_data,
        "budget_ms": budget_ms,
        "latency_ms": latency_ms,
        "trace_id": trace_id or new_trace_id(),
        "schema_version": SCHEMA_VERSION,
    }
    return {"meta": meta, "queries": names, "results": results}


async def search(resolver: Any, payload: Any, *, trace_id: str = ""
                 ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
    """校验并执行一次搜索。

    返回 ``(响应体, detail 列表)``: 请求被拒时响应体是 None、detail 非空;
    通过时 detail 是 None。由调用方决定状态码(HTTP 路由用 422)。
    """
    detail = validate_search_body(payload)
    if detail:
        return None, detail
    return await run_search(resolver, payload, trace_id=trace_id), None


def compact_search(doc: dict[str, Any], limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """搜索结果的紧凑版: 每个源只留概要, data 按 limit 截断。

    MCP 工具用: 一次扇出十几个源, 全文塞回给模型既费 token 又淹掉重点。
    """
    meta_in = doc.get("meta") or {}
    meta = {k: meta_in.get(k) for k in COMPACT_META_KEYS}
    results: list[dict[str, Any]] = []
    for r in doc.get("results") or []:
        data = r.get("data")
        if isinstance(data, list) and limit > 0:
            data = data[:limit]
        results.append({
            "source": r.get("source", ""),
            "endpoint": r.get("endpoint", ""),
            "count": r.get("count", 0),
            "total": r.get("total"),
            "degraded": r.get("degraded", False),
            "errors": r.get("errors") or [],
            "data": data,
        })
    return {"meta": meta, "queries": list(doc.get("queries") or []), "results": results}
