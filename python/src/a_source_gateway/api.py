"""
api.py - A 类数据源通用查询 API

只用 FastAPI + httpx, 全部元数据在内存, 不落业务数据。
所有响应统一用"信封"结构: {meta:{...描述这次请求...}, data:[...], fields:[...]}

    GET  /                      服务信息与路由表
    GET  /healthz               存活探针
    GET  /v1/schema             meta 规范本身(JSON Schema)
    GET  /v1/stats              缓存与注册表统计
    GET  /v1/sources            源清单(可按 family/q 过滤, 轻量投影)
    GET  /v1/sources/{sid}      单源完整 meta
    GET  /v1/sources/{sid}/endpoints/{eid}   端点 meta(请求/响应结构)
    POST /v1/query              执行查询 -> 信封
    GET  /v1/query              同上(查询参数形式, 便于浏览器/curl)
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .cache import BoundedCache
from .config import load_config
from .registry import build_registry
from .resolver import Resolver
from .schema import DEFAULT_BUDGET_MS, QueryRequest

CFG = load_config()
OFFLINE = CFG.offline

app = FastAPI(
    title="A-Class Source Gateway",
    version="1.0",
    description="海外 A 类(法规强制公开)数据源的通用元数据 + 实时查询网关。"
                "不存储业务数据, 只存源元数据;所有响应带 meta 描述请求与返回。",
)

_registry: dict[str, Any] = {}
_resolver: Resolver | None = None
_START_TS = time.time()


def _load() -> dict[str, Any]:
    """注册表在运行时由 data/ 下的两份声明式文件构建(约 4ms), 无生成物不同步问题。"""
    global _registry
    if not _registry:
        _registry = build_registry(CFG)
    return _registry


class QueryBody(BaseModel):
    source: str = Field(..., description="源 id, 见 GET /v1/sources")
    endpoint: str = Field(..., description="端点 id, 见 GET /v1/sources/{id}")
    params: dict[str, Any] = Field(default_factory=dict, description="端点参数")
    fields: list[str] = Field(default_factory=list, description="只返回这些字段")
    limit: int = Field(default=20, ge=1, le=500, description="最多返回多少条")
    since: str | None = Field(default=None, description="增量窗口起点(ISO 日期)")
    timeout_ms: int = Field(default=DEFAULT_BUDGET_MS, ge=50, le=DEFAULT_BUDGET_MS,
                            description="总预算, 上限 500ms")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建连接池并后台预热; 关闭时释放连接。"""
    global _resolver
    reg = _load()
    _resolver = Resolver(reg, offline=OFFLINE, cache=BoundedCache(
        max_entries=CFG.cache_entries, max_bytes=CFG.cache_bytes), cfg=CFG)
    await _resolver.startup()
    # 后台预热: 把连接池与缓存热起来, 让冷路径也落在预算内(不阻塞启动)
    if CFG.prewarm:
        asyncio.create_task(_resolver.prewarm())
    yield
    if _resolver:
        await _resolver.shutdown()


app.router.lifespan_context = lifespan


def _r() -> Resolver:
    if _resolver is None:
        raise HTTPException(status_code=503, detail="网关未就绪")
    return _resolver


