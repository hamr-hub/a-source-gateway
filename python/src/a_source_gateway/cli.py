"""
cli.py - 统一命令行入口

    a-source-gateway serve            # 起 HTTP API
    a-source-gateway mcp              # 起 MCP server (stdio)
    a-source-gateway list             # 列数据源
    a-source-gateway show <源>        # 看某个源的完整 meta
    a-source-gateway build-registry   # 导出 registry.json
    a-source-gateway bench            # 跑延迟基准
    a-source-gateway validate         # 校验 data/ 下的数据文件
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .config import CN_INVENTORY_CSV, INVENTORY_CSV, load_config
from .registry import (
    CN_EXTRA_COLUMNS,
    CN_PRIORITY_VALUES,
    INVENTORY_COLUMNS,
    build_registry,
    is_cn_country,
    load_aliases,
    load_cn_inventory_table,
    load_endpoint_defs,
    load_inventory,
    stable_slug,
)

REQUIRED_COLUMNS = [
    "分类", "数据源", "机构", "国别", "数据内容", "官方入口", "认证方式", "官方限速口径",
    "全量下载", "更新频率", "实测规模", "实测增量", "代理需求", "合规风险", "建议采集方式",
]

# 参数 example 里的占位符: 这类值会被渲染进真实请求, 上游回一个 200 的
# "Invalid Key" 页 —— 状态码是 200, 于是变成一次"成功的空结果"。
_PLACEHOLDER_EXAMPLE = re.compile(
    r"^(YOUR_[A-Z0-9_]+|CHANGEME|<[^>]+>|xxx+|PLACEHOLDER|TODO.*)$", re.I)

# scaffold_source.py 生成的占位符标记。出现在数据文件里 = 结论还没补齐, 不能入库。
TODO_MARK = "TODO("


def _registry() -> dict[str, Any]:
    return build_registry(load_config())


def cmd_list(args) -> int:
    reg = _registry()
    rows = reg["sources"]
    if args.family:
        rows = [r for r in rows if args.family in r["family"]]
    if args.q:
        q = args.q.lower()
        rows = [r for r in rows if any(q in str(r.get(k, "")).lower()
                                       for k in ("name", "summary", "org", "country"))]
    print(f"{'id':<34}{'名称':<34}{'族':<14}{'规模':>14}  端点")
    print("-" * 104)
    for r in rows[: args.limit]:
        total = r["scale"].get("total")
        print(f"{r['id']:<34}{r['name'][:32]:<34}{r['family'][:12]:<14}"
              f"{(f'{total:,}' if total else '-'):>14}  {len(r['endpoints'])}")
    print(f"\n共 {len(rows)} 条(总计 {reg['count']} 源 / {len(reg['families'])} 族)")
    return 0


def cmd_show(args) -> int:
    reg = _registry()
    from .resolver import Resolver
    r = Resolver(reg)
    sid = r.resolve_source(args.source)
    if not sid:
        print(f"未知数据源: {args.source}", file=sys.stderr)
        return 2
    src = reg["sources"][[s["id"] for s in reg["sources"]].index(sid)]
    if args.json:
        print(json.dumps(src, ensure_ascii=False, indent=2))
        return 0
    print(f"{src['name']}  ({src['id']})")
    print(f"  机构/国别 : {src['org']} / {src['country']}")
    print(f"  分类      : {src['family']}")
    print(f"  内容      : {src['summary']}")
    print(f"  官方入口  : {src['homepage']}   [链接状态 {src['link_status']}]")
    print(f"  认证      : {src['auth']}")
    print(f"  限速      : {src['limits']}")
    print(f"  规模      : {src['scale'].get('total')} {src['scale'].get('unit','')}"
          f"  ({src['scale'].get('basis','')})")
    print(f"  增量      : [{src['increment'].get('frequency','')}] {src['increment'].get('daily','')}")
    print(f"  代理策略  : {src['proxy_policy']}   合规风险: {src['risk']}")
    if src.get("aliases"):
        print(f"  别名      : {', '.join(src['aliases'])}")
    print("  端点:")
    for e in src["endpoints"]:
        resp = e["response"]
        bits = []
        if resp.get("item_path"):
            bits.append(f"item_path={resp['item_path']}")
        if resp.get("total_field"):
            bits.append(f"total={resp['total_field']}")
        if resp.get("fields"):
            bits.append(f"fields={len(resp['fields'])}")
        print(f"    - {e['id']:<24}{e['method']}  {e['url_template'][:74]}")
        if bits:
            print(f"      {' | '.join(bits)}")
    return 0


def cmd_build_registry(args) -> int:
    reg = _registry()
    out = Path(args.out) if args.out else Path("registry.json")
    out.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {out} ({out.stat().st_size / 1024:.1f} KiB) | "
          f"{reg['count']} 源 / {sum(len(s['endpoints']) for s in reg['sources'])} 端点")
    return 0


def _check_rows(header: list[str] | None, rows: list[dict[str, Any]], label: str,
                expected_columns: list[str]) -> list[str]:
    """单份清单的契约检查(表头列序/入口 URL/增量/代理/认证方式/占位符)。"""
    from a_source_gateway.registry import AUTH_RULES, classify_auth

    problems: list[str] = []
    if not rows:
        return [f"{label} 为空"]
    if header is not None and header != expected_columns:
        problems.append(f"{label} 表头与契约不一致 -> {header}(应为 {expected_columns})")
    for col in REQUIRED_COLUMNS:
        if col not in rows[0]:
            problems.append(f"{label} 缺列: {col}")
    names = [r.get("数据源", "") for r in rows]
    dup = {n for n in names if n and names.count(n) > 1}
    if dup:
        problems.append(f"{label} 有重复数据源名: {sorted(dup)}")
    known = {"无", "无key", "需邮箱", "免费key", "可选key", "需token"}
    for r in rows:
        name = r.get("数据源", "")
        if not (r.get("官方入口") or "").startswith("http"):
            problems.append(f"{name}: 官方入口不是 URL -> {r.get('官方入口')!r}")
        if not (r.get("实测增量") or "").strip():
            problems.append(f"{name}: 实测增量为空")
        if not (r.get("代理需求") or "").strip():
            problems.append(f"{name}: 代理需求为空")
        if not (r.get("认证方式") or "").strip():
            problems.append(f"{name}: 认证方式为空")
        else:
            raw = r["认证方式"].strip()
            hit = any(n.lower() in raw.lower() for n, _, _ in AUTH_RULES) or raw in known
            if not hit:
                problems.append(f"{name}: 认证方式取值无法识别 -> {raw!r}")
            else:
                typ, opt = classify_auth(raw)
                if typ == "none" and ("key" in raw.lower()) and "无key" not in raw.lower() \
                        and "可选" not in raw and "可注册" not in raw:
                    problems.append(f"{name}: 认证方式分类可疑 -> {raw!r}")
        # 占位符不是结论: scaffold_source.py 生成的 TODO(...) 必须被人补齐后才能入库
        for col, val in r.items():
            if col and isinstance(val, str) and TODO_MARK in val:
                problems.append(f"{name}: {col} 仍是占位符 -> {val[:60]!r}")
    return problems


def _check_csv_line_endings(cfg) -> list[str]:
    """data/*.csv 用 CRLF —— 这是契约, 不是格式偏好。

    `.gitattributes` 把 `data/*.csv` 标成 `-text`(二进制), 就是为了让 git 不去
    规范化行尾:`scaffold_source.py --apply` 承诺"老行一个字节都没动(含 CRLF 与
    引号)", 测试也按 CRLF 断言。

    行尾被某个工具悄悄改成 LF 时, 整个文件会变成一份**整文件**的 diff
    (108 行全删全增), 混在真实改动里几乎看不出来 —— 本轮它已经这样发生了两次,
    每次都要靠人眼发现。这里把它变成一条明确的报错。
    """
    problems: list[str] = []
    for name in (INVENTORY_CSV, CN_INVENTORY_CSV):
        try:
            raw = cfg.find_data(name).read_bytes()
        except (OSError, RuntimeError) as exc:
            problems.append(f"{name}: 读不到 -> {exc}")
            continue
        # 去掉所有 CRLF 之后还剩 \n, 说明有"裸 LF"(整文件 LF 或 CRLF/LF 混用)
        if b"\n" in raw.replace(b"\r\n", b""):
            problems.append(
                f"{name}: 行尾不是纯 CRLF(有裸 LF) —— data/*.csv 的 CRLF 是契约"
                f"(见 .gitattributes 的 -text 标注);"
                f"整文件行尾翻转会让 diff 变成 {raw.count(10)} 行的假改动")
    return problems


def cmd_validate(args) -> int:
    cfg = load_config()
    problems: list[str] = []

    # 0) 行尾契约(CRLF): 早于一切内容检查 —— 行尾错了, 后面的 diff 都不可信
    problems.extend(_check_csv_line_endings(cfg))

    # 1) 两份清单的列契约(规则 3.1:海外运行时清单 + 国内清单)
    rows = load_inventory(cfg)
    problems.extend(_check_rows(INVENTORY_COLUMNS, rows, "source_inventory.csv",
                                INVENTORY_COLUMNS))
    runtime_names = {r.get("数据源", "") for r in rows if r.get("数据源")}

    cn_rows: list[dict[str, Any]] = []
    cn_header: list[str] | None = None
    cn_header, cn_rows = load_cn_inventory_table(cfg)
    problems.extend(_check_rows(cn_header, cn_rows, CN_INVENTORY_CSV,
                                INVENTORY_COLUMNS + CN_EXTRA_COLUMNS))
    # 优先级列取值必须是 P0/P1/P2(国内清单专有列)
    for r in cn_rows:
        pr = (r.get("优先级") or "").strip()
        if pr not in CN_PRIORITY_VALUES:
            problems.append(f"{r.get('数据源')}: 优先级只能是 {'/'.join(CN_PRIORITY_VALUES)}"
                            f" -> {pr!r}")
    cn_names = {r.get("数据源", "") for r in cn_rows if r.get("数据源")}

    # 国别归属:一行只属于一份清单(国内源不要抄进海外清单)
    for n in sorted(runtime_names & cn_names):
        problems.append(f"{n}: 同时出现在 source_inventory.csv 与 source_inventory_cn.csv"
                        f"(同一源只能属于一份清单)")
    for r in cn_rows:
        if not is_cn_country(r.get("国别", "")):
            problems.append(f"{r.get('数据源')}: 在国内清单里但国别是 {r.get('国别')!r}"
                            f" —— 应落 source_inventory.csv")
    for r in rows:
        if is_cn_country(r.get("国别", "")):
            problems.append(f"{r.get('数据源')}: 在海外清单里但国别是 {r.get('国别')!r}"
                            f" —— 应落 source_inventory_cn.csv")

    names = runtime_names | cn_names

    # 2) aliases.json: 键必须精确等于 CSV 的「数据源」原文, 且别名不得跨源冲突
    aliases = load_aliases(cfg)
    for src_name, alias_list in aliases.items():
        if src_name not in names:
            problems.append(f"aliases.json 的键 {src_name!r} 在两份 CSV 里都找不到对应行"
                            f"(别名不会生效)")
        for alias in alias_list:
            norm = re.sub(r"[^a-z0-9]+", "", alias.lower())
            if not norm:
                continue
            for other, other_list in aliases.items():
                if other == src_name:
                    continue
                if norm in {re.sub(r"[^a-z0-9]+", "", a.lower()) for a in other_list}:
                    problems.append(f"别名 {alias!r} 同时被 {src_name!r} 与 {other!r} 占用"
                                    f"(解析会有歧义)")

    # 3) endpoints.json
    defs = load_endpoint_defs(cfg)
    for name, eps in defs.items():
        if name not in names:
            problems.append(f"endpoints.json 的 {name} 在两份 CSV 里都找不到对应行")
        # 已接入的国内源必须有稳定的 id(ASCII 名字或 ASCII 别名), 否则 id 会随清单增删而平移
        if name in cn_names and name not in runtime_names and stable_slug(name, aliases.get(name)) is None:
            problems.append(f"{name}: 已接入的国内源必须在 aliases.json 里备一个 ASCII 别名"
                            f"(否则源 id 会随清单顺序漂移)")
        for e in eps:
            if not e.get("url_template"):
                problems.append(f"{name}/{e.get('id')}: 缺 url_template")
            if not e.get("response"):
                problems.append(f"{name}/{e.get('id')}: 缺 response 结构")
            elif e["response"].get("shape") not in ("object", "array", "csv", "tsv", "xml", "jsonl", "text"):
                problems.append(f"{name}/{e.get('id')}: 未知 shape {e['response'].get('shape')!r}")
            for prm in e.get("params", []):
                has_value = bool(prm.get("example")) or "default" in prm
                if prm.get("required") and not has_value and not prm.get("env"):
                    # 凭据参数(env)豁免: 值只来自环境变量或调用方, 数据文件里
                    # 不该出现真实 key, 也不该用占位符冒充已配好。
                    problems.append(f"{name}/{e.get('id')}: 必填参数 {prm.get('name')} 没有 example/default"
                                    f"(凭据参数请声明 env)")
                ex = prm.get("example")
                if isinstance(ex, str) and _PLACEHOLDER_EXAMPLE.match(ex):
                    problems.append(f"{name}/{e.get('id')}: 参数 {prm.get('name')} 的 example "
                                    f"是占位符 {ex!r}(会被当成真值发给上游)")
            if "{size}" in e.get("url_template", "") and "size" not in \
                    {p.get("name") for p in e.get("params", [])}:
                problems.append(f"{name}/{e.get('id')}: url 用了 {{size}} 但没声明 size 参数")

    reg = build_registry(cfg)
    if problems:
        print(f"校验失败, {len(problems)} 个问题:")
        for p in problems:
            print("  -", p)
        return 1
    cn_note = ""
    if cn_rows:
        onboarded = [n for n in cn_names if n in defs]
        cn_note = (f" / 国内清单 {len(cn_rows)} 源"
                   f"(已接入 {len(onboarded)} / 候选 {len(cn_rows) - len(onboarded)})")
    print(f"校验通过: 海外 {len(rows)} 源{cn_note} / {len(defs)} 源有专用端点 / "
          f"{sum(len(v) for v in defs.values())} 端点定义")
    print(f"注册表构建: {reg['count']} 源 / {sum(len(s['endpoints']) for s in reg['sources'])} 端点")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    cfg = load_config()
    print(f"[a-source-gateway] http://{cfg.host}:{cfg.port}  "
          f"budget={cfg.budget_ms}ms  contact={cfg.contact or '(未设置)'}")
    uvicorn.run("a_source_gateway.api:app", host=cfg.host, port=cfg.port,
                log_level="warning", workers=cfg.workers if cfg.workers > 1 else None)
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import main as mcp_main
    asyncio.run(mcp_main())
    return 0


def cmd_bench(args) -> int:
    from .bench import run_bench
    summary = run_bench(url=args.url or "", rounds=args.rounds,
                        cold_rounds=args.cold_rounds, out=Path(args.out),
                        contact=os.environ.get("A_GATEWAY_CONTACT", ""))
    from .bench import print_summary
    print_summary(summary)
    print(f"报告已写入 {args.out}")
    return 0 if summary.get("pass") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="a-source-gateway",
        description="海外 A 类(法规强制公开)数据源的通用元数据与实时查询网关。"
                    "不存储业务数据, 单次调用硬预算 ≤500ms。")
    ap.add_argument("--version", action="store_true", help="打印版本")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="启动 HTTP API")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp", help="启动 MCP server (stdio)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("list", help="列出数据源")
    p.add_argument("--family", default="")
    p.add_argument("--q", default="")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看某个源的完整 meta")
    p.add_argument("source")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("build-registry", help="导出 registry.json")
    p.add_argument("--out", default="registry.json")
    p.set_defaults(func=cmd_build_registry)

    p = sub.add_parser("validate", help="校验数据文件")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("bench", help="跑延迟基准")
    p.add_argument("--url", default="", help="测已在跑的服务; 留空则自己拉起")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--cold-rounds", type=int, default=2,
                   help="冷路径轮数(每轮强制 cache miss)")
    p.add_argument("--out", default="bench-report.json")
    p.set_defaults(func=cmd_bench)

    args = ap.parse_args(argv)
    if args.version:
        from . import __version__
        print(__version__)
        return 0
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
