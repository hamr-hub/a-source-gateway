#!/usr/bin/env python3
"""
demo.py - "把 xxx 网站加进来" 的最后一步: 给出**调用示例 + 真实数据**。

    python3 scripts/demo.py crossref --endpoint works --limit 2

输出三块:
  1) 端点说明(做什么、要哪些必填参数、限速口径原文)
  2) 可直接粘贴的 curl
  3) 真实响应信封(data 截断到前 1-2 条, 标注取数时间)

降级(上游超时/报错/缓存过期)一律非零退出 —— 不拿降级数据当"跑通了"给人看。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8848"


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
_verify = _bs.load_sibling("verify_source")


def truncate_data(data: Any, keep: int = 2) -> Any:
    """data 只留前 1-2 条, 免得刷屏。"""
    if isinstance(data, list):
        return data[:keep]
    return data


def build_curl(gateway_url: str, source_id: str, endpoint_id: str,
               params: dict[str, Any], limit: int) -> str:
    body = json.dumps({"source": source_id, "endpoint": endpoint_id,
                       "params": params, "limit": limit}, ensure_ascii=False)
    return (f"curl -s -X POST {gateway_url.rstrip('/')}/v1/query "
            f"-H 'Content-Type: application/json' -d '{body}'")


def describe(src: dict[str, Any], ep: dict[str, Any]) -> str:
    resp = ep.get("response") or {}
    auth = src.get("auth") or {}
    limits = src.get("limits") or {}
    lines = [
        f"源      : {src.get('name')}  (id={src.get('id')})",
        f"机构/范围: {src.get('org')} / {src.get('country')}",
        f"内容    : {src.get('summary')}",
        f"官方入口: {src.get('homepage')}  [链接状态 {src.get('link_status')}]",
        f"认证    : {auth.get('type')}{'(可选)' if auth.get('optional') else ''} — {auth.get('note')}",
        f"限速口径: {limits.get('note') or '(未公布)'}",
        f"端点    : {ep.get('id')}  {ep.get('method', 'GET')}  {ep.get('name')}",
        f"响应形状: {resp.get('shape')}  item_path={resp.get('item_path')}  "
        f"total_field={resp.get('total_field')}",
        f"缓存/预算: {ep.get('cache_ttl_s')}s / {ep.get('budget_ms')}ms",
    ]
    required = [p for p in (ep.get("params") or []) if p.get("required")]
    optional = [p for p in (ep.get("params") or []) if not p.get("required")]
    lines.append("必填参数:")
    if required:
        for p in required:
            lines.append(f"  - {p.get('name')} ({p.get('type', 'string')}, {p.get('in', 'query')}): "
                         f"{p.get('desc', '')}  例: {p.get('example')}")
    else:
        lines.append("  (无)")
    if optional:
        lines.append("可选参数:")
        for p in optional:
            default = p.get("default")
            suffix = f"  默认: {default}" if default is not None else ""
            lines.append(f"  - {p.get('name')} ({p.get('type', 'string')}, {p.get('in', 'query')}): "
                         f"{p.get('desc', '')}{suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="demo.py",
        description="生成'调用示例 + 真实数据'的最终交付物。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例:\n  python3 scripts/demo.py crossref --endpoint works --limit 2")
    ap.add_argument("source", help="数据源 id / 名称 / 别名")
    ap.add_argument("--endpoint", default="", help="端点 id(默认第一个非 entry 端点)")
    ap.add_argument("--limit", type=int, default=2, help="取多少条(默认 2)")
    ap.add_argument("--params", default="", help="额外参数 JSON 串")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    ap.add_argument("--data-dir", default="", help="数据文件目录(默认 <repo>/data)")
    ap.add_argument("--gateway-url", default="", help=f"网关地址(默认 {DEFAULT_GATEWAY_URL})")
    ap.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒")
    ap.add_argument("--contact", default="", help="联系邮箱(拼进 UA)")
    args = ap.parse_args(argv)

    override: dict[str, Any] = {}
    if args.params:
        try:
            override = json.loads(args.params)
        except json.JSONDecodeError as exc:
            print(f"✗ --params 不是合法 JSON: {exc}", file=sys.stderr)
            return 2

    gateway_url = args.gateway_url or DEFAULT_GATEWAY_URL
    runtime = _bs.GatewayRuntime(backend="auto", data_dir=args.data_dir or None)
    src = runtime.get_source(args.source)
    if not src:
        print(f"✗ 未知数据源: {args.source}", file=sys.stderr)
        return 1
    ep = _verify.pick_endpoint(src, args.endpoint)
    if not ep:
        print(f"✗ {src['id']} 没有端点 {args.endpoint!r}", file=sys.stderr)
        return 1

    params, missing = _verify.collect_params(ep, override)
    if missing:
        print(f"✗ 端点必填参数缺 example/default: {', '.join(missing)}; 用 --params 补上",
              file=sys.stderr)
        return 1

    curl = build_curl(gateway_url, src["id"], ep.get("id", ""), params, args.limit)
    mode = "gateway"
    envelope: dict[str, Any] | None = None

    payload_req = {"source": src["id"], "endpoint": ep.get("id"), "params": params,
                   "limit": args.limit}
    gw = _verify.http_post_json(f"{gateway_url.rstrip('/')}/v1/query", payload_req,
                                timeout=max(3.0, min(args.timeout, 10.0)))
    if gw["ok"] and gw["status"] == 200:
        try:
            envelope = json.loads(gw["body"].decode("utf-8", "replace"))
        except json.JSONDecodeError:
            envelope = None
    else:
        mode = "direct"

    if envelope is None:
        # 网关没在跑: 直连上游, 按网关同构的信封格式拼一份(会标明是直连模式)
        mode = "direct"
        try:
            url = runtime.render_url(ep, params)
        except KeyError as exc:
            print(f"✗ 渲染 URL 失败: 缺少参数 {exc}", file=sys.stderr)
            return 1
        ua = _verify.DEFAULT_UA
        if args.contact and args.contact not in ua:
            ua = f"{ua} (mailto:{args.contact})"
        direct = _verify.http_get(url, timeout=args.timeout, user_agent=ua)
        if direct["status"] is None:
            print(f"✗ 上游不可达: {url} ({direct['error']})", file=sys.stderr)
            print("  这是网络/环境问题, 不等于源坏了 —— 请换个网络或确认上游状态后重试。",
                  file=sys.stderr)
            return 2
        text = direct["body"].decode("utf-8", "replace")
        shape = (ep.get("response") or {}).get("shape", "object")
        payload = runtime.parse_body(text, shape)
        data, total, truncated = runtime.project(payload, ep, limit=args.limit)
        degraded = direct["status"] >= 400
        errors = [] if not degraded else [{"type": "upstream_error", "status": direct["status"],
                                           "message": text[:300]}]
        envelope = {
            "meta": {
                "request_id": "direct", "schema_version": "1.1",
                "source": src["id"], "endpoint": ep.get("id"),
                "upstream": {"url": url, "method": ep.get("method", "GET"),
                             "status": direct["status"], "latency_ms": direct["latency_ms"]},
                "latency_ms": direct["latency_ms"],
                "budget_ms": int(ep.get("budget_ms") or 400),
                "cached": False, "cache_age_s": 0, "degraded": degraded, "offline": False,
                "count": len(data) if isinstance(data, list) else 1,
                "total": total, "truncated": truncated, "next_cursor": None,
                "rate_limit": {}, "fallback": {}, "errors": errors,
                "note": "网关未在运行, 本信封由 scripts/demo.py 直连上游后按网关同构格式拼装",
            },
            "data": data,
            "fields": [f.get("name") for f in ((ep.get("response") or {}).get("fields") or [])],
        }

    meta = envelope.get("meta") or {}
    data = truncate_data(envelope.get("data"))
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    result = {
        "source": src["id"], "source_name": src.get("name"), "endpoint": ep.get("id"),
        "mode": mode, "fetched_at": fetched_at, "curl": curl,
        "description": describe(src, ep),
        "envelope": {"meta": meta, "data": data, "fields": envelope.get("fields")},
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("端点说明")
        print("=" * 72)
        print(result["description"])
        print()
        print("=" * 72)
        print("调用示例(可直接粘贴)")
        print("=" * 72)
        print(curl)
        print()
        print("=" * 72)
        print(f"真实响应(取数时间 {fetched_at}, 模式 {mode})")
        print("=" * 72)
        print(json.dumps(result["envelope"], ensure_ascii=False, indent=2))
        print()
        print("※ 以上数据是真实上游响应, 不是示例/占位。")

    if meta.get("degraded"):
        print("\n✗ 这次调用是降级的, 不能算'源已跑通':", file=sys.stderr)
        for err in (meta.get("errors") or []):
            print(f"  - {err}", file=sys.stderr)
        if meta.get("fallback"):
            print(f"  fallback: {meta['fallback']}", file=sys.stderr)
        return 1
    if meta.get("offline"):
        print("\n✗ 网关处于离线模式, 数据不是实时的", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