@app.get("/")
async def root() -> dict[str, Any]:
    reg = _load()
    return {
        "service": "A-Class Source Gateway",
        "version": "1.0",
        "purpose": "不存业务数据;提供 A 类源的元数据索引与 ≤500ms 的实时查询代理",
        "budget_ms": DEFAULT_BUDGET_MS,
        "sources": reg["count"],
        "families": reg["families"],
        "offline": OFFLINE,
        "routes": [
            "GET  /v1/schema",
            "GET  /v1/stats",
            "GET  /v1/sources?family=&q=",
            "GET  /v1/sources/{source_id}",
            "GET  /v1/sources/{source_id}/endpoints/{endpoint_id}",
            "POST /v1/query",
            "GET  /v1/query?source=&endpoint=&params=<json>",
        ],
        "mcp": {
            "command": "python3 -m a_source_gateway.mcp_server",
            "tools": ["list_sources", "get_source_meta", "describe_endpoint",
                      "query_source", "get_meta_schema"],
        },
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    r = _r()
    return {"ok": True, "sources": len(r.sources), "offline": OFFLINE}


@app.get("/metrics")
async def metrics() -> str:
    """Prometheus 文本格式的观测指标(零依赖实现, 无需 prometheus_client)。"""
    r = _r()
    m = r.metrics
    q = max(1, m["queries_total"])
    lines = [
        "# HELP asg_queries_total 总查询数",
        "# TYPE asg_queries_total counter",
        f"asg_queries_total {m['queries_total']}",
        "# HELP asg_cache_hits_total 缓存命中数",
        "# TYPE asg_cache_hits_total counter",
        f"asg_cache_hits_total {m['cache_hits_total']}",
        "# HELP asg_degraded_total 降级返回数",
        "# TYPE asg_degraded_total counter",
        f"asg_degraded_total {m['degraded_total']}",
        "# HELP asg_timeouts_total 上游超时数",
        "# TYPE asg_timeouts_total counter",
        f"asg_timeouts_total {m['timeouts_total']}",
        "# HELP asg_errors_total 错误数",
        "# TYPE asg_errors_total counter",
        f"asg_errors_total {m['errors_total']}",
        "# HELP asg_warm_fills_total 后台补数成功数",
        "# TYPE asg_warm_fills_total counter",
        f"asg_warm_fills_total {m['warm_fills_total']}",
        "# HELP asg_warm_errors_total 后台补数失败数",
        "# TYPE asg_warm_errors_total counter",
        f"asg_warm_errors_total {m.get('warm_errors_total', 0)}",
        "# HELP asg_latency_avg_ms 平均耗时",
        "# TYPE asg_latency_avg_ms gauge",
        f"asg_latency_avg_ms {m['latency_sum_ms'] / q:.2f}",
        "# HELP asg_latency_max_ms 最大耗时",
        "# TYPE asg_latency_max_ms gauge",
        f"asg_latency_max_ms {m['latency_max_ms']:.0f}",
        "# HELP asg_cache_hit_ratio 缓存命中率",
        "# TYPE asg_cache_hit_ratio gauge",
        f"asg_cache_hit_ratio {m['cache_hits_total'] / q:.4f}",
    ]
    for src, v in sorted(m.get("by_source", {}).items()):
        lines.append(f'asg_source_queries{{source="{src}"}} {v["queries"]}')
        lines.append(f'asg_source_degraded{{source="{src}"}} {v["degraded"]}')
        lines.append(f'asg_source_latency_avg_ms{{source="{src}"}} '
                     f'{v["latency_sum"] / max(1, v["queries"]):.2f}')
    return "\n".join(lines) + "\n"


@app.get("/livez")
async def livez() -> dict[str, Any]:
    """存活探针: 进程还在就返回 200(上游不可达时也应为 200)。"""
    return {"ok": True, "uptime_s": round(time.time() - _START_TS, 1)}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """就绪探针: 网关已初始化且注册表可用才算 ready。"""
    reg = _load()
    ok = bool(reg.get("count"))
    return JSONResponse({"ok": ok, "sources": reg.get("count", 0),
                         "offline": OFFLINE}, status_code=200 if ok else 503)


@app.get("/v1/schema")
async def schema() -> dict[str, Any]:
    """meta 规范:描述 SourceSpec / EndpointSpec / 请求 / 响应信封的字段。"""
    return SCHEMA_DOC


@app.get("/v1/stats")
async def stats() -> dict[str, Any]:
    r = _r()
    reg = _load()
    return {
        "sources": reg["count"],
        "families": len(reg["families"]),
        "endpoints": sum(len(s.get("endpoints", [])) for s in reg["sources"]),
        "cache": r.cache.stats(),
        "prewarm": {"done": r.prewarm_done, "states": r.prewarmed,
                    "pending": len(r._warming), "filled": r.warm_fills},
        "offline": OFFLINE,
        "budget_ms": DEFAULT_BUDGET_MS,
    }


@app.get("/v1/sources")
async def list_sources(family: str = Query("", description="按分类过滤, 模糊匹配"),
                        q: str = Query("", description="关键词, 搜名称/描述/机构/国别"),
                        limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    r = _r()
    items = r.list_sources(family=family, q=q)
    return {"meta": {"count": len(items), "family": family, "q": q,
                      "latency_ms": 0, "schema_version": "1.0"},
            "data": items[:limit],
            "fields": ["id", "name", "family", "org", "country", "summary",
                       "link_status", "scale", "endpoint_count"]}


@app.get("/v1/sources/{source_id}")
async def get_source(source_id: str) -> dict[str, Any]:
    r = _r()
    s = r.get_source(source_id)
    if not s:
        raise HTTPException(status_code=404, detail=f"未知源 {source_id}")
    return {"meta": {"source": source_id, "schema_version": "1.0"}, "data": s}


@app.get("/v1/sources/{source_id}/endpoints/{endpoint_id}")
async def describe_endpoint(source_id: str, endpoint_id: str) -> dict[str, Any]:
    r = _r()
    d = r.describe(source_id, endpoint_id)
    if not d:
        raise HTTPException(status_code=404, detail=f"未知端点 {source_id}/{endpoint_id}")
    return {"meta": {"source": source_id, "endpoint": endpoint_id, "schema_version": "1.0"},
            "data": d}


@app.post("/v1/query")
async def query(body: QueryBody) -> JSONResponse:
    r = _r()
    if r.resolve_source(body.source) is None:
        raise HTTPException(status_code=404, detail=f"未知数据源 {body.source}")
    t0 = time.perf_counter()
    env = await r.query(QueryRequest(**body.model_dump()))
    return _envelope_response(env, t0)


def _envelope_response(env, t0: float) -> JSONResponse:
    """把客户端可见总耗时写回 meta, 并在超预算时裁剪 data。

    resolver 内部的 latency_ms 不含响应序列化与框架开销, 客户端实际看到的
    要大一些; 这里用整段墙钟覆盖它, 保证上报值 = 客户端可见值。
    """
    total_ms = (time.perf_counter() - t0) * 1000
    env.meta.latency_ms = int(total_ms)
    if total_ms > env.meta.budget_ms:
        env.meta.errors.append({"type": "budget_exceeded",
                                "message": f"客户端可见耗时 {int(total_ms)}ms 超过预算 {env.meta.budget_ms}ms"})
        env.meta.degraded = True
        if isinstance(env.data, list) and len(env.data) > 5:
            env.data = env.data[:5]
            env.meta.truncated = True
    return JSONResponse(env.to_dict(), headers={"X-Gateway-Latency-Ms": str(int(total_ms))})


@app.get("/v1/query")
async def query_get(source: str = Query(...), endpoint: str = Query(...),
                    params: str = Query("{}", description="JSON 串"),
                    fields: str = Query("", description="逗号分隔"),
                    limit: int = Query(20, ge=1, le=500),
                    since: str | None = None,
                    timeout_ms: int = Query(DEFAULT_BUDGET_MS, ge=50, le=DEFAULT_BUDGET_MS)) -> JSONResponse:
    r = _r()
    try:
        p = json.loads(params) if params else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="params 必须是合法 JSON") from exc
    # params 必须是对象。数组/标量在 Go 网关侧返回 400, 这里保持一致 ——
    # 否则同一个请求在两个平面上给出不同的状态码。
    if not isinstance(p, dict):
        raise HTTPException(status_code=400, detail="params 必须是 JSON 对象")
    if r.resolve_source(source) is None:
        raise HTTPException(status_code=404, detail=f"未知数据源 {source}")
    req = QueryRequest(source=source, endpoint=endpoint, params=p,
                       fields=[f for f in fields.split(",") if f],
                       limit=limit, since=since, timeout_ms=timeout_ms)
    t0 = time.perf_counter()
    env = await r.query(req)
    return _envelope_response(env, t0)


SCHEMA_DOC: dict[str, Any] = {
    "schema_version": "1.0",
    "description": "A 类数据源网关 meta 规范。描述数据源、可调用端点, 以及一次请求的请求/响应元数据。",
    "source_spec": {
        "id": "字符串, 稳定标识",
        "name": "字符串, 源名称",
        "family": "字符串, 十大分类之一",
        "org": "字符串, 发布机构",
        "country": "字符串, 国别",
        "summary": "字符串, 数据内容摘要",
        "homepage": "字符串, 官方入口(已实测校验)",
        "link_status": "字符串, 最近一次链接校验状态码",
        "link_checked_at": "ISO 日期",
        "auth": {"type": "none|api_key|oauth2", "header": "认证头名", "env": "凭据环境变量", "note": "说明"},
        "limits": {"rate_per_sec": "每秒请求数", "burst": "突发额度",
                   "daily_credits": "每日额度", "policy_url": "政策链接", "note": "官方限速原文口径"},
        "scale": {"total": "总量数字", "unit": "单位", "measured_at": "实测日期", "basis": "计数口径"},
        "increment": {"frequency": "daily|weekly|monthly|quarterly|realtime", "daily": "日增量描述",
                      "window": "增量窗口", "strategy": "增量取法"},
        "crawl": {"mode": "bulk|incremental|query|file", "batch_files": ["批量文件"], "notes": "说明"},
        "proxy_policy": "never|fallback —— 是否允许走代理池兜底",
        "risk": "low|medium|high 合规风险",
        "tags": ["检索标签"],
        "endpoints": ["EndpointSpec 列表"],
    },
    "endpoint_spec": {
        "id": "字符串, 端点标识",
        "name": "字符串, 端点名",
        "method": "GET|POST",
        "url_template": "字符串, 带 {param} 占位的上游 URL",
        "params": [{"name": "参数名", "in": "path|query|header|body", "type": "类型",
                    "required": "是否必填", "desc": "说明", "pattern": "正则",
                    "default": "默认值", "example": "示例值"}],
        "response": {"media_type": "响应类型", "shape": "object|array|csv|xml|tsv|text",
                     "fields": [{"name": "字段名", "type": "类型", "desc": "说明", "path": "位置"}],
                     "item_path": "列表数据所在路径", "total_field": "总数所在路径", "notes": "说明"},
        "cache_ttl_s": "缓存秒数",
        "budget_ms": "该端点允许的上游超时预算(默认 450, 总预算 500)",
        "notes": "坑位说明",
    },
    "request": {
        "source": "源 id", "endpoint": "端点 id", "params": {"参数名": "值"},
        "fields": ["只返回这些字段"], "limit": "最多返回条数",
        "since": "增量窗口起点", "timeout_ms": "总预算, ≤500",
    },
    "response_envelope": {
        "meta": {
            "request_id": "本次请求 id",
            "schema_version": "信封版本",
            "source": "源 id", "endpoint": "端点 id",
            "upstream": {"url": "实际请求的上游 URL", "method": "方法", "status": "状态码",
                         "latency_ms": "上游耗时"},
            "latency_ms": "网关总耗时",
            "budget_ms": "本次预算(≤500)",
            "cached": "是否命中缓存",
            "cache_age_s": "缓存年龄",
            "degraded": "是否降级返回",
            "offline": "是否离线模式",
            "count": "本次返回条数",
            "total": "上游记录总数",
            "truncated": "是否被 limit 截断",
            "next_cursor": "下一页游标(有则给)",
            "rate_limit": {"policy": "限速口径", "remaining": "剩余额度"},
            "fallback": {"upstream_url": "降级时可直接取的上游 URL", "method": "HTTP 方法",
                         "hint": "处理建议"},
            "errors": [{"type": "错误类型", "message": "说明"}],
        },
        "data": "实际数据(列表/对象)",
        "fields": ["本次返回的字段名"],
    },
}


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level="warning")


if __name__ == "__main__":
    main()
