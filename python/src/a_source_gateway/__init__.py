"""
a-source-gateway - 海外 A 类(法规强制公开)数据源的通用网关

不存储业务数据: 只保留源元数据索引, 数据按需实时取, 单次调用硬预算 ≤500ms。

    from a_source_gateway import Resolver, QueryRequest

    r = Resolver(build_registry())
    await r.startup()
    env = await r.query(QueryRequest(source="crossref", endpoint="works",
                                      params={"filter": "from-index-date:2026-10-21"}))
    print(env.meta.total, env.meta.latency_ms)
"""
from .config import Config, load_config
from .registry import build_registry
from .resolver import Resolver
from .schema import (
    DEFAULT_BUDGET_MS,
    QueryRequest,
    ResponseEnvelope,
    ResponseMeta,
    SourceSpec,
)

__version__ = "1.4.0"
__all__ = [
    "Config", "load_config", "build_registry", "Resolver",
    "QueryRequest", "ResponseEnvelope", "ResponseMeta", "SourceSpec",
    "DEFAULT_BUDGET_MS", "__version__",
]
