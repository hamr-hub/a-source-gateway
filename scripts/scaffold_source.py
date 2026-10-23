#!/usr/bin/env python3
"""
scaffold_source.py - 从侦察报告生成"待审"的源登记草稿。

    python3 scripts/scaffold_source.py --recon recon.json \
        --name "My Journal API" --family "七、学术与文献" \
        --org "My Journal" --country "全球" --summary "期刊论文元数据与引用" \
        --out-dir ./draft

默认只写补丁目录(不碰仓库里的真实数据文件):
    draft/source_inventory.row.csv   一行 CSV(含表头, 列序与真实文件一致)
    draft/endpoints.entry.json       endpoints.json 里 sources 下的一个键
    draft/REPORT.md                  逐字段说明 + 证据 + TODO 清单

加 --apply 才会写回真实文件(会先检查重名, 并先备份)。

分工原则: 侦察能测出来的(screenshot)自动填; 需要判断的(限速口径、增量取法、
合规定级)一律留 TODO 标记, 不编造。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Any

_TODO = "TODO"


def _bootstrap():
    """按路径加载 scripts/_asg_bootstrap.py(不把 scripts/ 做成包)。"""
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

# 认证方式取值必须落在分类器认识的词表里(见 docs/adding-a-source.md)
AUTH_VOCAB = {
    "无key": "none", "需邮箱": "email_param", "免费key": "api_key", "可注册key": "api_key",
    "可选key": "api_key(可选)", "需token": "token", "部分付费": "paid", "账号+付费": "paid",
    "免费/付费": "mixed", "部分免费部分付费": "mixed", "多数免费": "mixed", "部分注册": "mixed",
}

CSV_COLUMNS = [
    "分类", "数据源", "机构", "国别", "数据内容", "官方入口", "认证方式", "官方限速口径",
    "全量下载", "更新频率", "实测规模", "实测增量", "代理需求", "合规风险", "建议采集方式",
    "链接状态", "探测耗时ms",
]

# 分页类参数: 变成 {size} 占位符, 其余查询参数变成必填参数
PAGE_SIZE_NAMES = ("rows", "per-page", "per_page", "perpage", "page_size", "pagesize",
                   "limit", "size", "count", "num")
EMAIL_NAMES = ("mailto", "email", "e-mail", "contact_email")


# ── 小工具 ───────────────────────────────────────────────────────────────
def todo(text: str) -> str:
    """需要人工/agent 判断的字段统一打 TODO 标记, 方便 grep。"""
    return f"{_TODO}(待填:{text})"


def detect_terminator(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def read_raw(path: Path) -> str:
    """按字节读原文 —— read_text 会把 CRLF 悄悄变成 LF, 换行风格就测不出来了。"""
    return path.read_bytes().decode("utf-8")


def read_csv_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    raw = read_raw(path)
    rows = list(csv.reader(io.StringIO(raw, newline="")))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def write_csv_rows(path: Path, header: list[str], rows: list[list[str]], terminator: str) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator=terminator)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    path.write_text(buf.getvalue(), encoding="utf-8", newline="")


# ── 认证方式映射 ─────────────────────────────────────────────────────────
def map_auth(auth: dict[str, Any]) -> tuple[str, str, list[str]]:
    """侦察结论 → (认证方式取值, 依据, TODO 清单)。"""
    todos: list[str] = []
    kind = auth.get("kind")
    required = auth.get("required")
    if kind is None:
        return "无key", "侦察没有拿到认证线索, 暂按匿名可访问", ["认证方式: 必须人工确认"]
    if kind == "none":
        return "无key", auth.get("evidence", ""), []
    if kind == "api_key":
        value = "免费key" if required else "可选key"
        if not required:
            todos.append("认证方式: 侦察显示凭据可选(无凭据也返回 200), 需确认是'可选key'还是完全不需要")
        return value, auth.get("evidence", ""), todos
    if kind == "email":
        return "需邮箱", auth.get("evidence", ""), ["认证方式: 确认是否只是'带邮箱更优'而非强制"]
    if kind == "token":
        return "需token", auth.get("evidence", ""), ["认证方式: 确认凭据的申请方式与是否免费"]
    if kind == "basic":
        return "需token", auth.get("evidence", ""), ["认证方式: 上游是 Basic 认证, 词表里没有对应项, 需人工改写"]
    todos.append("认证方式: 侦察未能判定(上游报错但无认证线索), 必须人工确认后再定")
    return "需token", auth.get("evidence", ""), todos


def map_limits(rate: dict[str, Any]) -> tuple[str, list[str]]:
    """限速口径: 只抄响应头原文, 不把 "limit=1" 脑补成 "1次/秒"。"""
    todos: list[str] = []
    headers = rate.get("headers") or {}
    if not headers:
        return "未公布", ["官方限速口径: 响应头无信号, 需查官方文档后填原文(不要脑补单位)"]
    parts = [f"{k}: {v}" for k, v in headers.items()]
    todos.append("官方限速口径: 上面抄的是响应头原文, 需换成官方公布的原始口径(带单位)")
    return "; ".join(parts), todos


def map_scale(recon: dict[str, Any]) -> tuple[str, list[str]]:
    """实测规模: 只有在 total 是全量口径时才用, 否则写'未实测'。"""
    todos: list[str] = []
    total = recon.get("total_value")
    total_field = recon.get("total_field")
    is_global = recon.get("total_is_global")
    if total is None or not total_field:
        return "未实测", ["实测规模: 未取到总数字段, 需人工给出计数口径(例如 '以 /api/works 的 total 字段计')"]
    if not is_global:
        return ("未实测",
                [f"实测规模: 探测 URL 带过滤参数, 取到的 {total_field}={total} 只是子集, "
                 f"需另取全量口径再填"])
    return f"实测 {total} 条(接口 {total_field} 字段, 探测于 {recon.get('probed_at', '?')})", todos


# ── 端点定义 ─────────────────────────────────────────────────────────────
def clean_entry_url(url: str) -> str:
    """官方入口列: 去掉分页/礼貌池参数, 留可直达的入口(与仓库现有写法一致)。"""
    parts = urllib.parse.urlsplit(url)
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in PAGE_SIZE_NAMES and k.lower() not in EMAIL_NAMES]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment))


def default_endpoint_id(recon: dict[str, Any]) -> str:
    """端点 id: 取探测 URL 路径的最后一段(works / works.json → works)。"""
    path = urllib.parse.urlsplit(recon.get("final_url") or "").path
    seg = [s for s in path.split("/") if s]
    if not seg:
        return "entry"
    last = seg[-1]
    last = re.sub(r"\.(json|xml|csv|tsv)$", "", last, flags=re.I)
    return safe_param_name(last).lower() or "entry"


def safe_param_name(name: str) -> str:
    """占位符名必须是 [a-zA-Z0-9_]+(网关的渲染正则只认这个)。"""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def build_url_template(recon: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    """把探测 URL 变成 url_template + params。

    分页参数 → {size}; 邮箱参数 → {email}; 其余查询参数 → 必填参数(带实测 example)。
    上游的参数名保留不变(只换占位符里的名字), 免得拼出来的 URL 对不上官方接口。
    """
    todos: list[str] = []
    url = recon.get("final_url") or recon.get("url") or ""
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    params: list[dict[str, Any]] = []
    used_size = False
    used_email = False

    for name, value in query.items():
        lname = name.lower()
        if lname in PAGE_SIZE_NAMES and not used_size:
            used_size = True
            params.append({
                "name": "size", "in": "query", "type": "int", "required": False,
                "default": 10, "desc": f"每页条数(上游分页参数 {name})",
            })
            continue
        if lname in EMAIL_NAMES and not used_email:
            used_email = True
            params.append({
                "name": "email", "in": "query", "type": "string", "required": False,
                "desc": "联系邮箱(部分官方 API 用于礼貌池, 网关会用 A_GATEWAY_CONTACT 自动填)",
            })
            continue
        pname = safe_param_name(name)
        if pname != name:
            todos.append(f"params.{name}: 上游参数名含非标识符字符, 占位符写成 {{{pname}}}")
        params.append({
            "name": pname, "in": "query", "type": "string", "required": True,
            "example": value,
            "desc": f"{_TODO}(待补:参数说明; 实测取值来自侦察探测, 上游参数名 {name})",
        })
        todos.append(f"params.{pname}: 必填, 已填实测 example, 但参数含义要人工补")

    # 重建 query string: 键名保持上游原样, 值换成占位符
    kept: list[tuple[str, str]] = []
    for name in query:
        lname = name.lower()
        if lname in PAGE_SIZE_NAMES and used_size:
            kept.append((name, "{size}"))
        elif lname in EMAIL_NAMES and used_email:
            kept.append((name, "{email}"))
        else:
            kept.append((name, "{" + safe_param_name(name) + "}"))
    template = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(kept, safe="{}=&"), parts.fragment))
    return template, params, todos


def build_endpoint(recon: dict[str, Any], args, name: str) -> tuple[dict[str, Any], list[str]]:
    todos: list[str] = []
    template, params, t = build_url_template(recon)
    todos.extend(t)

    style = recon.get("api_style") or "object"
    shape = {"json": "object", "html": "text", "text": "text", "xml": "xml",
             "csv": "csv", "tsv": "tsv"}.get(style, "object")
    if style == "json" and recon.get("item_shape") is None and recon.get("item_path") is None:
        shape = "object"

    fields: list[dict[str, Any]] = []
    field_types = recon.get("field_types") or {}
    for fname in recon.get("fields") or []:
        fields.append({"name": fname, "type": field_types.get(fname, "string")})

    notes_bits = [f"侦察于 {recon.get('probed_at', '?')} 实测: HTTP {recon.get('status')}, "
                  f"{recon.get('latency_ms')}ms, {recon.get('body_bytes')}B"]
    if recon.get("item_path"):
        notes_bits.append(f"item_path={recon['item_path']}({recon.get('inference', {}).get('item_path', '')})")
    if recon.get("total_field"):
        notes_bits.append(f"total_field={recon['total_field']}")

    endpoint = {
        "id": args.endpoint_id,
        "name": args.endpoint_name or default_endpoint_name(recon, args.endpoint_id),
        "method": "GET",
        "url_template": template,
        "params": params,
        "response": {
            "media_type": recon.get("content_type") or "application/json",
            "shape": shape,
            "fields": fields,
            "notes": "; ".join(notes_bits),
        },
        "cache_ttl_s": args.cache_ttl_s,
        "budget_ms": args.budget_ms,
    }
    if recon.get("item_path"):
        endpoint["response"]["item_path"] = recon["item_path"]
    if recon.get("item_shape") and recon["item_shape"] != "list":
        endpoint["response"]["item_shape"] = recon["item_shape"]
    if recon.get("total_field"):
        endpoint["response"]["total_field"] = recon["total_field"]
    if not fields:
        todos.append("response.fields: 侦察没取到字段名, 需人工补齐")
    return endpoint, todos


def default_endpoint_name(recon: dict[str, Any], endpoint_id: str) -> str:
    path = urllib.parse.urlsplit(recon.get("final_url") or "").path
    seg = [s for s in path.split("/") if s]
    return seg[-1] if seg else endpoint_id


# ── 行生成 ───────────────────────────────────────────────────────────────
def build_row(recon: dict[str, Any], args, columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """生成 {列: 值} 与 TODO 清单。"""
    todos: list[str] = []
    auth_value, auth_why, auth_todos = map_auth(recon.get("auth") or {})
    todos.extend(auth_todos)
    limits_value, limit_todos = map_limits(recon.get("rate_limit") or {})
    todos.extend(limit_todos)
    scale_value, scale_todos = map_scale(recon)
    todos.extend(scale_todos)

    row = {
        "分类": args.family,
        "数据源": args.name,
        "机构": args.org,
        "国别": args.country,
        "数据内容": args.summary,
        "官方入口": clean_entry_url(recon.get("final_url") or recon.get("url") or ""),
        "认证方式": auth_value,
        "官方限速口径": limits_value,
        "全量下载": todo("批量文件/导出方式(官网下载页或 dump)"),
        "更新频率": todo("daily/weekly/monthly/quarterly/realtime"),
        "实测规模": scale_value,
        # 实测增量必须非空且不能等于实测规模 —— 这里给的是占位, 必须人工改写
        "实测增量": todo("日增量 + 取法(例: 日增约 N 条, 按 from-created-date)"),
        "代理需求": "兜底",
        "合规风险": todo("人工定级: 低/中/高(查官方条款与 robots.txt 后填)"),
        "建议采集方式": todo("增量策略(按哪个字段/哪个文件取)"),
        "链接状态": str(recon.get("status") or ""),
        "探测耗时ms": str(recon.get("latency_ms") or ""),
    }
    if "优先级" in columns:
        # 国内清单专有列(海外清单没有这列, 不会有这段)
        row["优先级"] = getattr(args, "priority", "") or "P2"
    todos.append("实测增量: 目前是占位文本, 必须换成真实增量口径(不能与实测规模相同)")
    todos.append("代理需求: 默认'兜底'(仓库里绝大多数源的口径), 按合规判断确认")
    todos.append("别名: 别名表在 data/aliases.json(本任务只读不改), 需要另一次改动加简称")
    if "UA" in (recon.get("auth", {}).get("evidence") or "") or "mailto" in str(
            recon.get("query_params", {})):
        todos.append("若上游要求 UA 带联系邮箱, 需在 认证方式/官方限速口径 旁注明(参考 SEC 的写法)")

    for key, val in (args.override or {}).items():
        if key not in row:
            todos.append(f"--override: 未知列名 {key!r}, 已忽略")
            continue
        row[key] = val
    return {c: row.get(c, "") for c in columns}, todos


# ── 报告 ─────────────────────────────────────────────────────────────────
def build_report_md(name: str, recon: dict[str, Any], row: dict[str, str],
                    columns: list[str], endpoint: dict[str, Any],
                    todos: list[str], evidence: dict[str, str],
                    out_dir: Path, applied: bool, family: str = "") -> str:
    lines: list[str] = []
    lines.append(f"# 源登记草稿: {name}")
    lines.append("")
    lines.append(f"- 探测 URL: `{recon.get('url')}`")
    lines.append(f"- 最终 URL: `{recon.get('final_url')}`")
    lines.append(f"- 探测时间: {recon.get('probed_at')}")
    lines.append(f"- 实测状态: HTTP {recon.get('status')} / {recon.get('latency_ms')}ms / "
                 f"{recon.get('body_bytes')}B / `{recon.get('content_type')}`")
    lines.append(f"- 侦察报告: `{recon.get('_source_path', '(内联)')}`")
    lines.append(f"- 补丁目录: `{out_dir}`")
    lines.append("")
    lines.append("## 1. CSV 行逐列说明")
    lines.append("")
    lines.append("| 列 | 生成值 | 依据 | 性质 |")
    lines.append("|---|---|---|---|")
    for col in columns:
        val = (row.get(col) or "").replace("|", "\\|")
        why = evidence.get(col, "")
        kind = "**TODO**" if val.startswith(_TODO) else (
            "实测" if col in ("官方入口", "链接状态", "探测耗时ms", "实测规模") else "推导")
        lines.append(f"| {col} | {val} | {why} | {kind} |")
    lines.append("")
    lines.append("## 2. 端点定义")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps({name: [endpoint]}, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 3. TODO 清单(必须人工/agent 补齐, 不要直接用占位值上线)")
    lines.append("")
    for i, t in enumerate(todos, 1):
        lines.append(f"{i}. {t}")
    lines.append("")
    lines.append("## 4. 如何应用")
    lines.append("")
    lines.append("```bash")
    lines.append("# 1) 先把 data/ 拷到临时目录, 在副本上验证")
    lines.append("cp -r data /tmp/asg-data-check")
    lines.append("")
    lines.append("# 2) 在副本上跑官方校验")
    lines.append("A_GATEWAY_DATA_DIR=/tmp/asg-data-check python3 -m a_source_gateway.cli validate")
    lines.append("")
    lines.append("# 3) 真要落库时(会先查重名, 并把备份写进补丁目录):")
    lines.append(f"python3 scripts/scaffold_source.py --recon <recon.json> --name {name!r} \\")
    lines.append(f"    --family {family!r} --org '...' --country '...' --summary '...' --apply")
    lines.append("```")
    lines.append("")
    if applied:
        lines.append("> 本次已写回真实数据文件(备份见补丁目录下的 *.bak)。")
    else:
        lines.append("> 本次只生成了补丁, 真实数据文件未被修改。")
    lines.append("")
    lines.append("## 5. 诚实性声明")
    lines.append("")
    lines.append("- 带 **TODO** 的字段是占位, 不是结论;")
    lines.append("- 标为“实测”的字段来自侦察报告里的真实 HTTP 响应;")
    lines.append("- 标为“推导”的字段由侦察报告的 `inference` 字段给出推理依据。")
    lines.append("")
    return "\n".join(lines)



# ── 应用到真实文件 ───────────────────────────────────────────────────────
def apply_to_data(data_dir: Path, name: str, row: dict[str, str], columns: list[str],
                  endpoint: dict[str, Any], out_dir: Path,
                  csv_path: Path | None = None,
                  write_endpoints: bool = True) -> list[str]:
    """写回真实数据文件。先查重名, 再备份, 最后写。任一步失败都不写半个文件。

    csv_path: 目标清单文件(国内源 → source_inventory_cn.csv);None = 海外清单。
    write_endpoints: 国内源只入候选池, 不写 endpoints.json
                     (国内源要接网关, 得先有显式端点定义 + 别名)。
    """
    csv_path = csv_path or (data_dir / "source_inventory.csv")
    json_path = data_dir / "endpoints.json"
    problems: list[str] = []

    if not csv_path.is_file():
        raise FileNotFoundError(f"数据文件不存在: {csv_path}")
    if not json_path.is_file():
        raise FileNotFoundError(f"数据文件不存在: {json_path}")

    raw = read_raw(csv_path)
    header, rows = read_csv_rows(csv_path)
    if list(header) != list(columns):
        problems.append(f"CSV 表头与预期不一致: {header}")
    if any((r and r[1] == name) for r in rows if len(r) > 1):
        problems.append(f"数据源名已存在: {name}")

    # 跨清单查重: 同一个源只能属于一份清单(规则 3.1), 两边都要看
    for other in (data_dir / "source_inventory.csv", data_dir / "source_inventory_cn.csv"):
        if not other.is_file() or other == csv_path:
            continue
        _h, other_rows = read_csv_rows(other)
        if any((r and r[1] == name) for r in other_rows if len(r) > 1):
            problems.append(f"数据源名已存在于另一份清单({other.name}): {name}")

    doc = json.loads(json_path.read_text(encoding="utf-8"))
    if name in (doc.get("sources") or {}):
        problems.append(f"endpoints.json 里已有同名源: {name}")
    if problems:
        return problems

    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(csv_path, out_dir / f"{csv_path.name}.bak")
    if write_endpoints:
        shutil.copy2(json_path, out_dir / "endpoints.json.bak")

    terminator = detect_terminator(raw)
    write_csv_rows(csv_path, header, rows + [[row.get(c, "") for c in header]], terminator)

    if write_endpoints:
        doc.setdefault("sources", {})[name] = [endpoint]
        text = json.dumps(doc, ensure_ascii=False, indent=2)
        if read_raw(json_path).endswith("\n"):
            text += "\n"
        json_path.write_text(text, encoding="utf-8")
    return []


# ── CLI ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="scaffold_source.py",
        description="从侦察报告生成源登记草稿(CSV 行 + endpoints 条目 + 说明报告)。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例:\n"
               "  python3 scripts/scaffold_source.py --recon recon.json \\\n"
               "      --name 'My Journal API' --family '七、学术与文献' --org 'My Journal' \\\n"
               "      --country '全球' --summary '期刊论文元数据与引用' --out-dir ./draft")
    ap.add_argument("--recon", required=True, help="侦察报告 JSON(recon.py 产出)")
    ap.add_argument("--name", required=True, help="数据源名称(会成为 endpoints.json 的键)")
    ap.add_argument("--family", required=True, help="分类, 如 '七、学术与文献'")
    ap.add_argument("--org", default="", help="发布机构")
    ap.add_argument("--country", default="", help="国别/覆盖范围(写『中国』则落国内清单)")
    ap.add_argument("--summary", default="", help="一句话数据内容摘要")
    ap.add_argument("--priority", default="P2", choices=["P0", "P1", "P2"],
                    help="国内清单的优先级列(仅国内清单有此列, 默认 P2)")
    ap.add_argument("--out-dir", default="./draft", help="补丁输出目录(默认 ./draft)")
    ap.add_argument("--data-dir", default="", help="数据文件目录(默认 <repo>/data)")
    ap.add_argument("--apply", action="store_true", help="写回真实数据文件(默认只写补丁)")
    ap.add_argument("--endpoint-id", default="", help="端点 id(默认取 URL 路径最后一段)")
    ap.add_argument("--endpoint-name", default="", help="端点显示名")
    ap.add_argument("--cache-ttl-s", type=int, default=900, help="缓存秒数(默认 900)")
    ap.add_argument("--budget-ms", type=int, default=400, help="上游预算毫秒(默认 400)")
    ap.add_argument("--override", action="append", default=[],
                    help="覆盖某一列的值, 形如 --override '官方限速口径=50次/秒'(可重复)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    overrides: dict[str, str] = {}
    for item in args.override:
        if "=" not in item:
            print(f"✗ --override 格式应为 '列=值': {item!r}", file=sys.stderr)
            return 2
        key, val = item.split("=", 1)
        overrides[key.strip()] = val
    args.override = overrides

    recon_path = Path(args.recon)
    if not recon_path.is_file():
        print(f"✗ 找不到侦察报告: {recon_path}", file=sys.stderr)
        return 2
    recon = json.loads(recon_path.read_text(encoding="utf-8"))
    recon["_source_path"] = str(recon_path)

    data_dir = Path(args.data_dir) if args.data_dir else _bs.default_data_dir()
    # 规则 3.1:国别决定落哪份清单 —— 判定与列契约都取自网关包(唯一事实源, 不在脚本里另抄一份)
    from a_source_gateway.registry import (
        CN_EXTRA_COLUMNS,
        INVENTORY_COLUMNS,
        is_cn_country,
    )
    is_cn = is_cn_country(args.country)
    csv_path = data_dir / ("source_inventory_cn.csv" if is_cn else "source_inventory.csv")
    expected_columns = INVENTORY_COLUMNS + (CN_EXTRA_COLUMNS if is_cn else [])
    if csv_path.is_file():
        header, _rows = read_csv_rows(csv_path)
        columns = header or list(expected_columns)
    else:
        columns = list(expected_columns)

    row, todos = build_row(recon, args, columns)
    if not args.endpoint_id:
        args.endpoint_id = default_endpoint_id(recon)
    endpoint, ep_todos = build_endpoint(recon, args, args.name)
    todos.extend(ep_todos)

    # 硬约束自查: 认证方式词表 / 实测增量非空且不等于实测规模 / {param} 都已声明
    problems: list[str] = []
    auth_value = row.get("认证方式", "")
    if auth_value not in AUTH_VOCAB:
        problems.append(f"认证方式 {auth_value!r} 不在分类器词表里")
    inc = (row.get("实测增量") or "").strip()
    if not inc:
        problems.append("实测增量为空")
    if inc == (row.get("实测规模") or "").strip():
        problems.append("实测增量等于实测规模")
    declared = {p["name"] for p in endpoint.get("params", [])}
    for ph in set(re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint["url_template"])):
        if ph not in declared:
            problems.append(f"url_template 用了 {{{ph}}} 但 params 里没声明")
    if not endpoint.get("url_template"):
        problems.append("端点缺 url_template")
    if not endpoint.get("response"):
        problems.append("端点缺 response")
    for p in endpoint.get("params", []):
        if p.get("required") and not p.get("example") and "default" not in p:
            problems.append(f"必填参数 {p.get('name')} 没有 example/default")

    evidence = build_evidence(recon, args)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    terminator = detect_terminator(read_raw(csv_path)) if csv_path.is_file() else "\n"
    write_csv_rows(out_dir / "source_inventory.row.csv", columns, [[row.get(c, "") for c in columns]],
                   terminator)
    (out_dir / "endpoints.entry.json").write_text(
        json.dumps({args.name: [endpoint]}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "REPORT.md").write_text(
        build_report_md(args.name, recon, row, columns, endpoint, todos, evidence, out_dir, False,
                       args.family),
        encoding="utf-8")

    if args.apply:
        problems_apply = apply_to_data(data_dir, args.name, row, columns, endpoint, out_dir,
                                        csv_path=csv_path, write_endpoints=not is_cn)
        if problems_apply:
            print("✗ 拒绝写入:", file=sys.stderr)
            for p in problems_apply:
                print("  -", p, file=sys.stderr)
            return 1
        (out_dir / "REPORT.md").write_text(
            build_report_md(args.name, recon, row, columns, endpoint, todos, evidence, out_dir, True,
                       args.family),
            encoding="utf-8")

    print(f"补丁已写出: {out_dir}/")
    print(f"  - source_inventory.row.csv  ({len(columns)} 列)")
    print(f"  - endpoints.entry.json      (源名 {args.name!r})")
    print("  - REPORT.md                 (逐字段说明 + TODO)")
    print()
    print("自动填好的(有实测依据):")
    for col in columns:
        val = row.get(col) or ""
        if not val.startswith(_TODO):
            print(f"  {col:<12} = {val}")
    print()
    print(f"必须人工/agent 补齐的 TODO({len(todos)} 项):")
    for i, t in enumerate(todos, 1):
        print(f"  {i}. {t}")
    if args.apply:
        if is_cn:
            print(f"\n✓ 已写入国内候选清单 {csv_path}(备份在 {out_dir}/{csv_path.name}.bak)")
            print("  国内源只入候选池:本次未写 endpoints.json,网关运行时尚未加载该源。")
            print("  接入网关还需另外完成:端点定义(endpoints.json) + ASCII 别名(aliases.json)。")
            print("  运行时已按『海外清单 + 国内清单』双加载,不需要再改代码。")
        else:
            print(f"\n✓ 已写回 {data_dir}(备份在 {out_dir}/*.bak)")
    else:
        print("\n未修改真实数据文件。需要落库时加 --apply。")
    if problems:
        print("\n✗ 自查发现问题:", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 1
    return 0


def build_evidence(recon: dict[str, Any], args) -> dict[str, str]:
    inf = recon.get("inference") or {}
    ev = {
        "分类": "由使用者按十大分类指定",
        "数据源": "由使用者指定(同时是 endpoints.json 的键)",
        "机构": "由使用者指定",
        "国别": "由使用者指定",
        "数据内容": "由使用者指定的摘要",
        "官方入口": f"侦察实测: {recon.get('final_url')} (HTTP {recon.get('status')}), 已去掉分页参数",
        "认证方式": f"侦察判定: {inf.get('auth', '')}",
        "官方限速口径": f"侦察抄到的响应头原文: {inf.get('rate_limit', '')}",
        "全量下载": "需人工查官网下载页",
        "更新频率": "需人工确认",
        "实测规模": f"侦察实测: {inf.get('total_value', inf.get('total_field', '未取到'))}",
        "实测增量": "需人工按增量口径实测(单次侦察测不出增量)",
        "代理需求": "仓库默认口径'兜底'",
        "合规风险": "需人工查官方条款与 robots.txt 后定级",
        "建议采集方式": "需人工定",
        "链接状态": f"侦察实测 HTTP 状态码: {recon.get('status')}",
        "探测耗时ms": f"侦察实测耗时: {recon.get('latency_ms')}ms",
    }
    return ev


if __name__ == "__main__":
    sys.exit(main())
