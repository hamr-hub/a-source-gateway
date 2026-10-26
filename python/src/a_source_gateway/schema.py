"""
schema.py - A 类数据源网关的 meta 规范

三个层级的描述:
  1. SourceSpec     一个数据源(机构/内容/认证/限速/规模/增量/抓取方式/代理策略)
  2. EndpointSpec   一个可调用接口(请求参数 + 响应结构 + 缓存 + 预算)
  3. 信封            请求 QueryRequest / 响应 ResponseEnvelope
                    meta 段描述"这次请求打到哪、用了多久、有没有降级、命中多少",
                    data 段只放结果本身,两者严格分离,便于机器消费。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = "1.2"   # 1.2: 源检索覆盖 canonical id/别名/端点 id; 源投影新增 aliases; 非 JSON 正文如实标降级(upstream_not_json)
DEFAULT_BUDGET_MS = 500
UPSTREAM_BUDGET_MS = 400      # 上游取数的墙钟上限
RESERVE_MS = 100              # 留给解析/投影/序列化/框架开销的余量


# ── 参数 / 响应字段描述 ──────────────────────────────────────────────────
@dataclass
class ParamSpec:
    name: str
    in_: str = "query"                 # path | query | header | body  (对外 JSON key 为 "in")
    type: str = "string"               # string | int | float | bool | string[]
    required: bool = False
    desc: str = ""
    pattern: str | None = None
    default: Any = None
    example: Any = None
    env: str | None = None             # 凭据从哪个环境变量读(值不进数据文件)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if v is not None}
        d["in"] = d.pop("in_")
        return d


@dataclass
class FieldSpec:
    name: str
    type: str = "string"
    desc: str = ""
    path: str | None = None          # 在上游响应里的位置,如 "meta.pagination.total"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class AuthSpec:
    type: str = "none"      # none | api_key | oauth2 | token | email_param | paid | mixed
    header: str | None = None
    env: str | None = None           # 凭据从哪个环境变量读
    optional: bool = False              # 凭据是可选的(如"可选key")
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class LimitsSpec:
    rate_per_sec: float | None = None
    burst: int | None = None
    daily_credits: int | None = None
    policy_url: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ScaleSpec:
    total: int | None = None
    unit: str = ""
    measured_at: str = ""
    basis: str = ""                     # 计数口径(哪个端点的哪个字段)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class IncrementSpec:
    frequency: str = ""                 # daily | weekly | monthly | quarterly | realtime
    daily: str = ""
    window: str = ""
    strategy: str = ""                  # 增量怎么取(按哪个字段/哪个文件)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class CrawlSpec:
    mode: str = "query"                 # bulk | incremental | query | file
    batch_files: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], "")}


@dataclass
class ResponseSpec:
    media_type: str = "application/json"
    shape: str = "object"               # object | array | csv | tsv | xml | jsonl | text
    fields: list[FieldSpec] = field(default_factory=list)
    item_path: str | None = None     # 列表数据在响应里的路径
    item_shape: str = "list"             # list=数组 | map=字典值域(如 SEC company_tickers)
    item_flatten: bool = False      # 列表套列表时展平一层(如上交所公告 pageHelp.data)
    total_field: str | None = None   # 总数字段路径
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "media_type": self.media_type,
            "shape": self.shape,
            "fields": [f.to_dict() for f in self.fields],
        }
        if self.item_path:
            d["item_path"] = self.item_path
        if self.item_shape != "list":
            d["item_shape"] = self.item_shape
        if self.item_flatten:
            d["item_flatten"] = True
        if self.total_field:
            d["total_field"] = self.total_field
        if self.notes:
            d["notes"] = self.notes
        return d


@dataclass
class EndpointSpec:
    id: str
    name: str
    method: str = "GET"
    url_template: str = ""
    params: list[ParamSpec] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)   # 端点专有请求头(如 Accept)
    page_size: int | None = None                          # 上游页大小(字段肥的源要调小)
    response: ResponseSpec = field(default_factory=ResponseSpec)
    cache_ttl_s: int = 900
    budget_ms: int = UPSTREAM_BUDGET_MS
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "method": self.method,
            "url_template": self.url_template,
            "params": [p.to_dict() for p in self.params],
            "headers": self.headers,
            "page_size": self.page_size,
            "response": self.response.to_dict(),
            "cache_ttl_s": self.cache_ttl_s,
            "budget_ms": self.budget_ms,
            "notes": self.notes,
        }
        return d


@dataclass
class SourceSpec:
    id: str
    name: str
    family: str
    org: str = ""
    country: str = ""
    summary: str = ""
    homepage: str = ""
    link_status: str = ""
    link_checked_at: str = ""
    auth: AuthSpec = field(default_factory=AuthSpec)
    limits: LimitsSpec = field(default_factory=LimitsSpec)
    scale: ScaleSpec = field(default_factory=ScaleSpec)
    increment: IncrementSpec = field(default_factory=IncrementSpec)
    crawl: CrawlSpec = field(default_factory=CrawlSpec)
    proxy_policy: str = "fallback"      # never | fallback
    risk: str = "low"
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)   # 常见简称/别名, 便于模糊解析
    ua_requires_contact: bool = False   # 该源要求 UA 里带联系邮箱(否则上游直接 403)
    endpoints: list[EndpointSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "family": self.family,
            "org": self.org,
            "country": self.country,
            "summary": self.summary,
            "homepage": self.homepage,
            "link_status": self.link_status,
            "link_checked_at": self.link_checked_at,
            "auth": self.auth.to_dict(),
            "limits": self.limits.to_dict(),
            "scale": self.scale.to_dict(),
            "increment": self.increment.to_dict(),
            "crawl": self.crawl.to_dict(),
            "proxy_policy": self.proxy_policy,
            "risk": self.risk,
            "tags": self.tags,
            "aliases": self.aliases,
            "ua_requires_contact": self.ua_requires_contact,
            "endpoints": [e.to_dict() for e in self.endpoints],
        }

    def endpoint(self, endpoint_id: str) -> EndpointSpec | None:
        for e in self.endpoints:
            if e.id == endpoint_id:
                return e
        return None


# ── 请求 / 响应信封 ──────────────────────────────────────────────────────
@dataclass
class QueryRequest:
    source: str
    endpoint: str
    params: dict[str, Any] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)   # 只回这些字段
    limit: int = 20
    since: str | None = None
    timeout_ms: int = DEFAULT_BUDGET_MS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = SCHEMA_VERSION
        return d


@dataclass
class UpstreamMeta:
    url: str = ""
    method: str = "GET"
    status: int | None = None
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResponseMeta:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    schema_version: str = SCHEMA_VERSION
    source: str = ""
    endpoint: str = ""
    upstream: UpstreamMeta = field(default_factory=UpstreamMeta)
    latency_ms: int = 0
    budget_ms: int = DEFAULT_BUDGET_MS
    cached: bool = False
    cache_age_s: int = 0
    degraded: bool = False
    offline: bool = False
    count: int = 0
    total: int | None = None
    truncated: bool = False
    next_cursor: str | None = None
    rate_limit: dict[str, Any] = field(default_factory=dict)
    fallback: dict[str, Any] = field(default_factory=dict)   # 降级时的直连兜底信息
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "schema_version": self.schema_version,
            "source": self.source,
            "endpoint": self.endpoint,
            "upstream": self.upstream.to_dict(),
            "latency_ms": self.latency_ms,
            "budget_ms": self.budget_ms,
            "cached": self.cached,
            "cache_age_s": self.cache_age_s,
            "degraded": self.degraded,
            "offline": self.offline,
            "count": self.count,
            "total": self.total,
            "truncated": self.truncated,
            "next_cursor": self.next_cursor,
            "rate_limit": self.rate_limit,
            "fallback": self.fallback,
            "errors": self.errors,
        }


@dataclass
class ResponseEnvelope:
    meta: ResponseMeta
    data: Any = None
    fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"meta": self.meta.to_dict(), "data": self.data, "fields": self.fields}


def now_ms() -> float:
    return time.monotonic() * 1000.0
