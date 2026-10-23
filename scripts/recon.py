#!/usr/bin/env python3
"""
recon.py - 候选数据源侦察。

只做"看得见、测得到"的事: 每条结论都带证据, 测不出来的写 null + warning,
绝不把猜测当事实写进报告。

    python3 scripts/recon.py "https://api.crossref.org/works?rows=1" -o recon.json

产出: 一份 schema 稳定的侦察报告 JSON, 供 scaffold_source.py 消费。

只用标准库(urllib.request), 不装任何东西就能跑。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_UA = "a-source-gateway/1.0 (+https://github.com/a-source-gateway)"
BODY_SAMPLE_BYTES = 2048
MAX_BODY_BYTES = 2 * 1024 * 1024

# 约定俗成的文档/健康检查路径(探测时逐个试, 总请求数有硬上限)
PROBE_PATHS = [
    "/openapi.json", "/swagger.json", "/api-docs", "/.well-known/openapi.json",
    "/docs", "/api", "/v1", "/v2", "/health", "/status",
    "/swagger.yaml", "/openapi.yaml", "/api/docs", "/redoc",
]
MAX_TOTAL_REQUESTS = 20          # 含首次探测在内的总请求数上限
DOC_PATH_TOKENS = ("openapi", "swagger", "api-docs", "apidec", "docs", "redoc", "well-known")

# 数组字段名优先表(命中优先名才优先被选为 item_path)
PREFERRED_ITEM_KEYS = ("results", "data", "items", "records", "hits", "list", "rows",
                       "docs", "entries", "elements", "content")

# 总数字段名优先表(顺序即优先级)
TOTAL_KEY_PRIORITY = (
    "total", "totalcount", "total_count", "totalresults", "total-results", "total_results",
    "numfound", "num_found", "numtotal", "num_total", "hits.total", "count",
)
TOTAL_KEY_PATHS = ("hits.total", "meta.total", "meta.count", "pagination.total",
                   "page.total", "result.total")

# 只影响"取多少条"的参数: 带这些参数时, 上游报的 total 仍然是全量口径
PAGING_ONLY_PARAMS = {
    "rows", "row", "per-page", "per_page", "perpage", "page", "limit", "size",
    "offset", "start", "start-index", "startindex", "format", "mailto", "email",
    "select", "fields", "include", "format_version",
}


# ── HTTP ─────────────────────────────────────────────────────────────────
def build_user_agent(user_agent: str = "", contact: str = "") -> str:
    ua = (user_agent or "").strip() or DEFAULT_UA
    contact = (contact or "").strip()
    if contact and contact not in ua:
        ua = f"{ua} (mailto:{contact})"
    return ua


def http_get(url: str, timeout: float = 15.0, user_agent: str = DEFAULT_UA,
             max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
    """发一次 GET。HTTP 错误也算一次成功探测(错误正文本身是证据)。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "identity",
    })
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
            return {
                "status": resp.status, "final_url": resp.geturl(),
                "content_type": resp.headers.get("Content-Type", "") or "",
                "headers": dict(resp.headers.items()), "body": body,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(max_bytes)
        except Exception:                                    # noqa: BLE001
            body = b""
        return {
            "status": exc.code, "final_url": url,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "headers": dict(exc.headers.items()) if exc.headers else {},
            "body": body, "latency_ms": int((time.perf_counter() - t0) * 1000),
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:                                 # noqa: BLE001
        return {
            "status": None, "final_url": url, "content_type": "", "headers": {},
            "body": b"", "latency_ms": int((time.perf_counter() - t0) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


# ── 内容类型与结构 ───────────────────────────────────────────────────────
def detect_api_style(content_type: str, body: str) -> tuple[str, str]:
    """按 content-type + 正文嗅探判断 API 风格。返回 (style, 依据)。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct:
        if "json" in ct:
            return "json", f"content-type={ct}"
        if "xml" in ct:
            return "xml", f"content-type={ct}"
        if "tab-separated" in ct:
            return "tsv", f"content-type={ct}"
        if "csv" in ct:
            return "csv", f"content-type={ct}"
        if "html" in ct:
            return "html", f"content-type={ct}"
        if "text/plain" in ct:
            return "text", f"content-type={ct}"
    stripped = (body or "").lstrip()
    if not stripped:
        return "text", "空正文"
    if stripped[:1] in "{[":
        return "json", "正文以 { 或 [ 开头(嗅探)"
    low = stripped[:200].lower()
    if low.startswith("<?xml") or low.startswith("<!doctype") or low.startswith("<html"):
        return "xml", "正文以 XML/HTML 标签开头(嗅探)"
    first = stripped.splitlines()[0] if stripped.splitlines() else ""
    if "\t" in first and first.count("\t") >= 1:
        return "tsv", "正文首行含制表符(嗅探)"
    if "," in first:
        return "csv", "正文首行含逗号(嗅探)"
    return "text", "无法归类的纯文本"


def parse_json_body(body: str) -> tuple[Any | None, str | None]:
    """解析 JSON 正文。失败返回 (None, 错误说明) —— 不猜。"""
    text = (body or "").strip()
    if not text:
        return None, "正文为空"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败: {exc} (位置 {exc.pos})"


def _walk_dicts(node: Any, prefix: str = "", depth: int = 0, max_depth: int = 3):
    """遍历 dict 节点(不进数组), 产出 (路径, 节点)。"""
    if depth > max_depth or not isinstance(node, dict):
        return
    yield prefix, node
    for key, val in node.items():
        if isinstance(val, dict):
            path = f"{prefix}.{key}" if prefix else key
            yield from _walk_dicts(val, path, depth + 1, max_depth)


def _find_arrays(payload: Any, max_depth: int = 3) -> list[tuple[str, str, int]]:
    """找出深度 <= max_depth 的所有数组字段, 返回 (路径, 键名, 深度)。"""
    out: list[tuple[str, str, int]] = []
    if not isinstance(payload, dict):
        return out

    def rec(node: dict, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        for key, val in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(val, list):
                out.append((path, key, depth + 1))
            elif isinstance(val, dict):
                rec(val, path, depth + 1)

    rec(payload, "", 0)
    return out


def infer_item_path(payload: Any) -> dict[str, Any]:
    """推断 item_path / item_shape。返回 dict: item_path, item_shape, evidence。"""
    if isinstance(payload, list):
        return {
            "item_path": None, "item_shape": "list",
            "evidence": "顶层就是数组, 整包即列表, item_path 无意义(网关按整包返回)",
        }
    if not isinstance(payload, dict):
        return {
            "item_path": None, "item_shape": None,
            "evidence": f"顶层是 {type(payload).__name__}, 不是对象也不是数组, 无法定位列表",
        }
    arrays = _find_arrays(payload, max_depth=3)
    if not arrays:
        values = list(payload.values())
        if values and all(isinstance(v, dict) for v in values):
            return {
                "item_path": None, "item_shape": "map",
                "evidence": "顶层是 {id: 记录} 字典(所有值都是对象), 按 map 处理",
            }
        return {
            "item_path": None, "item_shape": None,
            "evidence": "深度<=3 的对象里没有任何数组字段, 无法定位列表",
        }
    preferred = [a for a in arrays if a[1].lower() in PREFERRED_ITEM_KEYS]
    chosen = (preferred or arrays)[0]
    path, key, depth = chosen
    if preferred:
        why = (f"深度<=3 内第一个数组字段, 键名 '{key}' 命中优先名表"
               f"({'/'.join(PREFERRED_ITEM_KEYS[:5])}...)")
    else:
        why = f"深度<=3 内第一个数组字段, 键名 '{key}' 不在优先名表, 取首个数组"
    return {
        "item_path": path, "item_shape": "list",
        "evidence": f"{why}; 路径={path}, 深度={depth}, 数组长度={len(payload.get(key, []) or []) if depth == 1 else '见正文'}",
    }


def infer_total_field(payload: Any) -> dict[str, Any]:
    """推断 total_field。返回 dict: total_field, evidence, candidates。"""
    if not isinstance(payload, dict):
        return {"total_field": None, "evidence": "顶层不是对象, 无总数字段可言", "candidates": []}

    candidates: list[tuple[str, str, int, int, Any]] = []   # (path, key, prio, depth, value)
    for prefix, node in _walk_dicts(payload, "", 0, 3):
        depth = 0 if not prefix else prefix.count(".") + 1
        for key, val in node.items():
            if isinstance(val, bool) or not isinstance(val, int | float):
                continue
            key_l = key.lower()
            prio = None
            if key_l in TOTAL_KEY_PRIORITY:
                prio = TOTAL_KEY_PRIORITY.index(key_l)
            else:
                full = f"{prefix}.{key}" if prefix else key
                if full.lower() in TOTAL_KEY_PATHS:
                    prio = TOTAL_KEY_PATHS.index(full.lower())
            if prio is not None:
                path = f"{prefix}.{key}" if prefix else key
                candidates.append((path, key, prio, depth, val))

    if not candidates:
        return {
            "total_field": None,
            "evidence": ("深度<=3 的对象里没有找到 total/count/totalCount/numFound/total_results "
                         "这类数值型总数字段"),
            "candidates": [],
        }
    candidates.sort(key=lambda c: (c[2], c[3], c[0]))
    path, key, prio, depth, value = candidates[0]
    evidence = (f"键名 '{key}' 命中总数字段优先表(第 {prio + 1} 顺位), 值={value}, "
                f"路径={path}, 深度={depth}")
    same = [c for c in candidates if c[1] == key]
    if len(same) > 1:
        evidence += f"; 同名候选有 {len(same)} 个({', '.join(c[0] for c in same)}), 取第一个"
    return {
        "total_field": path, "evidence": evidence,
        "candidates": [{"path": c[0], "value": c[4]} for c in candidates[:8]],
    }


def extract_fields(payload: Any, item_path: str | None) -> dict[str, Any]:
    """从第一条记录里取字段名与类型。"""
    items = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and item_path:
        node: Any = payload
        for part in item_path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
                break
        if isinstance(node, list):
            items = node
    if not items or not isinstance(items, list) or not isinstance(items[0], dict):
        return {"fields": [], "field_types": {},
                "evidence": "没取到字典型记录, 无法列字段"}
    first = items[0]
    types: dict[str, str] = {}
    for key, val in first.items():
        types[key] = ("null" if val is None else
                      "array" if isinstance(val, list) else
                      "object" if isinstance(val, dict) else
                      "int" if isinstance(val, int) and not isinstance(val, bool) else
                      "float" if isinstance(val, float) else
                      "bool" if isinstance(val, bool) else "string")
    return {
        "fields": list(first.keys()), "field_types": types,
        "evidence": f"取第一条记录({len(first)} 个字段)的键名与 JSON 类型",
    }


def resolve_path(payload: Any, path: str) -> Any:
    """按点号路径取值(与网关 Resolver._project 的 total_field 解析一致)。"""
    node: Any = payload
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            idx = int(part)
            node = node[idx] if idx < len(node) else None
        else:
            return None
    return node


def detect_delimited(body: str) -> dict[str, Any]:
    """非 JSON 正文: 认分隔符与列名, 但不编 item_path。"""
    text = body or ""
    first = text.splitlines()[0] if text.splitlines() else ""
    delim = "\t" if "\t" in first else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    except Exception:                                        # noqa: BLE001
        rows = []
    columns = rows[0] if rows else []
    return {
        "delimiter": "\\t" if delim == "\t" else ",",
        "columns": columns,
        "column_count": len(columns),
        "row_count": len(rows) - 1 if rows else 0,
        "evidence": (f"按分隔符 {delim!r} 解析, 首行列名 {len(columns)} 个"
                     f"({', '.join(columns[:8])}{'...' if len(columns) > 8 else ''})"),
    }


# ── 认证与限速 ───────────────────────────────────────────────────────────
def detect_auth(status: int | None, headers: dict[str, str], body: str) -> dict[str, Any]:
    """从状态码/响应头/正文判断是否要认证。"""
    low = {k.lower(): v for k, v in (headers or {}).items()}
    text = (body or "")[:4000]
    evidence: list[str] = []
    kind = "none"
    required = False

    if status in (401, 403):
        required = True
        evidence.append(f"HTTP {status} 拒绝匿名访问")
    wa = low.get("www-authenticate")
    if wa:
        scheme = (wa.split() or [""])[0].lower()
        kind = {"bearer": "token", "basic": "basic", "digest": "basic"}.get(scheme, "unknown")
        evidence.append(f"WWW-Authenticate: {wa[:100]}")
    for key, typ, label in (("api_key", r"\b(api[ _\-]?key|apikey|x-api-key)\b", "api_key"),
                            ("token", r"\b(bearer|oauth2?|access[ _\-]?token)\b", "token"),
                            ("email", r"\b(mailto|your e-?mail address)\b", "email")):
        m = re.search(typ, text, re.I)
        if m:
            evidence.append(f"正文提到 {m.group(0)!r} → 疑似需要 {label}")
            if kind in ("none", "unknown"):
                kind = key
            break
    if kind == "none" and status is not None and status >= 400:
        kind = "unknown"
        evidence.append(f"HTTP {status} 且无认证线索, 认证方式待人工确认")
    if kind == "api_key" and not required:
        evidence.append("本次未带凭据即返回 200 → 凭据很可能可选")
    if kind == "none" and not required:
        evidence.append("无 401/403、无 WWW-Authenticate、正文无凭据线索 → 匿名可访问")
    return {
        "required": required, "kind": kind,
        "evidence": "; ".join(evidence) if evidence else "无认证线索",
    }


def detect_rate_limit(headers: dict[str, str]) -> dict[str, Any]:
    """从响应头里找限速信号。"""
    low = {k.lower(): v for k, v in (headers or {}).items()}
    picked: dict[str, str] = {}
    for key, val in low.items():
        if key.startswith(("x-ratelimit-", "x-rate-limit-", "ratelimit-")) or key == "retry-after":
            picked[key] = val
    declared: float | int | None = None
    evidence: list[str] = []
    for key in ("x-ratelimit-limit", "x-rate-limit-limit", "ratelimit-limit",
                "ratelimit-requests-limit", "x-ratelimit-requests-limit"):
        if key in picked:
            m = re.search(r"\d+", picked[key])
            if m:
                declared = int(m.group(0))
                evidence.append(f"{key}={picked[key]} → 上限 {declared}")
            break
    if "retry-after" in picked:
        evidence.append(f"retry-after={picked['retry-after']} (重试等待秒数, 不是速率上限)")
    return {
        "headers": picked, "declared": declared,
        "evidence": "; ".join(evidence) if evidence else "响应头里没有限速信号",
    }


# ── 路径探测 ─────────────────────────────────────────────────────────────
def probe_bases(url: str) -> list[str]:
    """探测基址: 站点根 + 当前路径的父目录。"""
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    bases = [origin]
    path = parts.path or "/"
    parent = path.rsplit("/", 1)[0]
    if parent and parent != "/" and f"{origin}{parent}" not in bases:
        bases.append(f"{origin}{parent}")
    return bases


def run_probes(url: str, timeout: float, user_agent: str, budget: int) -> list[dict[str, Any]]:
    """按常规路径列表探测, 总请求数受 budget 限制。"""
    results: list[dict[str, Any]] = []
    bases = probe_bases(url)
    for path in PROBE_PATHS:
        for base in bases:
            if len(results) >= budget:
                return results
            full = f"{base}{path}"
            res = http_get(full, timeout=timeout, user_agent=user_agent, max_bytes=256 * 1024)
            results.append({
                "path": path, "base": base, "url": full, "status": res["status"],
                "content_type": (res["content_type"] or "").split(";")[0].strip(),
                "bytes": len(res["body"]), "latency_ms": res["latency_ms"],
                "error": res["error"],
            })
    return results


# ── 报告 ─────────────────────────────────────────────────────────────────
def build_report(url: str, probe: dict[str, Any], probes: list[dict[str, Any]],
                 max_probes: int) -> dict[str, Any]:
    """把探测结果组装成稳定 schema 的报告。"""
    warnings: list[str] = []
    errors: list[str] = []
    inference: dict[str, str] = {}

    if probe["error"] and probe["status"] is None:
        errors.append(f"首次请求失败: {probe['error']}")
    body_bytes = probe["body"] or b""
    text = body_bytes.decode("utf-8", "replace")

    style, style_why = detect_api_style(probe["content_type"], text)
    inference["api_style"] = style_why

    # 探测 URL 上的查询参数: 决定上游报的 total 是全量口径还是过滤后的子集
    query_params = dict(urllib.parse.parse_qsl(
        urllib.parse.urlsplit(probe["final_url"] or url).query, keep_blank_values=True))
    scoped_params = [k for k in query_params if k.lower() not in PAGING_ONLY_PARAMS]
    total_is_global = not scoped_params
    inference["total_is_global"] = (
        "探测 URL 只带分页/礼貌池参数 → 上游 total 是全量口径"
        if total_is_global else
        f"探测 URL 带了 {', '.join(scoped_params)} → 上游 total 只覆盖过滤后的子集, 不能当全量规模")
    if not total_is_global:
        warnings.append(f"探测 URL 带过滤参数({', '.join(scoped_params)}), "
                        f"total 只能当子集规模, 实测规模需另取无过滤口径")

    item_path = None
    item_shape = None
    total_field = None
    total_value: int | None = None
    fields: list[str] = []
    field_types: dict[str, str] = {}
    payload: Any = None
    columns: list[str] = []
    delimiter: str | None = None

    if style == "json":
        payload, err = parse_json_body(text)
        if err:
            warnings.append(f"{err} —— item_path/total_field 无法推断")
            inference["item_path"] = f"未推断: {err}"
            inference["total_field"] = f"未推断: {err}"
        else:
            item = infer_item_path(payload)
            item_path, item_shape = item["item_path"], item["item_shape"]
            inference["item_path"] = item["evidence"]
            inference["item_shape"] = item["evidence"]
            if item_path is None and item_shape is None:
                warnings.append("JSON 里没找到数组字段: 需要人工确认列表位置")
            tot = infer_total_field(payload)
            total_field = tot["total_field"]
            inference["total_field"] = tot["evidence"]
            if total_field is None:
                warnings.append("没找到总数字段: 端点可以不声明 total_field")
            else:
                raw_total = resolve_path(payload, total_field)
                if isinstance(raw_total, int | float) and not isinstance(raw_total, bool):
                    total_value = int(raw_total)
                    inference["total_value"] = (
                        f"读 {total_field} 字段的实际值 = {total_value}"
                        f"{'(该请求带过滤条件, 数字只覆盖过滤后的子集)' if not total_is_global else ''}")
                else:
                    warnings.append(f"total_field 指向的 {total_field} 不是数字, 无法用于实测规模")
            fx = extract_fields(payload, item_path)
            fields, field_types = fx["fields"], fx["field_types"]
            inference["fields"] = fx["evidence"]
    elif style in ("csv", "tsv"):
        det = detect_delimited(text)
        columns = det["columns"]
        delimiter = det["delimiter"]
        inference["columns"] = det["evidence"]
        inference["item_path"] = "非 JSON 正文, 不编造 item_path(网关按 csv/tsv 形状整包解析)"
        warnings.append(f"{style} 正文: 网关需用 shape={style} 的端点, 列表语义由调用方自己解释")
    else:
        inference["item_path"] = f"{style} 正文, 不编造 item_path"
        warnings.append(f"{style} 正文: 无法自动推断列表结构, 需人工确认端点形状")

    auth = detect_auth(probe["status"], probe["headers"], text)
    inference["auth"] = auth["evidence"]
    rate = detect_rate_limit(probe["headers"])
    inference["rate_limit"] = rate["evidence"]
    if rate["declared"] is None:
        warnings.append("未探测到限速头: 官方限速口径需人工确认(可写'未公布')")

    docs_urls = [p["url"] for p in probes
                 if p["status"] == 200 and any(t in p["path"].lower() for t in DOC_PATH_TOKENS)]
    inference["docs_urls"] = (f"探测 {len(probes)} 个常规路径, 其中 {len(docs_urls)} 个返回 200 且路径像文档"
                              if probes else "未做路径探测(--no-probe)")

    report: dict[str, Any] = {
        "url": url,
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "final_url": probe["final_url"] or url,
        "status": probe["status"],
        "content_type": (probe["content_type"] or "").split(";")[0].strip() or None,
        "latency_ms": probe["latency_ms"],
        "body_bytes": len(body_bytes),
        "body_sample": text[:BODY_SAMPLE_BYTES],
        "api_style": style,
        "auth": auth,
        "rate_limit": rate,
        "item_path": item_path,
        "item_shape": item_shape,
        "total_field": total_field,
        "total_value": total_value,
        "total_is_global": total_is_global,
        "query_params": query_params,
        "fields": fields,
        "field_types": field_types,
        "columns": columns,
        "delimiter": delimiter,
        "docs_urls": docs_urls,
        "probes": probes,
        "max_probes": max_probes,
        "warnings": warnings,
        "errors": errors,
        "inference": inference,
    }
    if payload is not None:
        report["payload_is_json"] = True
    return report


def summarize(report: dict[str, Any]) -> str:
    """人看的摘要。"""
    lines = [
        f"URL       : {report['url']}",
        f"最终 URL  : {report['final_url']}",
        f"状态/耗时 : {report['status']} / {report['latency_ms']}ms / {report['body_bytes']}B",
        f"内容类型  : {report['content_type']}  → API 风格 {report['api_style']}",
        f"认证      : required={report['auth']['required']} kind={report['auth']['kind']}",
        f"限速      : declared={report['rate_limit']['declared']} "
        f"headers={list(report['rate_limit']['headers'])}",
        f"item_path : {report['item_path']}  (item_shape={report['item_shape']})",
        f"total     : {report['total_field']}",
        f"字段      : {', '.join(report['fields'][:12])}"
        f"{' ...' if len(report['fields']) > 12 else ''}",
        f"文档地址  : {', '.join(report['docs_urls']) or '(未发现)'}",
    ]
    for w in report["warnings"]:
        lines.append(f"  ⚠ {w}")
    for e in report["errors"]:
        lines.append(f"  ✗ {e}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="recon.py",
        description="候选数据源侦察: 探测状态/延迟/结构/认证/限速, 推断 item_path 与 total_field。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例:\n  python3 scripts/recon.py 'https://api.crossref.org/works?rows=1' -o recon.json")
    ap.add_argument("url", help="要探测的 URL")
    ap.add_argument("-o", "--out", default="recon.json", help="报告输出路径(默认 ./recon.json)")
    ap.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒(默认 15)")
    ap.add_argument("--user-agent", default="", help="自定义 User-Agent")
    ap.add_argument("--contact", default="", help="联系邮箱: 拼进 UA(很多官方 API 强制要求)")
    ap.add_argument("--no-probe", action="store_true", help="跳过常规路径探测")
    ap.add_argument("--max-probes", type=int, default=MAX_TOTAL_REQUESTS,
                    help=f"总请求数上限(含首次探测, 默认 {MAX_TOTAL_REQUESTS})")
    ap.add_argument("--quiet", action="store_true", help="只写文件, 不打印摘要")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ua = build_user_agent(args.user_agent, args.contact)

    probe = http_get(args.url, timeout=args.timeout, user_agent=ua)
    if probe["status"] is None:
        print(f"✗ 连不上 {args.url}: {probe['error']}", file=sys.stderr)
        report = {
            "url": args.url,
            "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "final_url": args.url, "status": None, "content_type": None, "latency_ms": probe["latency_ms"],
            "body_bytes": 0, "body_sample": "", "api_style": None,
            "auth": {"required": None, "kind": None, "evidence": "未取得响应, 无法判断"},
            "rate_limit": {"headers": {}, "declared": None, "evidence": "未取得响应"},
            "item_path": None, "item_shape": None, "total_field": None,
            "total_value": None, "total_is_global": False, "query_params": {},
            "fields": [], "field_types": {}, "columns": [], "delimiter": None,
            "docs_urls": [], "probes": [], "max_probes": args.max_probes,
            "warnings": ["上游不可达, 本报告不含任何实测结论"],
            "errors": [f"首次请求失败: {probe['error']}"],
            "inference": {"item_path": "未推断: 上游不可达", "total_field": "未推断: 上游不可达"},
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出 {args.out}(仅含失败记录)", file=sys.stderr)
        return 2

    budget = max(0, min(args.max_probes, MAX_TOTAL_REQUESTS) - 1)
    probes: list[dict[str, Any]] = []
    if not args.no_probe and budget > 0:
        probes = run_probes(args.url, timeout=min(args.timeout, 8.0), user_agent=ua, budget=budget)

    report = build_report(args.url, probe, probes, args.max_probes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.quiet:
        print(summarize(report))
        print(f"\n报告已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
