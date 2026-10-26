#!/usr/bin/env python3
"""
verify_source.py - 新增源的证据闸门: 真的查一次, 然后断言登记的元数据是真的。

    python3 scripts/verify_source.py crossref --endpoint works
    python3 scripts/verify_source.py crossref --json        # 机器可读(CI/agent 用)

四种结局严格区分(退出码不同):
    0 verified     —— 真的取到数据, 声明的元数据全部对上
    1 failed/degraded —— 取到了但降级了, 或声明的 item_path/total_field 对不上
    2 unreachable  —— 上游根本连不上(这是"环境问题", 不是"源坏了")
    3 credentials_missing —— 端点定义没问题, 只是本机没配对应的 A_GATEWAY_* 凭据

后两类都不算"元数据失真": 把环境/凭据问题混进失败数, 报告就没有信号了。

"连不上"绝不等于"源有问题", 也不会伪造任何数据。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
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
    """按点号路径取值(与网关 Resolver._project 解析 total_field 的规则一致)。

    点号切分取不到时, 再按整串字面键试一次 —— 上游 OData 的 "@odata.count"
    这类键名本身带点, 只按点号切分必定取不到。三个实现(Python resolver.py、
    Python parser_worker.py、Go parser.go)都有这个兜底, 复验脚本不跟上就会把
    正确的元数据判成错误(WHO GHO 的 total_field 就是 "@odata.count")。
    """
    node: Any = payload
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
    if isinstance(payload, dict) and path in payload:
        return payload[path]
    return None


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

    def drop(self, name: str) -> None:
        """撤销同名断言 —— 用于后来发现该断言在当前情形下不成立(而非失败)。"""
        self.items[:] = [i for i in self.items if i["name"] != name]

    @property
    def hard_failures(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i["severity"] == "hard" and not i["ok"]]

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i["severity"] == "warn" and not i["ok"]]

    @property
    def ok(self) -> bool:
        return not self.hard_failures


def quota_blocked(payload: Any) -> str:
    """识别"HTTP 200 但配额/限流耗尽"的响应。

    典型例子是 BLS: 状态码 200, 正文里 status=REQUEST_NOT_PROCESSED +
    "daily threshold ... has been reached"。把它当成 item_path 写错,
    会让人去改一份本来正确的元数据。返回人话原因, 没问题返回 ""。

    判定要同时满足两条, 否则容易误伤:
      1. status 像个"拒绝"(ERROR / FAIL / NOT_PROCESSED / LIMIT ...),
         而不是任意一个非 OK 的业务状态;
      2. message 里确实提到了配额/限流。
    """
    if not isinstance(payload, dict):
        return ""
    status = str(payload.get("status") or "").upper()
    if not status or status in ("OK", "200", "SUCCESS"):
        return ""
    refusal_tokens = ("ERROR", "FAIL", "NOT_PROCESSED", "INVALID", "DENIED",
                      "LIMIT", "EXCEEDED", "THRESHOLD", "QUOTA", "REJECTED")
    if not any(tok in status for tok in refusal_tokens):
        return ""
    parts = payload.get("message") or []
    if isinstance(parts, str):
        parts = [parts]
    text = " ".join(str(p) for p in parts)
    if any(k in text.lower() for k in ("threshold", "quota", "rate limit", "exceeded")):
        return f"status={status}: {text[:160]}"
    return ""


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
    """必填参数从 env/example/default 取; 取不到就报出来(不编造)。

    凭据参数(env)优先从环境变量取 —— 数据文件里不该有真实 key,
    也不能拿占位符(example="YOUR_XXX")去敲上游。

    missing 里放的是**参数名本身**(不带任何后缀): 调用方要靠名字判断
    "缺的是不是凭据"(见 describe_missing / credential 判定), 名字一旦被
    拼成人话, 就没法再按名字比对端点定义。
    """
    params: dict[str, Any] = {}
    missing: list[str] = []
    for p in ep.get("params", []):
        name = p.get("name")
        if not name:
            continue
        if p.get("required"):
            if p.get("env"):
                env_value = os.environ.get(p["env"], "")
                if env_value:
                    params[name] = env_value
                else:
                    missing.append(name)
            elif p.get("example") is not None:
                params[name] = p["example"]
            elif "default" in p:
                params[name] = p["default"]
            else:
                missing.append(name)
        elif p.get("default") is not None:
            params[name] = p["default"]
    params.update(override or {})
    return params, missing


def describe_missing(ep: dict[str, Any], missing: list[str]) -> list[str]:
    """把缺掉的参数名说成人话: 凭据类的要带上环境变量名。"""
    env_by_name = {p.get("name"): p.get("env") for p in ep.get("params", []) if p.get("name")}
    out: list[str] = []
    for name in missing:
        env = env_by_name.get(name)
        out.append(f"{name}(环境变量 {env} 未设置)" if env else name)
    return out


def is_slow(row: dict[str, Any]) -> bool:
    """这次降级是不是"网络慢"造成的, 而不是元数据写错。

    跨境往返常常是 0.4-7.5s, 网关的上游预算只有 400ms —— 于是调用
    **正常地**降级。那不是源的问题, 混进 failures 会让报告失去信号。
    """
    if row.get("status") != "degraded":
        return False
    types: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if isinstance(t, str):
                types.add(t)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(row)
    # 传输层失败的三个类型名(两个平面共用同一套词表, 见 resolver.py 的
    # _transport_error_type 与 Go 侧 upstreamErrorType):
    # connect_error / read_error / upstream_error。旧名 ConnectError 已不再使用。
    return bool(types & {"timeout", "budget_exceeded",
                         "connect_error", "read_error", "upstream_error"})


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
    slow = 0
    credentials_missing = 0
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
        elif status == "credentials_missing":
            # 端点定义是对的, 只是本机没配 A_GATEWAY_* 凭据。
            # 与 unreachable 同理: 不计入"元数据失真"的失败数。
            credentials_missing += 1
        elif is_slow(row):
            # 降级原因是跨境往返慢/预算不够, 不是元数据写错。
            # 与"真的写错了"混在一起统计, 报告就没有信号了:
            # 实测 109 源里 103 个 degraded, 其中 81 个只是超时。
            slow += 1
        elif status != "verified":
            failures += 1
        print(f"[{i}/{len(ids)}] {sid}: {status}"
              f"{' (网络慢)' if is_slow(row) else ''}", file=sys.stderr)

    summary = {
        "mode": "all",
        "checked_at": now_iso(),
        "total": len(results),
        "verified": sum(1 for r in results if r.get("status") == "verified"),
        "failed": failures,
        "slow": slow,
        "unreachable": unreachable,
        "credentials_missing": credentials_missing,
        "results": results,
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    return min(failures, 100)


_COL_NAME = "数据源"
_COL_VERIFIED = "最后验证"
# 两份清单按 国别 分流(规则见 AGENTS.md §2): 同一个源只应出现在其中一份里,
# 所以写回时两份都扫一遍, 命中哪份改哪份 —— 漏掉一份, 那条日期就会静默消失。
_INVENTORY_FILES = ("source_inventory.csv", "source_inventory_cn.csv")


def write_verified_dates(args: argparse.Namespace) -> int:
    """把本批次已核过的源的复核日期写回两份清单 CSV。

    守规矩的点有两个:
      1. 只有源名下每一个被复验过的端点都是 verified, 才敢把日期写成"这个源已核对"。
         任何 failed/degraded 都意味着元数据对不上, 那天就不是"核对通过"。
      2. unreachable(环境问题) 与 credentials_missing(本机没配凭据) 默认也拦下来,
         除非显式给了 --allow-partial-date —— 宁可让日期空着(运行时读到
         "unverified" 哨兵), 也不写一个自己没跑通过的日期。

    返回实际改写的行数。
    """
    report_path = Path(args.recap_json) if getattr(args, "recap_json", "") else None
    if report_path is None or not report_path.is_file():
        print("✗ --write-verified-date 需要 --recap-json 指向本次汇总 JSON",
              file=sys.stderr)
        return 0
    try:
        summary = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"✗ 读 {report_path} 失败: {exc}", file=sys.stderr)
        return 0

    statuses: dict[str, list[str]] = {}
    for row in summary.get("results") or []:
        name = str(row.get("source_name") or row.get("resolved_source") or "").strip()
        if name:
            statuses.setdefault(name, []).append(str(row.get("status") or "failed"))

    blocked = {"failed", "degraded"}
    if not args.allow_partial_date:
        blocked |= {"unreachable", "credentials_missing"}

    ok = {name for name, items in statuses.items() if not blocked.intersection(items)}
    if not ok:
        return 0

    written = 0
    for rel in _INVENTORY_FILES:
        path = Path(args.data_dir) / rel if args.data_dir else (
            Path(__file__).resolve().parent.parent / "data" / rel)
        if not path.is_file():
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            continue
        fields = list(rows[0].keys())
        if _COL_VERIFIED not in fields:
            continue
        changed = False
        for r in rows:
            if str(r.get(_COL_NAME) or "").strip() in ok and r.get(_COL_VERIFIED) != args.date:
                r[_COL_VERIFIED] = args.date
                changed = True
                written += 1
        if changed:
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify_source.py",
        description="证据闸门: 真查一次上游, 断言登记的 item_path/total_field/fields 是真的。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("退出码: 0=已用真实数据验证通过 / 1=断言失败或降级 / "
                "2=上游不可达 / 3=缺凭据(需设 A_GATEWAY_* 环境变量)"))
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
    ap.add_argument("--allow-partial-date", action="store_true",
                    help="配合 --write-verified-date: 该源只要没出现 failed/degraded 就写日期"
                         "(容忍 unreachable / credentials_missing)")
    ap.add_argument("--via-gateway", action="store_true",
                    help="--all 时仍走网关链路(默认直连上游; 走网关会因 400ms 预算大面积降级)")
    ap.add_argument("--write-verified-date", action="store_true",
                    help="配合 --recap-json: 把这批复验结果里的日期写回清单 CSV 的『最后验证』列")
    ap.add_argument("--recap-json", default="",
                    help="--write-verified-date 读的汇总 JSON 路径(上一次 --all --json 的输出)")
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    help="要写入的 ISO 日期(默认今天)")
    args = ap.parse_args(argv)

    if args.write_verified_date:
        # 写日期是纯本地后处理, 不需要跑网络, 也不该要求 --all。
        print(f"最后验证写回: {write_verified_dates(args)} 行",
              file=sys.stderr)
        return 0

    if args.all:
        if args.source:
            ap.error("--all 与具体源不能同时给")
        if not args.json:
            print("✗ --all 只支持 --json 输出", file=sys.stderr)
            return 2
        # 走网关时上游预算只有 400ms, 而跨境往返实测 0.4-7.5s —— 于是几乎
        # 每个源都"正常地"降级, 报告变成一份网络延迟报告而不是元数据报告。
        if not args.via_gateway:
            args.direct_only = True
        rc = run_all(args)
        return rc
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
        # 实测: SEC data.sec.gov 拒绝含 github.com 链接的 UA(403),
        # 认 "名字 (+邮箱)" 这种朴素格式。验证脚本的目的是把上游打通,
        # UA 就用最不惹事的形式。
        ua = f"a-source-gateway-verify/1.0 (+{args.contact})"

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
        # 缺凭据 ≠ 元数据错: 这些源的端点定义是对的, 只是本机没配对应的
        # A_GATEWAY_* 环境变量。混进 failed 会让"元数据失真"这个信号失真。
        missing_detail = describe_missing(ep, missing_params)
        env_by_name = {p.get("name"): p.get("env") for p in ep.get("params", []) if p.get("name")}
        missing_env = [n for n in missing_params if env_by_name.get(n)]
        credential_only = bool(missing_env) and len(missing_env) == len(missing_params)
        assertions.add(
            "required_params_available",
            False,
            "warn" if credential_only else "hard",
            f"必填参数取不到值: {', '.join(missing_detail)};"
            f"凭据参数请设对应环境变量或用 --params 补上")
        report = build_report(src, ep, runtime, args, gateway_url, assertions, warnings,
                              errors,
                              status="credentials_missing" if credential_only else "failed",
                              transport=transport, params=used_params,
                              gateway_call=gateway_call)
        report["errors"].append(f"必填参数取不到值: {', '.join(missing_detail)}")
        emit(report, args.json)
        return 3 if credential_only else 1

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
    # 配额/限流状态: HTTP 200 但正文里写着"没给数据"(如 BLS 的 REQUEST_NOT_PROCESSED)。
    # 非空时说明上游活着但配额耗尽 —— 不是元数据写错。正文解析出来后才知道。
    quota_note = ""
    # 结构断言的跳过理由(空串 = 正常断言)。
    structure_skip_reason = ""
    # 通用透传端点(entry)按设计就是"官方入口直取": 正文是 HTML 页面属于预期结果,
    # 不是错误页。对它做 JSON 断言会把几十个只有 entry 端点的源全判成失败,
    # 从而淹没真正需要修的源。这里只对它断言"入口可达且返回了正文"。
    is_generic_entry = ep.get("id") == "entry"
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
            assertions.add(
                "upstream_body_is_json",
                is_generic_entry,
                "warn" if is_generic_entry else "hard",
                "端点 entry 是通用透传(官方入口直取), 正文为 HTML 属预期"
                if is_generic_entry
                else f"上游返回的不是 JSON(疑似 HTML 错误页): {exc}")
            # 正文不是 JSON 时, item_path / total_field / 字段名断言都没有意义
            # (拿错误页去比对路径, 只会误导)。如实记成"因上游非 JSON 跳过"。
            skip_reason = ("端点 entry 为通用透传, 无声明结构可断言"
                           if is_generic_entry else "上游返回的不是 JSON, 已跳过该断言")
            assertions.add("item_path_resolves_to_list", True, "warn", skip_reason)
            assertions.add("total_field_resolves_to_number", True, "warn", skip_reason)
            assertions.add("declared_fields_present", True, "warn", skip_reason)
            assertions.add("data_not_empty", True, "warn", skip_reason)
        if payload is not None:
            data, total, truncated = runtime.project(payload, ep, limit=args.limit)
            quota_note = quota_blocked(payload)
        if upstream_status is not None and upstream_status >= 400:
            assertions.add("upstream_status_ok", False, "hard",
                           f"上游返回 HTTP {upstream_status}: {text[:200]}")
            assertions.add("upstream_not_error", False, "hard",
                           f"上游 HTTP {upstream_status}, 算降级, 不能算'源已跑通'")
        elif body_not_json and not is_generic_entry:
            # 状态码是 200 但正文是 HTML 错误页: 这**不是**"上游正常返回"。
            # 上游状态正常 ≠ 拿到数据 —— 说成"正常返回, 未降级"会重新制造
            # 本轮要消灭的假成功观感。通用透传端点(entry)除外: 它本来就是取页面。
            assertions.add("upstream_status_ok", True, "hard",
                           f"上游 HTTP {upstream_status}, {len(raw_body)}B")
            assertions.add("upstream_not_error", False, "hard",
                           "上游状态码正常, 但正文不是 JSON(疑似错误页), 算降级")
        elif body_not_json and is_generic_entry:
            assertions.add("upstream_status_ok", True, "hard",
                           f"上游 HTTP {upstream_status}, {len(raw_body)}B")
            assertions.add("upstream_not_error", True, "hard",
                           "通用透传端点取到官方入口页面(非结构化数据), 入口可达")
        elif quota_note:
            # HTTP 200 但正文里带着配额/限流状态(如 BLS 的 REQUEST_NOT_PROCESSED +
            # "daily threshold ... reached"): 上游没给数据, 但 item_path 声明本身是对的。
            # 报成 item_path 写错会让人去改一份正确的元数据。
            assertions.add("upstream_status_ok", True, "hard",
                           f"上游 HTTP {upstream_status}, {len(raw_body)}B")
            assertions.add("upstream_not_error", False, "hard",
                           f"上游配额/限流未返回数据: {quota_note}")
            # 结构断言在下面统一处理(结构断言块跑在状态分支之后, 提前 drop 会
            # 被后面重新 add 覆盖掉 —— 那正是这个 bug 的来由)。
            structure_skip_reason = "上游配额受限未返回数据, 结构断言无意义, 已跳过"
        else:
            assertions.add("upstream_status_ok", True, "hard",
                           f"上游 HTTP {upstream_status}, {len(raw_body)}B")
            assertions.add("upstream_not_error", True, "hard", "上游正常返回, 未降级")
        if payload is not None:
            if structure_skip_reason:
                # 配额受限: 结构断言无意义(拿不到数据比不出字段), 统一记成"已跳过"。
                for name in ("item_path_resolves_to_list", "total_field_resolves_to_number",
                             "declared_fields_present", "data_not_empty"):
                    assertions.drop(name)
                    assertions.add(name, True, "warn", structure_skip_reason)
            else:
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
                    assertions.add("data_not_empty", False, "warn",
                                   "取到的是空列表(可能是过滤条件太窄)")
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

    if body_not_json and not is_generic_entry:
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
