#!/usr/bin/env python3
"""
verify_source.py - 新增源的证据闸门: 真的查一次, 然后断言登记的元数据是真的。

    python3 scripts/verify_source.py crossref --endpoint works
    python3 scripts/verify_source.py crossref --json        # 机器可读(CI/agent 用)

三种结局严格区分(退出码不同):
    0 verified     —— 真的取到数据, 声明的元数据全部对上
    1 failed/degraded —— 取到了但降级了, 或声明的 item_path/total_field 对不上
    2 unreachable  —— 上游根本连不上(这是"环境问题", 不是"源坏了")

"连不上"绝不等于"源有问题", 也不会伪造任何数据。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_UA = "a-source-gateway/1.0 (+https://github.com/a-source-gateway)"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8848"
MAX_SAMPLE_CHARS = 1200


def _bootstrap():
    import importlib.util
    path = Path(__file__).resolve().parent / "_asg_bootstrap.py"
    key = "_asg_bootstrap"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


_bs = _bootstrap()


# ── 工具 ─────────────────────────────────────────────────────────────────
def resolve_path(payload: Any, path: str) -> Any:
    """按点号路径取值(与网关 Resolver._project 解析 total_field 的规则一致)。"""
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


def http_get(url: str, timeout: float, user_agent: str,
             headers: dict[str, str] | None = None) -> dict[str, Any]:
    """取一次上游。

    headers 是端点专有请求头(如 Accept: application/json)。**必须带上**:
    不带就等于换了一个上游请求, 上游可能回另一种结构(ORCID 默认回 XML),
    于是断言拿到的响应和网关实际拿到的不是一回事, 判出假阴性。
    """
    req_headers = {"User-Agent": user_agent, "Accept": "*/*", "Accept-Encoding": "identity"}
    for k, v in (headers or {}).items():
        req_headers[k] = v
    req = urllib.request.Request(url, headers=req_headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4 * 1024 * 1024)
            return {"ok": True, "status": resp.status, "url": resp.geturl(),
                    "content_type": resp.headers.get("Content-Type", "") or "",
                    "body": body, "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "error": None}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(1024 * 1024)
        except Exception:                                    # noqa: BLE001
            body = b""
        return {"ok": False, "status": exc.code, "url": url,
                "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
                "body": body, "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": f"HTTP {exc.code}"}
    except Exception as exc:                                 # noqa: BLE001
        return {"ok": False, "status": None, "url": url, "content_type": "", "body": b"",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": f"{type(exc).__name__}: {exc}"}


def http_post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": DEFAULT_UA})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4 * 1024 * 1024)
            return {"ok": True, "status": resp.status, "body": body,
                    "latency_ms": int((time.perf_counter() - t0) * 1000), "error": None}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65536)
        except Exception:                                    # noqa: BLE001
            body = b""
        return {"ok": False, "status": exc.code, "body": body,
                "latency_ms": int((time.perf_counter() - t0) * 1000), "error": f"HTTP {exc.code}"}
    except Exception as exc:                                 # noqa: BLE001
        return {"ok": False, "status": None, "body": b"",
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": f"{type(exc).__name__}: {exc}"}


def truncate_json(obj: Any, limit: int = MAX_SAMPLE_CHARS) -> Any:
    """把对象裁到可读长度(字符串裁断, 列表只留前两项)。"""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out[k] = truncate_json(v, limit)
        return out
    if isinstance(obj, list):
        return [truncate_json(v, limit) for v in obj[:2]]
    if isinstance(obj, str) and len(obj) > limit:
        return obj[:limit] + f"...(截断, 原文 {len(obj)} 字符)"
    return obj


# ── 断言 ─────────────────────────────────────────────────────────────────
class Assertions:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, severity: str, detail: str) -> bool:
        self.items.append({"name": name, "ok": bool(ok), "severity": severity, "detail": detail})
        return bool(ok)

    @property
    def hard_failures(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i["severity"] == "hard" and not i["ok"]]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i["severity"] == "warn" and not i["ok"]]

    @property
    def ok(self) -> bool:
        return not self.hard_failures


def check_declared_fields(payload: Any, ep: dict, item_path: str | None,
                          assertions: Assertions) -> list[str]:
    """声明的字段名要么是真实 item 的键, 要么是能解析的路径。"""
    resp = ep.get("response") or {}
    declared = [f.get("name") for f in (resp.get("fields") or []) if f.get("name")]
    if not declared:
        assertions.add("declared_fields_present", True, "warn", "端点没有声明 response.fields")
        return []

    items: Any = None
    if item_path:
        items = resolve_path(payload, item_path)
    if not isinstance(items, list) and isinstance(payload, list):
        items = payload
    # 列表套列表时展平一层(与网关 item_flatten 语义一致), 否则取不到条目的键
    if (resp.get("item_flatten") and isinstance(items, list) and items
            and isinstance(items[0], list)):
        flat: list[Any] = []
        for sub in items:
            if isinstance(sub, list):
                flat.extend(sub)
            else:
                flat.append(sub)
        items = flat
    item_keys: set[str] = set()
    if isinstance(items, list) and items and isinstance(items[0], dict):
        item_keys = set(items[0].keys())

    missing: list[str] = []
    for name in declared:
        if name in item_keys:
            continue
        if resolve_path(payload, name) is not None:
            continue
        alt = name.replace("[]", ".")
        if alt != name and resolve_path(payload, alt) is not None:
            continue
        if alt.split(".")[-1] in item_keys:
            continue
        missing.append(name)
    assertions.add(
        "declared_fields_present", not missing, "warn",
        f"声明字段 {len(declared)} 个, 全部命中" if not missing
        else f"{len(missing)} 个声明字段没在真实响应里找到: {', '.join(missing[:10])}")
    return missing


# ── 主流程 ───────────────────────────────────────────────────────────────
def build_curl(gateway_url: str, source_id: str, endpoint_id: str,
               params: dict[str, Any], limit: int) -> str:
    body = json.dumps({"source": source_id, "endpoint": endpoint_id,
                       "params": params, "limit": limit}, ensure_ascii=False)
    return (f"curl -s -X POST {gateway_url.rstrip('/')}/v1/query "
            f"-H 'Content-Type: application/json' -d '{body}'")


def pick_endpoint(src: dict[str, Any], wanted: str) -> dict[str, Any] | None:
    eps = src.get("endpoints") or []
    if wanted:
        return next((e for e in eps if e.get("id") == wanted), None)
    for e in eps:
        if e.get("id") != "entry":
            return e
    return eps[0] if eps else None


def collect_params(ep: dict[str, Any], override: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """必填参数从 example/default 取; 取不到就报出来(不编造)。"""
    params: dict[str, Any] = {}
    missing: list[str] = []
    for p in ep.get("params", []):
        name = p.get("name")
        if not name:
            continue
        if p.get("required"):
            if p.get("example") is not None:
                params[name] = p["example"]
            elif "default" in p:
                params[name] = p["default"]
            else:
                missing.append(name)
        elif p.get("default") is not None:
            params[name] = p["default"]
    params.update(override or {})
    return params, missing


def run_all(args: argparse.Namespace) -> int:
    """复验注册表里的所有源(逐源子进程, 单源失败不拖垮整批)。

    输出一份汇总 JSON(mode="all");退出码等于未通过源数,上限 100,
    unreachable 不计入失败(那是环境问题,不是源的问题,由报告里的状态体现)。
    """
    try:
        runtime = _bs.GatewayRuntime(backend=args.resolver_backend,
                                     data_dir=args.data_dir or None)
    except Exception as exc:  # noqa: BLE001
        json.dump({"mode": "all", "error": f"载入注册表失败: {exc}", "results": []},
                  sys.stdout, ensure_ascii=False, indent=2)
        return 2
    ids = sorted(runtime.sources_by_id())

    results: list[dict[str, Any]] = []
    unreachable = 0
    failures = 0
    cmd_base = [sys.executable, str(Path(__file__).resolve()), "--json",
                "--limit", str(args.limit), "--timeout", str(args.timeout),
                "--resolver-backend", args.resolver_backend]
    if args.data_dir:
        cmd_base += ["--data-dir", args.data_dir]
    if args.contact:
        cmd_base += ["--contact", args.contact]
    if args.no_raw_refetch:
        cmd_base += ["--no-raw-refetch"]
    if args.direct_only:
        cmd_base += ["--direct-only"]

    for i, sid in enumerate(ids, 1):
        try:
            proc = subprocess.run(cmd_base + [sid], capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            results.append({"source": sid, "status": "unreachable",
                            "errors": ["单源复验超过 90s, 按不可达计"]})
            unreachable += 1
            print(f"[{i}/{len(ids)}] {sid}: unreachable (timeout)", file=sys.stderr)
            continue
        try:
            row = json.loads(proc.stdout)
        except json.JSONDecodeError:
            row = {"source": sid, "status": "failed",
                   "errors": [f"子进程输出无法解析 (rc={proc.returncode}): {proc.stderr[-300:]}"]}
        results.append(row)
        status = row.get("status", "failed")
        if status == "unreachable":
            unreachable += 1
        elif status != "verified":
            failures += 1
        print(f"[{i}/{len(ids)}] {sid}: {status}", file=sys.stderr)

    summary = {
        "mode": "all",
        "checked_at": now_iso(),
        "total": len(results),
        "verified": sum(1 for r in results if r.get("status") == "verified"),
        "failed": failures,
        "unreachable": unreachable,
        "results": results,
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    return min(failures, 100)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify_source.py",
        description="证据闸门: 真查一次上游, 断言登记的 item_path/total_field/fields 是真的。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码: 0=已用真实数据验证通过 / 1=断言失败或降级 / 2=上游不可达")
    ap.add_argument("source", nargs="?", default="",
                    help="数据源 id / 名称 / 别名(--all 时省略)")
    ap.add_argument("--endpoint", default="", help="端点 id(默认取第一个非 entry 端点)")
    ap.add_argument("--all", action="store_true",
                    help="复验注册表里所有源;输出汇总 JSON, 退出码=未通过源数(上限 100)")
    ap.add_argument("--json", action="store_true", help="输出机器可读的验证报告")
    ap.add_argument("--data-dir", default="", help="数据文件目录(默认 <repo>/data)")
    ap.add_argument("--gateway-url", default="", help=f"网关地址(默认 {DEFAULT_GATEWAY_URL})")
    ap.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒(默认 20)")
    ap.add_argument("--limit", type=int, default=3, help="取多少条(默认 3)")
    ap.add_argument("--params", default="", help="额外参数 JSON 串, 如 '{\"filter\":\"...\"}'")
    ap.add_argument("--contact", default="", help="联系邮箱(拼进 UA, 官方 API 常要求)")
    ap.add_argument("--resolver-backend", choices=["auto", "gateway", "mirror"], default="auto",
                    help="解析器实现来源: gateway=用网关自己的, mirror=用镜像实现")
    ap.add_argument("--no-raw-refetch", action="store_true",
                    help="网关可用时不再单独取一次原文(只按网关信封断言)")
    ap.add_argument("--direct-only", action="store_true",
                    help="不查网关, 直接打上游(只验源本身的元数据, 不验网关链路)")
    args = ap.parse_args(argv)

    if args.all:
        if args.source:
            ap.error("--all 与具体源不能同时给")
        if not args.json:
            print("✗ --all 只支持 --json 输出", file=sys.stderr)
            return 2
        return run_all(args)
    if not args.source:
        ap.error("缺少数据源 id/名称/别名(或用 --all 复验全部)")

    override: dict[str, Any] = {}
    if args.params:
        try:
            override = json.loads(args.params)
        except json.JSONDecodeError as exc:
            print(f"✗ --params 不是合法 JSON: {exc}", file=sys.stderr)
            return 2

    gateway_url = args.gateway_url or DEFAULT_GATEWAY_URL
    ua = DEFAULT_UA
    if args.contact and args.contact not in ua:
        ua = f"{ua} (mailto:{args.contact})"

    assertions = Assertions()
    warnings: list[str] = []
    errors: list[str] = []

    # 1) 找源
    try:
        runtime = _bs.GatewayRuntime(backend=args.resolver_backend,
                                     data_dir=args.data_dir or None)
    except Exception as exc:                                # noqa: BLE001
        print(f"✗ 载入注册表失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if runtime.backend == "mirror" and args.resolver_backend == "auto":
        warnings.append(f"网关包装不上, 退回镜像实现({runtime.reason})")

    src = runtime.get_source(args.source)
    if not src:
        report = {
            "source": args.source, "status": "failed", "checked_at": now_iso(),
            "resolver_backend": runtime.backend,
            "assertions": [{"name": "source_resolves", "ok": False, "severity": "hard",
                            "detail": f"注册表里找不到源 {args.source!r}"}],
            "errors": [f"未知数据源 {args.source}"], "warnings": warnings,
        }
        emit(report, args.json)
        return 1
    assertions.add("source_resolves", True, "hard", f"解析到源 id={src['id']} ({src['name']})")

    ep = pick_endpoint(src, args.endpoint)
    if not ep:
        report = {
            "source": src["id"], "status": "failed", "checked_at": now_iso(),
            "resolver_backend": runtime.backend,
            "assertions": [{"name": "endpoint_resolves", "ok": False, "severity": "hard",
                            "detail": f"源 {src['id']} 没有端点 {args.endpoint!r}"}],
            "errors": [f"端点不存在: {args.endpoint}"], "warnings": warnings,
        }
        emit(report, args.json)
        return 1
    assertions.add("endpoint_resolves", True, "hard",
                   f"端点 {ep.get('id')}({ep.get('method', 'GET')})")

    transport = "direct"
    gateway_call: dict[str, Any] = {"called": False, "note": "未查询网关"}
    used_params: dict[str, Any] = {}
    params, missing_params = collect_params(ep, override)
    used_params = dict(params)
    if missing_params:
        assertions.add("required_params_available", False, "hard",
                       f"必填参数缺 example/default: {', '.join(missing_params)};"
                       f"用 --params 补上")
        report = build_report(src, ep, runtime, args, gateway_url, assertions, warnings,
                              errors, status="failed", transport=transport, params=used_params,
                              gateway_call=gateway_call)
        report["errors"].append(f"必填参数缺 example/default: {', '.join(missing_params)}")
        emit(report, args.json)
        return 1

    resp_spec = ep.get("response") or {}
    item_path = resp_spec.get("item_path")
    total_field = resp_spec.get("total_field")
    shape = resp_spec.get("shape", "object")

    # 2) 先打网关(网关在跑就证明整条运行时链路是通的)
    envelope: dict[str, Any] | None = None
    raw_body: bytes | None = None
    raw_meta: dict[str, Any] = {}
    upstream_status: int | None = None
    upstream_url = ""
    upstream_latency = 0

    payload_req = {"source": src["id"], "endpoint": ep.get("id"), "params": used_params,
                   "limit": args.limit}
    gw = {"ok": False, "status": None, "body": b"", "error": "已按 --direct-only 跳过网关"}
    if not args.direct_only:
        gw = http_post_json(f"{gateway_url.rstrip('/')}/v1/query", payload_req,
                             timeout=max(3.0, min(args.timeout, 10.0)))
    if gw["ok"] and gw["status"] == 200:
        try:
            envelope = json.loads(gw["body"].decode("utf-8", "replace"))
            transport = "gateway"
        except json.JSONDecodeError:
            envelope = None
    elif gw["status"] in (404, 400):
        warnings.append(f"网关在跑但拒了这个查询(HTTP {gw['status']}), 改走直连上游")

    if envelope is not None:
        meta = envelope.get("meta") or {}
        upstream = meta.get("upstream") or {}
        upstream_status = upstream.get("status")
        upstream_url = upstream.get("url") or ""
        upstream_latency = upstream.get("latency_ms") or 0
        raw_meta = {"status": upstream_status, "url": upstream_url,
                    "content_type": "", "latency_ms": upstream_latency}
        gateway_call = {
            "called": True, "degraded": bool(meta.get("degraded")),
            "status": upstream_status, "url": upstream_url,
            "latency_ms": meta.get("latency_ms"),
            "errors": meta.get("errors") or [], "fallback": meta.get("fallback") or {},
            "total": meta.get("total"), "count": meta.get("count"),
        }
        if meta.get("degraded"):
            assertions.add("not_degraded", False, "hard",
                           f"网关返回降级: errors={meta.get('errors')} fallback={meta.get('fallback')}")
        else:
            assertions.add("not_degraded", True, "hard", "网关返回未降级")
        # 严格断言用原文: 再取一次网关实际用的那个 URL
        if upstream_url and not args.no_raw_refetch:
            direct = http_get(upstream_url, timeout=args.timeout, user_agent=ua,
                               headers=ep.get("headers") or None)
            if direct["status"] is not None:
                raw_body = direct["body"]
                raw_meta = {"status": direct["status"], "url": direct["url"],
                            "content_type": direct["content_type"],
                            "latency_ms": direct["latency_ms"]}
                upstream_status = direct["status"]
            else:
                warnings.append(f"取原文失败({direct['error']}), item_path/total_field 只能按网关信封断言")
    else:
        # 3) 直连上游
        try:
            upstream_url = runtime.render_url(ep, params)
        except KeyError as exc:
            assertions.add("url_renderable", False, "hard", f"渲染 URL 失败: 缺少参数 {exc}")
            report = build_report(src, ep, runtime, args, gateway_url, assertions, warnings,
                                  errors, status="failed", transport=transport, params=used_params)
            emit(report, args.json)
            return 1
        assertions.add("url_renderable", True, "hard", f"渲染出 URL: {upstream_url}")
        direct = http_get(upstream_url, timeout=args.timeout, user_agent=ua,
                               headers=ep.get("headers") or None)
        if direct["status"] is None:
            report = build_report(src, ep, runtime, args, gateway_url, assertions, warnings,
                                  errors, status="unreachable", transport="direct",
                                  params=used_params, gateway_call=gateway_call)
            report["errors"].append(f"上游不可达: {direct['error']}")
            emit(report, args.json)
            return 2
        raw_body = direct["body"]
        raw_meta = {"status": direct["status"], "url": direct["url"],
                    "content_type": direct["content_type"], "latency_ms": direct["latency_ms"]}
        upstream_status = direct["status"]
        upstream_latency = direct["latency_ms"]
        gateway_call = {"called": False, "note": "网关不可用或未查询, 已直连上游"}

    # 4) 断言声明的元数据
    payload: Any = None
    data: Any = None
    body_not_json = False
    if raw_body is not None:
        text = raw_body.decode("utf-8", "replace")
        # 严格解析: 上游正文不是 JSON(错误页 / WAF 拦截页)时, 把它单独报出来。
        # 以前这里静默兜底成 {"_text": ...}, 于是元数据断言报成
        # "item_path=... → dict", 让人误以为是 item_path 写错了 ——
        # 实际上要改的是上游可访问性, 不是元数据。
        try:
            payload = runtime.parse_body(text, shape, strict=True)
        except _bs.UpstreamNotJSONError as exc:
            body_not_json = True
            payload = None
            assertions.add("upstream_body_is_json", False, "hard",
                           f"上游返回的不是 JSON(疑似 HTML 错误页): {exc}")
            # 正文不是 JSON 时, item_path / total_field / 字段名断言都没有意义
            # (拿错误页去比对路径, 只会误导)。如实记成"因上游非 JSON 跳过"。
            assertions.add("item_path_resolves_to_list", True, "warn",
                           "上游返回的不是 JSON, 已跳过该断言")
            assertions.add("total_field_resolves_to_number", True, "warn",
                           "上游返回的不是 JSON, 已跳过该断言")
            assertions.add("declared_fields_present", True, "warn",
                           "上游返回的不是 JSON, 已跳过该断言")
            assertions.add("data_not_empty", True, "warn",
                           "上游返回的不是 JSON, 已跳过该断言")
        if payload is not None:
            data, total, truncated = runtime.project(payload, ep, limit=args.limit)
        if upstream_status is not None and upstream_status >= 400:
            assertions.add("upstream_status_ok", False, "hard",
                           f"上游返回 HTTP {upstream_status}: {text[:200]}")
            assertions.add("upstream_not_error", False, "hard",
                           f"上游 HTTP {upstream_status}, 算降级, 不能算'源已跑通'")
        else:
            assertions.add("upstream_status_ok", True, "hard",
                           f"上游 HTTP {upstream_status}, {len(raw_body)}B")
            assertions.add("upstream_not_error", True, "hard", "上游正常返回, 未降级")
        if payload is not None:
            if item_path:
                node = resolve_path(payload, item_path)
                ok = isinstance(node, list)
                assertions.add("item_path_resolves_to_list", ok, "hard",
                               f"item_path={item_path!r} → {type(node).__name__}"
                               f"{f'(长度 {len(node)})' if isinstance(node, list) else ''}")
            else:
                assertions.add("item_path_resolves_to_list", True, "warn",
                               "端点没声明 item_path, 跳过该断言")
            if total_field:
                node = resolve_path(payload, total_field)
                ok = isinstance(node, int | float) and not isinstance(node, bool)
                assertions.add("total_field_resolves_to_number", ok, "hard",
                               f"total_field={total_field!r} → {node!r}")
            else:
                assertions.add("total_field_resolves_to_number", True, "warn",
                               "端点没声明 total_field, 跳过该断言")
            check_declared_fields(payload, ep, item_path, assertions)
            if isinstance(data, list) and not data:
                assertions.add("data_not_empty", False, "warn", "取到的是空列表(可能是过滤条件太窄)")
            else:
                assertions.add("data_not_empty", True, "warn",
                               f"取到 {len(data) if isinstance(data, list) else 1} 条")
    else:
        # 只有网关信封(没取原文): 按信封断言
        meta = envelope.get("meta") if envelope else {}
        gateway_data = (envelope or {}).get("data")
        data = gateway_data
        if item_path:
            ok = isinstance(gateway_data, list)
            assertions.add("item_path_resolves_to_list", ok, "hard",
                           f"按网关投影判断: item_path={item_path!r} → {type(gateway_data).__name__}")
        if total_field:
            ok = isinstance(meta.get("total"), int | float)
            assertions.add("total_field_resolves_to_number", ok, "hard",
                           f"按网关投影判断: total_field={total_field!r} → meta.total={meta.get('total')!r}")
        check_declared_fields(gateway_data, ep, item_path, assertions)

    status = "verified"
    if assertions.hard_failures:
        status = "failed"
        if any(a["name"] == "not_degraded" for a in assertions.hard_failures):
            status = "degraded"
    if upstream_status is None and transport == "direct":
        status = "unreachable"

    if body_not_json:
        # 诊断口径: 这是"上游给了错误页", 不是"端点元数据写错了"。
        errors.append("上游返回的不是 JSON(疑似 HTML 错误页) —— 改上游可访问性, 不要改 item_path/total_field")

    report = build_report(src, ep, runtime, args, gateway_url, assertions, warnings, errors,
                          raw_body=raw_body, data=data, status=status, raw_meta=raw_meta,
                          transport=transport, params=used_params, gateway_call=gateway_call)
    emit(report, args.json)
    if status == "verified":
        return 0
    if status == "unreachable":
        return 2
    return 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_report(src, ep, runtime, args, gateway_url, assertions, warnings, errors,
                 raw_body=None, data=None, status="failed", raw_meta=None,
                 transport="direct", params=None, gateway_call=None) -> dict[str, Any]:
    resp_spec = ep.get("response") or {}
    raw_meta = raw_meta or {}
    sample: Any = None
    if isinstance(data, list) and data:
        sample = truncate_json(data[0])
    elif isinstance(data, dict):
        sample = truncate_json(data)
    curl = build_curl(gateway_url, src["id"], ep.get("id", ""), params or {}, args.limit)
    return {
        "source": args.source,
        "resolved_source": src["id"],
        "source_name": src.get("name"),
        "endpoint": ep.get("id"),
        "status": status,
        "checked_at": now_iso(),
        "transport": transport,
        "resolver_backend": runtime.backend,
        "gateway_url": gateway_url,
        "upstream": {
            "url": raw_meta.get("url", ""),
            "status": raw_meta.get("status"),
            "latency_ms": raw_meta.get("latency_ms"),
            "content_type": raw_meta.get("content_type"),
            "bytes": len(raw_body) if raw_body is not None else None,
        },
        "declared": {
            "item_path": resp_spec.get("item_path"),
            "total_field": resp_spec.get("total_field"),
            "fields": [f.get("name") for f in (resp_spec.get("fields") or [])],
            "shape": resp_spec.get("shape"),
        },
        "gateway_call": gateway_call or {"called": False},
        "assertions": assertions.items,
        "sample": sample,
        "curl": curl,
        "errors": errors,
        "warnings": warnings + [f"{a['name']}: {a['detail']}" for a in assertions.warnings],
    }


def emit(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"源      : {report.get('source')} → {report.get('resolved_source')} "
          f"({report.get('source_name')})")
    print(f"端点    : {report.get('endpoint')}")
    print(f"结论    : {report.get('status').upper()}")
    up = report.get("upstream") or {}
    if up.get("url"):
        print(f"上游    : HTTP {up.get('status')} {up.get('latency_ms')}ms {up.get('bytes')}B "
              f"{up.get('url')}")
    print("断言:")
    for a in report.get("assertions", []):
        mark = "✓" if a["ok"] else ("✗" if a["severity"] == "hard" else "!")
        print(f"  {mark} [{a['severity']}] {a['name']}: {a['detail']}")
    if report.get("sample") is not None:
        print("真实响应样本(第一条):")
        print(json.dumps(report["sample"], ensure_ascii=False, indent=2)[:MAX_SAMPLE_CHARS])
    if report.get("curl"):
        print("可粘贴的调用:")
        print(report["curl"])
    for w in report.get("warnings", []):
        print(f"  ! {w}")
    for e in report.get("errors", []):
        print(f"  ✗ {e}")


if __name__ == "__main__":
    sys.exit(main())
