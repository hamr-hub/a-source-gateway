"""
resolver.py - 端点渲染 + 硬预算执行 + 降级

核心约束: **整个请求必须在 500ms 内返回**。
  - 元数据查询: 纯内存, < 5ms
  - 数据查询:   给上游的硬超时 = min(端点预算, 剩余预算 - 50ms 余量)
  - 超时/连不上: 不抛错, 降级返回(过期缓存优先), meta.degraded=true + errors[]
  - 绝不因为上游慢而让调用方等超过 500ms
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import time
import urllib.parse
from typing import Any

import httpx

from .cache import BoundedCache
from .compat import timeout_ctx
from .config import Config, load_config
from .registry import build_search_index, normalize_key
from .schema import (
    DEFAULT_BUDGET_MS,
    QueryRequest,
    ResponseEnvelope,
    ResponseMeta,
    UpstreamMeta,
    now_ms,
)

# 默认参考值; 运行期以 Config 为准
UA = "a-source-gateway/1.0 (+https://github.com/a-source-gateway)"
JSON_LIMIT_BYTES = 4 * 1024 * 1024     # 超过就不整体透传, 走摘要
MAX_BODY_BYTES = 2 * 1024 * 1024       # 正文上限(运行期由 Config 覆盖)
MAX_PARSE_ROWS = 15000                  # CSV/TSV 最多解析这么多行
DEFAULT_PAGE_SIZE = 25                  # 上游页大小默认值
PREWARM_CONCURRENCY = 6                 # 预热并发(串行要十几分钟, 冷启动期间取不到数)

# 端点专有请求头里的 ${VAR} 引用(凭据从环境变量读, 不进数据文件)
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 上游正文不是 JSON 时, 在错误里回带的正文前若干字符(去换行、限长)
# 与 Go 侧 parser.NotJSONSnippetRunes 一致, 两个平面回出的片段长度相同。
NOT_JSON_PREVIEW_CHARS = 240


def body_preview(body: str, limit: int = NOT_JSON_PREVIEW_CHARS) -> str:
    """正文前若干字符: 换行/制表符压成空格、限长。

    用途是让调用方一眼看出"上游给的是 HTML 错误页而不是数据" ——
    换行必须去掉, 否则错误消息会被撑成多行, 日志和信封都不好读。
    与 Go 侧 parser.notJSONSnippet 的处理一致。
    """
    flat = body[:limit * 4].replace("\n", " ").replace("\r", " ").replace("\t", " ").strip()
    if len(flat) > limit:
        return flat[:limit] + "…"
    return flat


class UpstreamNotJSONError(Exception):
    """上游正文不是 JSON(常见于 WAF/反爬返回的 HTML 错误页)。

    以前这里静默兜底成 {"_text": 原文}, 于是投影得到空列表, 而 meta.degraded
    仍是 false —— 调用方分不清"确实没有数据"和"上游给了错误页"。现在它必须
    让调用方看见降级。
    """

    def __init__(self, preview: str):
        super().__init__(f"上游返回的不是 JSON(疑似 HTML 错误页); 正文片段: {preview}")
        self.preview = preview


def _dig_path(node: Any, path: str) -> Any:
    """点号路径取值; 列表只支持数字下标。

    点号切分取不到时, 再按整串字面键试一次 —— 上游 OData 的 "@odata.count"
    这类键名本身带点, 按 "." 切永远取不到它(与 parser worker / Go parser 同语义)。
    """
    root = node
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                break
            node = node[part]
        elif isinstance(node, list) and part.isdigit():
            idx = int(part)
            if idx >= len(node):
                break
            node = node[idx]
        else:
            break
    else:
        return node
    if isinstance(root, dict) and path in root:
        return root[path]
    return None

# 延迟直方图桶边界(ms), 与 gateway/internal/metrics 的 latencyBuckets 严格一致。
# 500ms 是项目硬契约, 桶在该刻度两侧更密; Prometheus 的 +Inf 桶由输出端补。
LATENCY_BUCKETS_MS = (10, 25, 50, 100, 200, 300, 400, 500, 750, 1000, 2000, 5000)

# 上游页大小统一值: 这样"调用方要 3 条"与"要 50 条"命中同一条缓存。
# 按端点可覆盖: 字段特别肥的源(如 OpenAlex 单条约 25KB)要调小, 否则正文会超上限被截断。
PAGE_SIZE_PARAMS = {"size", "rows", "per_page", "per-page", "page_size", "pageSize"}


# 空结果只短暂缓存: 上游在高并发下偶尔返回 200 + 空载荷,
# 缓存下来会让调用方在 TTL 内一直拿到空壳。30 秒后自愈。
EMPTY_PAYLOAD_TTL_SECONDS = 30


def _is_empty_payload(payload: Any) -> bool:
    """载荷里没有可用记录(空列表/空对象/None)。"""
    if payload is None:
        return True
    if isinstance(payload, list):
        return len(payload) == 0
    if isinstance(payload, dict):
        return len(payload) == 0
    return False


class Resolver:
    # 后台补数用的长预算(不影响用户请求的 500ms 承诺)
    WARM_BUDGET_MS = 15000   # 默认值, 运行期以 Config.warm_budget_ms 为准

    def __init__(self, registry: dict[str, Any], offline: bool = False,
                 cache: BoundedCache | None = None, pool_size: int = 32,
                 cfg: Config | None = None, transport=None):
        self.cfg = cfg or load_config()
        self.registry = registry
        self.sources: dict[str, dict[str, Any]] = {s["id"]: s for s in registry["sources"]}
        # 搜索索引在注册表构建期一次算好: 请求期不再逐源跑正则(见 registry.build_search_index)
        self.search = build_search_index(registry["sources"])
        self.offline = offline
        self.cache = cache or BoundedCache()
        self._client: httpx.AsyncClient | None = None
        self._pool_size = pool_size
        self._transport = transport      # 注入 httpx.MockTransport 可离线测试
        self.prewarmed: dict[str, str] = {}      # source_id -> 预热状态
        self._prewarm_done = False
        self._warming: dict[str, bool] = {}      # 正在后台补数的缓存键
        self.warm_fills = 0                     # 后台补数成功次数
        self._page_override: dict[str, int] = {}   # 端点 -> 更小的页大小(正文截断时自适应)
        self._too_large: set = set()              # 已知是"文件型大响应"的端点
        # 观测指标(供 /metrics 暴露)
        # latency_buckets_ms: 延迟直方图(累计桶), 桶边界与 Go 网关严格一致。
        # 500ms 是项目硬契约, 所以桶在 500 附近更密; +Inf 隐含。
        self.metrics: dict[str, Any] = {
            "queries_total": 0, "cache_hits_total": 0, "degraded_total": 0,
            "timeouts_total": 0, "errors_total": 0, "warm_fills_total": 0,
            "warm_errors_total": 0, "warm_last_error": "",
            "latency_sum_ms": 0.0, "latency_max_ms": 0.0,
            "latency_buckets_ms": dict.fromkeys(LATENCY_BUCKETS_MS, 0),
            "by_source": {},
        }

    def _ssl_context(self):
        """系统信任库优先(部分官方站的证书链只在系统库里有效)。

        A_GATEWAY_VERIFY=none 可关校验, 仅限自有可信网络, 生产环境不要开。
        """
        import ssl
        if not self.cfg.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return ssl.create_default_context()

    # ── 生命周期 ──────────────────────────────────────────────────────
    async def startup(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                # 不设客户端级 timeout: 预算完全由每次请求的 asyncio.timeout 控制。
                # 客户端级 read timeout 会独立于预算把慢响应打断(例如 29MB 的 OFAC XML),
                # 导致后台补数静默失败。
                timeout=httpx.Timeout(None),
                # 用系统 CA 信任库而不是 httpx 默认的 certifi:
                # 部分官方站(如 treasury.gov)的证书链在系统库里才有效,
                # 用 certifi 会报 CERTIFICATE_VERIFY_FAILED。
                verify=self._ssl_context(),
                limits=httpx.Limits(max_connections=self._pool_size,
                                    max_keepalive_connections=self._pool_size),
                headers=self.cfg.headers(),
                follow_redirects=True,
                transport=self._transport,
            )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── 元数据 ────────────────────────────────────────────────────────
    def resolve_source(self, key: str) -> str | None:
        """把用户给的标识解析成源 id。

        顺序: 精确 id -> 别名 -> id 归一 -> name 归一 -> 名称/id 包含。
        故意宽松: MCP/LLM 调用方常常用简称("ctgov"、"sec"、"ofac")。

        全部走注册表期算好的索引(registry.SearchIndex), 请求期零正则分配;
        语义与改造前逐条对齐(见 registry.build_search_index 的优先级说明)。
        """
        if not key:
            return None
        if key in self.sources:
            return key
        norm = normalize_key(key)
        if not norm:                   # 纯中文等无 ASCII 字符的键, 不做模糊匹配
            return None
        sid = self.search.alias_index.get(norm)
        if sid:
            return sid
        hits = [sid for sid, n_name, n_id in self.search.norm_keys
                if norm in n_name or norm in n_id]
        if len(hits) == 1:
            return hits[0]
        if hits:                       # 多个命中: 取名字最短的(通常是主源)
            return min(hits, key=lambda h: len(self.sources[h]["name"]))
        return None

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        rid = self.resolve_source(source_id)
        return self.sources.get(rid) if rid else None

    def list_sources(self, family: str = "", q: str = "") -> list[dict[str, Any]]:
        """轻量投影, 不返回 endpoints, 保持响应小。

        `q` 对每个源的**可搜索文本**做子串匹配(大小写不敏感): canonical id、
        全部别名、name、org、country、family、summary、该源全部端点 id。
        以前只匹配 name/summary/org/country/family, 于是"能按别名调用、却搜不到"。
        """
        out = []
        ql = q.lower()
        for s in self.sources.values():
            if family and family not in s["family"]:
                continue
            if ql and ql not in self.search.haystack.get(s["id"], ""):
                continue
            out.append({
                "id": s["id"], "name": s["name"],
                # 别名: 调用方搜到源之后要知道该用哪个简称去调用。
                # 没有别名时给空数组(键始终在场, 调用方不用 .get 兜底)。
                "aliases": [str(a) for a in (s.get("aliases") or [])],
                "family": s["family"],
                "org": s["org"], "country": s["country"],
                "summary": s["summary"][:160],
                "link_status": s.get("link_status", ""),
                "scale": s.get("scale", {}).get("total"),
                "endpoint_count": len(s.get("endpoints", [])),
            })
        return out

    def describe(self, source_id: str, endpoint_id: str = "") -> dict[str, Any] | None:
        rid = self.resolve_source(source_id)
        s = self.sources.get(rid) if rid else None
        if not s:
            return None
        if not endpoint_id:
            return s
        for e in s.get("endpoints", []):
            if e["id"] == endpoint_id:
                return {"source": {k: s[k] for k in
                                   ("id", "name", "family", "org", "country", "summary",
                                    "homepage", "link_status", "auth", "limits", "scale",
                                    "increment", "crawl", "proxy_policy", "risk")},
                        "endpoint": e}
        return None

    # ── 查询 ──────────────────────────────────────────────────────────
    async def query(self, req: QueryRequest, budget_ms: int | None = None) -> ResponseEnvelope:
        t0 = now_ms()
        # 用户请求一律 ≤500ms; 后台预热器可以显式放宽
        strict = budget_ms is None
        budget = budget_ms or min(req.timeout_ms or DEFAULT_BUDGET_MS, DEFAULT_BUDGET_MS)
        meta = ResponseMeta(source=req.source, endpoint=req.endpoint, budget_ms=budget)

        rid = self.resolve_source(req.source)
        s = self.sources.get(rid) if rid else None
        if not s:
            meta.errors.append({"type": "not_found", "message": f"未知数据源 {req.source}"})
            meta.latency_ms = int(now_ms() - t0)
            return ResponseEnvelope(meta=meta, data=None)
        req.source = rid or req.source
        meta.source = rid or req.source

        ep = next((e for e in s.get("endpoints", []) if e["id"] == req.endpoint), None)
        if ep is None:
            meta.errors.append({"type": "endpoint_not_found",
                                "message": f"{req.source} 没有端点 {req.endpoint}"})
            meta.latency_ms = int(now_ms() - t0)
            return ResponseEnvelope(meta=meta, data=None)

        # 该源要求 UA 带联系邮箱, 但没配 -> 直接给可执行提示, 不去打上游吃 403
        if s.get("ua_requires_contact") and not self.cfg.contact:
            meta.degraded = True
            meta.errors.append({"type": "contact_required",
                                "message": f"{s['name']} 要求 User-Agent 里带联系邮箱, "
                                           f"当前未配置(A_GATEWAY_CONTACT)"})
            meta.fallback = {"upstream_url": s.get("homepage", ""), "method": "GET",
                             "hint": "设置环境变量 A_GATEWAY_CONTACT=you@example.com 后重试"}
            meta.latency_ms = int(now_ms() - t0)
            return ResponseEnvelope(meta=meta, data=[], fields=req.fields)

        # 渲染 URL
        ep_key = f"{req.source}|{req.endpoint}"
        if ep_key in self._too_large:
            meta.degraded = True
            meta.errors.append({"type": "response_too_large",
                                "message": "该端点是文件型大响应, 网关只做摘要; 请直连上游"})
            meta.fallback = {"upstream_url": ep.get("url_template", ""), "method": ep["method"],
                             "hint": "文件型大响应建议直连上游或走批量下载, 网关不做整体透传"}
            meta.latency_ms = int(now_ms() - t0)
            return ResponseEnvelope(meta=meta, data=[], fields=req.fields)
        eff_page = self._page_override.get(ep_key, int(ep.get("page_size") or DEFAULT_PAGE_SIZE))
        try:
            url = self._render(ep, req, eff_page)
        except KeyError as e:
            meta.errors.append({"type": "missing_param", "message": f"缺少必填参数 {e}"})
            meta.latency_ms = int(now_ms() - t0)
            return ResponseEnvelope(meta=meta, data=None)
        except Exception as e:
            meta.errors.append({"type": "bad_template", "message": str(e)})
            meta.latency_ms = int(now_ms() - t0)
            return ResponseEnvelope(meta=meta, data=None)

        meta.upstream = UpstreamMeta(url=url, method=ep["method"])
        cache_key = self._cache_key(req, url, eff_page)
        # 后台补数要能按更小的页大小重取, 这里把上下文带过去
        ep_ctx = {**ep, "_page_key": ep_key, "_src": req.source,
                  "_epid": req.endpoint, "_params": dict(req.params or {})}

        # 缓存命中
        val, fresh, age = self.cache.get(cache_key)
        if fresh:
            meta.cached = True
            meta.cache_age_s = age
            meta.upstream.status = (val.get("_status") if isinstance(val, dict) else None)
            data, total, truncated = self._project(val.get("_data") if isinstance(val, dict) else val,
                                                    ep, req)
            meta.count = len(data) if isinstance(data, list) else (1 if data is not None else 0)
            meta.total = total
            meta.truncated = truncated
            meta.latency_ms = int(now_ms() - t0)
            self._observe(meta, cached=True)
            return ResponseEnvelope(meta=meta, data=data, fields=req.fields)

        if val is not None and not fresh:
            # stale-while-revalidate: 过期条目立刻返回陈旧值, 后台按 TTL 刷新。
            # 调用方永远等不到上游的 400ms。
            meta.cached = True
            meta.cache_age_s = age
            meta.degraded = True
            meta.errors.append({"type": "stale",
                                "message": f"缓存已过期 {age}s, 返回陈旧值并在后台刷新"})
            data, total, truncated = self._project(val.get("_data") if isinstance(val, dict) else val,
                                                    ep, req)
            meta.count = len(data) if isinstance(data, list) else (1 if data is not None else 0)
            meta.total = total
            meta.truncated = truncated
            meta.latency_ms = int(now_ms() - t0)
            self._schedule_warm(ep_ctx, url, cache_key, force=True)
            self._observe(meta, cached=True)
            return ResponseEnvelope(meta=meta, data=data, fields=req.fields)

        if self.offline:
            meta.offline = True
            meta.degraded = True
            meta.errors.append({"type": "offline", "message": "网关处于离线模式, 仅提供元数据与缓存"})
            if val is not None:
                data, total, truncated = self._project(
                    val.get("_data") if isinstance(val, dict) else val, ep, req)
                meta.cached = True
                meta.cache_age_s = age
                meta.degraded = True
                meta.count = len(data) if isinstance(data, list) else 0
                meta.total = total
                meta.latency_ms = int(now_ms() - t0)
                return ResponseEnvelope(meta=meta, data=data, fields=req.fields)
            meta.latency_ms = int(now_ms() - t0)
            return ResponseEnvelope(meta=meta, data=[], fields=req.fields)

        # 实时取数 —— 硬预算(总墙钟, 覆盖连接/首包/正文下载全过程)
        remaining = budget - (now_ms() - t0) - self.cfg.reserve_ms   # 预留解析/序列化/框架开销
        # 端点自带的上限只约束用户请求; 预热器按自己的长预算走
        cap = (ep.get("budget_ms") or self.cfg.upstream_budget_ms) if strict else remaining
        timeout_s = max(0.05, min(cap, remaining) / 1000.0)
        t_up = time.perf_counter()
        try:
            status, body, truncated = await self._fetch(ep, url, timeout_s)
            meta.upstream.latency_ms = int((time.perf_counter() - t_up) * 1000)
            meta.upstream.status = status
            if truncated:
                # 正文超上限被截断 -> JSON 多半解析不了, 明确降级而不是静默返回空
                # (截断判定必须排在解析之前: 否则"太大"会被误报成"不是 JSON")
                meta.degraded = True
                meta.errors.append({"type": "response_too_large",
                                    "message": f"响应超过 {self.cfg.max_body_bytes // 1024 // 1024}MB 上限被截断;"
                                               f"请减小 limit 或直连上游"})
                meta.fallback = {"upstream_url": url, "method": ep["method"],
                                 "hint": "减小 limit 后重试, 或直连上游"}
                self._schedule_warm(ep_ctx, url, cache_key)
                data, total, truncated_out = self._fallback(val, ep, req)
                meta.cached = val is not None
                meta.cache_age_s = age
                meta.count = len(data) if isinstance(data, list) else 0
                meta.latency_ms = int(now_ms() - t0)
                return ResponseEnvelope(meta=meta, data=data, fields=req.fields)
            # 正文必须是 JSON —— 否则多半是 WAF/反爬的 HTML 错误页,
            # 静默兜底成 {"_text": ...} 会让调用方把"上游报错"误读成"没有数据"。
            # (状态码 >= 400 的正文同样要判: 状态码与"不是 JSON"两条原因都报)
            payload = self._parse(body, ep["response"].get("shape", "object"), strict=True)
            if status < 400:          # 只缓存成功响应
                ttl = self._adaptive_ttl(ep, cache_key)
                if _is_empty_payload(payload) and ttl > EMPTY_PAYLOAD_TTL_SECONDS:
                    ttl = EMPTY_PAYLOAD_TTL_SECONDS
                self.cache.put(cache_key, {"_data": payload, "_status": status}, ttl)
            data, total, truncated = self._project(payload, ep, req)
            meta.count = len(data) if isinstance(data, list) else (1 if data is not None else 0)
            meta.total = total
            meta.truncated = truncated
            if status >= 400:
                meta.degraded = True
                meta.errors.append({"type": "upstream_error", "status": status,
                                    "message": body[:300]})
        except UpstreamNotJSONError as e:
            # 上游给了非 JSON 正文(多半是 HTML 错误页): 如实标降级, 别装成"成功 + 0 条"
            meta.degraded = True
            status = meta.upstream.status or 0
            if status >= 400:
                # 状态码 >= 400: 状态码才是根因, 先报它(与 Go 侧顺序一致)
                meta.errors.append({"type": "upstream_error", "status": status,
                                    "message": body[:300]})
            meta.errors.append({"type": "upstream_not_json",
                                "message": str(e)[:300]})
            meta.fallback = {"upstream_url": url, "method": ep["method"],
                             "hint": "上游返回的不是 JSON(疑似 HTML 错误页); "
                                     "请直连上游确认, 或稍后重试"}
            self._schedule_warm(ep_ctx, url, cache_key)
            data, total, truncated = self._fallback(val, ep, req)
            meta.cached = val is not None
            meta.cache_age_s = age
            meta.count = len(data) if isinstance(data, list) else 0
            meta.total = total
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
            # 注: 3.11 起 asyncio.TimeoutError 就是内建 TimeoutError 的别名;
            # 3.10 上它们是两个类, 所以两个都捕获 —— 否则 3.10 的超时会漏到通用分支,
            # 信封里的 errors[].type 会变成 "TimeoutError" 而不是 "timeout"。
            meta.degraded = True
            meta.errors.append({"type": "timeout",
                                "message": f"上游超过 {int(timeout_s * 1000)}ms 预算, 已降级"})
            meta.fallback = {"upstream_url": url, "method": ep["method"],
                             "hint": "网关预算内取不到;可直连上游, 或稍后重试(后台正在补数)"}
            self._schedule_warm(ep_ctx, url, cache_key)
            data, total, truncated = self._fallback(val, ep, req)
            meta.cached = val is not None
            meta.cache_age_s = age
            meta.count = len(data) if isinstance(data, list) else 0
            meta.total = total
        except Exception as e:
            meta.degraded = True
            meta.errors.append({"type": type(e).__name__, "message": str(e)[:300]})
            meta.fallback = {"upstream_url": url, "method": ep["method"],
                             "hint": "网关取数失败;可直连上游"}
            self._schedule_warm(ep_ctx, url, cache_key)
            data, total, truncated = self._fallback(val, ep, req)
            meta.cached = val is not None
            meta.cache_age_s = age
            meta.count = len(data) if isinstance(data, list) else 0
            meta.total = total

        meta.latency_ms = int(now_ms() - t0)
        if meta.latency_ms > budget:                      # 兜底: 绝不超标上报
            meta.errors.append({"type": "budget_exceeded",
                                "message": f"实际 {meta.latency_ms}ms 超过预算 {budget}ms"})
        self._observe(meta)
        return ResponseEnvelope(meta=meta, data=data, fields=req.fields)

    # ── 内部工具 ──────────────────────────────────────────────────────
    async def _fetch(self, ep: dict[str, Any], url: str, timeout_s: float
                     ) -> tuple[int, str, bool]:
        """一次上游取数, 受总墙钟预算约束, 正文有上限。返回 (status, body, truncated)。"""
        assert self._client is not None, "client 未初始化"
        async with timeout_ctx(timeout_s), self._client.stream(
                ep["method"], url, headers=self._endpoint_headers(ep) or None) as r:
            body, truncated = await self._read_capped(r, self.cfg.max_body_bytes)
            return r.status_code, body, truncated

    def _endpoint_headers(self, ep: dict[str, Any]) -> dict[str, str]:
        """端点专有请求头; 值里的 ${VAR} 用环境变量替换。

        凭据不进数据文件: 数据里只写变量名, 真正的值由部署时的环境变量提供。
        引用了未设置的变量时, 整个头都不发 —— 发一个残缺的凭据
        (比如只剩 "Bearer ")比不发更糟。
        """
        out: dict[str, str] = {}
        for k, v in (ep.get("headers") or {}).items():
            if not isinstance(v, str):
                continue
            names = _ENV_REF_RE.findall(v)
            if not names:
                out[k] = v
                continue
            values: list[str] = []
            for name in names:
                env_value = os.environ.get(name, "")
                if not env_value:
                    values = []
                    break
                values.append(env_value)
            if not values:
                continue
            out[k] = _ENV_REF_RE.sub(
                lambda mm: os.environ.get(mm.group(1), ""), v)
        return out

    def _adaptive_ttl(self, ep: dict[str, Any], cache_key: str) -> int:
        """热条目(被反复访问)用更短 TTL, 让数据更新更及时。"""
        base = int(ep.get("cache_ttl_s") or self.cfg.cache_ttl_s)
        try:
            entry = self.cache._items.get(cache_key)      # noqa: SLF001
        except Exception:
            entry = None
        if entry is not None and entry.hits >= 3:
            return max(30, base // 4)
        return base

    def _observe(self, meta: ResponseMeta, cached: bool = False) -> None:
        """累计观测指标(供 /metrics 暴露)。"""
        m = self.metrics
        m["queries_total"] += 1
        if cached:
            m["cache_hits_total"] += 1
        if meta.degraded:
            m["degraded_total"] += 1
        for e in meta.errors:
            if e.get("type") == "timeout":
                m["timeouts_total"] += 1
            if e.get("type") not in ("stale",):
                m["errors_total"] += 1
        m["latency_sum_ms"] += meta.latency_ms
        m["latency_max_ms"] = max(m["latency_max_ms"], meta.latency_ms)
        for bound in LATENCY_BUCKETS_MS:
            if meta.latency_ms <= bound:
                m["latency_buckets_ms"][bound] += 1
        src = m["by_source"].setdefault(meta.source, {"queries": 0, "degraded": 0, "latency_sum": 0})
        src["queries"] += 1
        src["latency_sum"] += meta.latency_ms
        if meta.degraded:
            src["degraded"] += 1

    def _schedule_warm(self, ep: dict[str, Any], url: str, cache_key: str,
                       force: bool = False) -> None:
        """用户请求超时后(或缓存过期), 后台用长预算补一次数 —— 下次请求就能秒回。"""
        if not force and cache_key in self._warming:
            return
        if force and cache_key in self._warming:
            return
        self._warming[cache_key] = True

        async def _run() -> None:
            try:
                status, body, truncated = await self._fetch(ep, url, self.cfg.warm_budget_ms / 1000.0)
                if status >= 500:                      # 上游偶发 5xx -> 重试一次
                    await asyncio.sleep(1.0)
                    status, body, truncated = await self._fetch(
                        ep, url, self.cfg.warm_budget_ms / 1000.0)
                if truncated:
                    # 文件型大响应(OFAC 29MB XML / HMLR CSV 之类): 截断的残片没有缓存价值,
                    # 也不该占缓存。改为记录状态, 让查询侧直接给"请直连上游"的指引。
                    # (截断判定排在解析之前: 残片本来就解析不出 JSON)
                    self._too_large.add(ep.get("_page_key", ""))
                    self.warm_fills += 1
                    return
                if status < 400:
                    # 严格解析: HTML 错误页绝不能进缓存(否则 TTL 内一直拿到假空壳)。
                    # 与 Go 侧一致 —— 失败就当预热失败, 不写缓存。
                    payload = self._parse(body, ep["response"].get("shape", "object"),
                                          strict=True)
                    ttl = self._adaptive_ttl(ep, cache_key)
                    # 后台补数与用户请求用同一把尺子: 空结果不钉死整个 TTL
                    if _is_empty_payload(payload) and ttl > EMPTY_PAYLOAD_TTL_SECONDS:
                        ttl = EMPTY_PAYLOAD_TTL_SECONDS
                    self.cache.put(cache_key, {"_data": payload, "_status": status}, ttl)
                self.warm_fills += 1
                self.metrics["warm_fills_total"] += 1
            except Exception as e:
                # 后台补数失败不能静默: 否则"缓存永远填不上"这类问题无从排查
                self.metrics["warm_errors_total"] += 1
                self.metrics["warm_last_error"] = f"{type(e).__name__}: {e}"[:200]
            finally:
                self._warming.pop(cache_key, None)

        try:
            asyncio.create_task(_run())
        except RuntimeError:
            self._warming.pop(cache_key, None)

    @staticmethod
    async def _read_capped(r: httpx.Response, cap: int) -> tuple[str, bool]:
        """流式读正文, 到 cap 就停 —— 防止大文件把时间预算吃光。返回 (text, truncated)。"""
        chunks: list[bytes] = []
        size = 0
        truncated = False
        async for chunk in r.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= cap:
                truncated = True
                break
        return b"".join(chunks).decode("utf-8", "replace"), truncated

    @property
    def prewarm_done(self) -> bool:
        return self._prewarm_done

    async def prewarm(self, source_ids: list[str] | None = None) -> None:
        """后台预热: 建立 TCP/TLS 连接并取一次, 让冷路径也快。

        只取小样本(limit=1), 不落业务数据, 只让缓存与连接池热起来。

        预热用的参数必须和真实查询渲染出来的 URL 一致 —— 否则缓存里躺着
        的是没人会请求的 URL, 源还是冷的, 每次调用照样降级。
        并发预热: 逐个串行要十几分钟, 冷启动期间大部分源都取不到数。
        """
        if self._client is None or self.offline:
            return
        targets: list[tuple[str, dict[str, Any]]] = []
        for sid, s in self.sources.items():
            if source_ids and sid not in source_ids:
                continue
            ep = next((e for e in s.get("endpoints", []) if e["id"] != "entry"), None)
            if not ep:
                continue
            targets.append((sid, {
                "source": sid, "endpoint": ep["id"],
                "params": self._default_params(ep),
                "limit": 1, "timeout_ms": 450,
            }))

        sem = asyncio.Semaphore(PREWARM_CONCURRENCY)

        async def run(sid: str, body: dict[str, Any]) -> None:
            async with sem:
                try:
                    env = await self.query(QueryRequest(**body), budget_ms=self.cfg.warm_budget_ms)
                    self.prewarmed[sid] = "degraded" if env.meta.degraded else "ok"
                except Exception as e:
                    self.prewarmed[sid] = f"err:{type(e).__name__}"

        await asyncio.gather(*(run(sid, body) for sid, body in targets))
        self._prewarm_done = True

    def _default_params(self, ep: dict[str, Any]) -> dict[str, Any]:
        """调用方不传参时, 查询路径会渲染出来的参数集。

        规则与 _render 一致: 环境变量 > 有 default 的取 default > 必填且只有
        example 的取 example; email/mailto 端点在值为空时回落到已配置的联系邮箱。
        """
        params: dict[str, Any] = {}
        for p in ep.get("params", []):
            name = p["name"]
            if p.get("env"):
                env_value = os.environ.get(p["env"], "")
                if env_value:
                    params[name] = env_value
                    continue
                # 凭据缺失: 不回落到 example(理由见 _render 里的同一条规则)。
                if p.get("required"):
                    continue
            if p.get("default") is not None:
                params[name] = p["default"]
            elif (p.get("required") and p.get("example") is not None
                  and not p.get("env")):
                params[name] = p["example"]
            if name in ("email", "mailto") and not params.get(name) and self.cfg.contact:
                params[name] = self.cfg.contact
        return params

    def _render(self, ep: dict[str, Any], req: QueryRequest,
                page_size: int | None = None) -> str:
        params = dict(req.params or {})
        # 参数默认值
        for p in ep.get("params", []):
            # 凭据注入: 数据文件里只写环境变量名, 真正的值由部署时提供。
            # 优先级: 调用方显式传的值 > 环境变量 > 端点声明的默认值。
            if p["name"] not in params and p.get("env"):
                env_value = os.environ.get(p["env"], "")
                if env_value:
                    params[p["name"]] = env_value
            if p["name"] not in params and p.get("default") is not None:
                params[p["name"]] = p["default"]
            # 必填参数的兜底: 端点声明的 example 就是"这个端点怎么调"的答案。
            # 没有它, 调用方按文档那样 params:{} 调不动大多数端点。
            #
            # 凭据参数(p["env"])不参与这个兜底: 环境变量没配时回落 example 会把
            # "YOUR_CENSUS_API_KEY" 这样的占位符真的发给上游, 上游回一个
            # 200 + "Invalid Key" 的 HTML 页 —— 状态码是 200, 于是调用方看到
            # 一次"成功的空结果", 而不是"缺凭据"。宁可报缺参数。
            if (p["name"] not in params and p.get("required")
                    and p.get("example") is not None and not p.get("env")):
                params[p["name"]] = p["example"]
            # 端点要邮箱参数(如 OpenAlex 的 mailto)且用户已配置 -> 自动填
            if p["name"] in ("email", "mailto") and not params.get(p["name"]) \
                    and getattr(self, "cfg", None) and self.cfg.contact:
                params[p["name"]] = self.cfg.contact
        missing = [p["name"] for p in ep.get("params", [])
                   if p.get("required") and p["name"] not in params]
        if missing:
            raise KeyError(", ".join(missing))
        if "url" not in params:
            params["url"] = ""
        params.pop(self.CACHE_BUSTER, None)      # 不发给上游
        page_size = int(page_size if page_size is not None
                         else (ep.get("page_size") or DEFAULT_PAGE_SIZE))
        for k in list(params):
            if k in PAGE_SIZE_PARAMS and params[k] not in (None, ""):
                params[k] = page_size

        raw = ep["url_template"]
        # 先做 {{}} 风格的占位替换需要 url 编码; 这里按 path/query 分别处理
        # 路径参数里的 "/" 不能被编码(否则 EIA 的 v2 路由会被打成 %2F 而 404)
        path_params = {p["name"] for p in ep.get("params", []) if p.get("in") == "path"}

        def sub(m):
            key = m.group(1)
            v = params.get(key, "")
            # 所有占位符一律百分号编码(只有路径参数保留 "/")。
            # {url} 也不例外: 原样替换会让调用方往查询串里注入额外参数
            # (如 url=http://x/&admin=true) 或用 "#" 截断查询串。
            # 合法的 url 参数值是完整 URL, 编码后 ":" "/" 变成 %3A %2F —— 上游解码后还原。
            if key in path_params:
                return urllib.parse.quote(str(v), safe="/")
            return urllib.parse.quote(str(v), safe="")

        url = re.sub(r"\{([a-zA-Z0-9_]+)\}", sub, raw)

        # query 参数: 没出现在模板里的, 追加到 query string
        used = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", raw))
        extra = {k: v for k, v in params.items()
                 if k not in used and k not in ("url",) and v is not None and v != ""}
        if extra:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(extra, doseq=True)
        # 清掉空值查询参数(如 mailto=); 裸键(没有 "=")保留 —— 与 Go 侧逐行对齐
        if "?" in url:
            base_q, _, q = url.partition("?")
            parts: list[str] = []
            for kv in q.split("&"):
                if not kv:
                    continue
                if kv.endswith("="):
                    continue                       # 空值(mailto=)
                if "=" not in kv:
                    parts.append(kv)                # 裸键(pretty): 保留
                    continue
                if kv.split("=", 1)[1] == "":
                    continue
                parts.append(kv)
            url = base_q + ("?" + "&".join(parts) if parts else "")
        return url

    CACHE_BUSTER = "_cb"      # 保留参数: 只进缓存键, 不发给上游

    @staticmethod
    def _cache_key(req: QueryRequest, url: str, page_size: int = DEFAULT_PAGE_SIZE) -> str:
        """只按上游请求内容做键: limit/fields 是网关侧的投影, 不影响上游响应。"""
        p = {k: v for k, v in (req.params or {}).items() if k not in ("email",)}
        for k in list(p):
            if k in PAGE_SIZE_PARAMS:
                p[k] = page_size
        return f"{req.source}|{req.endpoint}|{url}|{sorted(p.items())}"

    def _parse(self, body: str, shape: str, strict: bool = False) -> Any:
        """把上游正文解析成中间结构。

        `strict=True` 时, json 形状解析失败会抛 UpstreamNotJSONError(而不是
        静默兜底成 {"_text": 原文}) —— 查询路径用它把"上游给了 HTML 错误页"
        如实标成降级。默认 lenient, 供只需要拿到原始文本的调用方使用
        (scripts/ 侧的 verify/demo 就是这么用的)。
        """
        if shape in ("csv", "tsv"):
            delim = "\t" if shape == "tsv" else ","
            rows = []
            for i, row in enumerate(csv.reader(io.StringIO(body), delimiter=delim)):
                if i >= self.cfg.max_parse_rows:     # 解析护栏: 别让大表把预算吃光
                    break
                rows.append(row)
            return rows
        if shape == "jsonl":            # JSON Lines: 每行一个对象
            out = []
            for line in body.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(out) >= MAX_PARSE_ROWS:
                    break
            return out
        if shape == "xml":
            return {"_xml": body[:JSON_LIMIT_BYTES]}
        if shape == "text":
            return {"_text": body[:200_000]}
        try:
            return json.loads(body)
        except Exception:
            if strict:
                raise UpstreamNotJSONError(body_preview(body)) from None
            return {"_text": body[:200_000]}

    def _project(self, payload: Any, ep: dict[str, Any], req: QueryRequest
                 ) -> tuple[Any, int | None, bool]:
        """从上游响应里取出列表、总数,并按 fields/limit 裁剪。"""
        total = None
        resp = ep.get("response", {})
        total_field = resp.get("total_field")
        item_path = resp.get("item_path")

        if total_field:
            node = _dig_path(payload, total_field)
            if isinstance(node, int | float):
                total = int(node)

        data: Any = payload
        if resp.get("item_shape") == "map" and isinstance(payload, dict):
            data = list(payload.values())          # 形如 {"0": {...}, "1": {...}}
        elif item_path:
            # 顶层是数组时: 路径首段是数字才按下标定位(如 "0.data");
            # 否则保持整个数组(旧行为, 不能让"顶层数组"变成空列表)。
            head = item_path.split(".")[0]
            if isinstance(payload, dict) or head.isdigit():
                node = _dig_path(payload, item_path)
                data = node if node is not None else []
            else:
                data = payload
            if resp.get("item_flatten") and isinstance(data, list) and data \
                    and isinstance(data[0], list):
                # 列表套列表(如上交所公告 pageHelp.data): 展平一层
                flat: list[Any] = []
                for sub in data:
                    if isinstance(sub, list):
                        flat.extend(sub)
                    else:
                        flat.append(sub)
                data = flat

        truncated = False
        if isinstance(data, list):
            if total is None:
                total = len(data)
            if req.limit and len(data) > req.limit:
                data = data[: req.limit]
                truncated = True
            elif total is not None and total > len(data):
                truncated = True     # 上游页取满且还有更多, 调用方可按需翻页
            if req.fields and data and isinstance(data[0], dict):
                keep = set(req.fields)
                # 非字典行原样保留(与 parser worker / Go 引擎一致), 不抛错
                data = [{k: v for k, v in row.items() if k in keep}
                        if isinstance(row, dict) else row for row in data]
        elif isinstance(payload, dict) and "_xml" in payload:
            data = {"_xml_bytes": len(payload["_xml"].encode("utf-8")),
                    "_truncated": len(payload["_xml"]) >= JSON_LIMIT_BYTES}
            truncated = payload.get("_truncated", False)

        return data, total, truncated

    @staticmethod
    def _fallback(val: Any, ep: dict[str, Any], req: QueryRequest):
        if val is None:
            return [], None, False
        raw = val.get("_data") if isinstance(val, dict) else val
        return raw, None, False
