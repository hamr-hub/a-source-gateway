"""
_asg_mirror.py - 网关运行时语义的镜像实现(只用标准库)。

什么时候用得上:
  - 网关包装不上(缺 httpx / 没装包)时, verify_source.py 与 demo.py 仍要能跑;
  - 拿它当"规范的可执行副本" —— 下面的 resolve_source / render_url / project /
    parse_body 逐行对照 src/registry.py 与 src/resolver.py 抄的, 语义一致。

网关包可用时, GatewayRuntime 优先用网关自己的实现, 镜像只是兜底。
报告里的 resolver_backend 字段会写明实际用的是哪一套。
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

JSON_LIMIT_BYTES = 4 * 1024 * 1024
MAX_PARSE_ROWS = 15000
DEFAULT_PAGE_SIZE = 25
PAGE_SIZE_PARAMS = {"size", "rows", "per_page", "per-page", "page_size", "pageSize"}

C_FAMILY, C_NAME, C_ORG, C_COUNTRY = "分类", "数据源", "机构", "国别"
C_CONTENT, C_ENTRY = "数据内容", "官方入口"
C_AUTH, C_LIMITS, C_BULK, C_FREQ = "认证方式", "官方限速口径", "全量下载", "更新频率"
C_SCALE, C_INCREMENT, C_PROXY = "实测规模", "实测增量", "代理需求"
C_RISK, C_CRAWL = "合规风险", "建议采集方式"
C_LINK, C_LINK_MS = "链接状态", "探测耗时ms"

CSV_COLUMNS = [
    C_FAMILY, C_NAME, C_ORG, C_COUNTRY, C_CONTENT, C_ENTRY, C_AUTH, C_LIMITS,
    C_BULK, C_FREQ, C_SCALE, C_INCREMENT, C_PROXY, C_RISK, C_CRAWL, C_LINK, C_LINK_MS,
]

UNIT_MAP = [("取值", "values"), ("张表", "tables"), ("表", "tables"),
             ("份", "filings"), ("项", "items"), ("篇", "papers"), ("条", "records"),
             ("家", "entities"), ("个", "records"), ("种", "types"), ("笔", "transactions"),
             ("件", "items"), ("国", "countries"), ("位", "people")]
COUNT_UNITS = "份条项个家种笔件国位机构标目页版款座套"

AUTH_RULES: list[tuple[str, str, bool]] = [
    ("无key", "none", False), ("无 key", "none", False), ("无需", "none", False),
    ("需邮箱", "email_param", False), ("邮箱", "email_param", False),
    ("可选key", "api_key", True), ("可注册key", "api_key", False),
    ("免费key", "api_key", False), ("api key", "api_key", False),
    ("需token", "token", False), ("token", "oauth2", False), ("oauth", "oauth2", False),
    ("部分付费", "paid", False), ("需付费", "paid", False), ("账号+付费", "paid", False),
    ("免费/付费", "mixed", False), ("部分免费部分付费", "mixed", False),
    ("多数免费", "mixed", False), ("部分注册", "mixed", False),
]


# ── 解析辅助(与 src/registry.py 一致) ───────────────────────────────────
def classify_auth(text: str) -> tuple[str, bool]:
    t = (text or "").strip().lower()
    if not t or t in ("无", "none", "-"):
        return "none", False
    if "可选" in t and "key" in t:
        return "api_key", True
    for needle, typ, opt in AUTH_RULES:
        if needle.lower() in t:
            return typ, opt
    return "none", False


def slugify(name: str, idx: int, aliases: list[str] | None = None) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if len(s) >= 3:
        return s
    for a in (aliases or []):
        a = a.strip().lower()
        if re.fullmatch(r"[a-z0-9_]{3,}", a):
            return a
    return f"src_{idx:03d}"


def parse_total(text: str) -> int | None:
    if not text:
        return None
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    seg = text.split("实测", 1)[-1] if "实测" in text else text
    cands: list[int] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|亿|千)?\s*(?=[" + COUNT_UNITS + r"])", seg):
        mult = {"万": 10_000, "亿": 100_000_000, "千": 1_000}.get(m.group(2) or "", 1)
        n = int(float(m.group(1)) * mult)
        if n >= 100:
            cands.append(n)
    if not cands:
        for m in re.finditer(r"(\d{4,})", seg):
            cands.append(int(m.group(1)))
    return max(cands) if cands else None


def parse_unit(text: str) -> str:
    for kw, unit in UNIT_MAP:
        if kw in text:
            return unit
    return ""


# ── 注册表 ───────────────────────────────────────────────────────────────
def _load_aliases() -> dict[str, list[str]]:
    """别名表: 新版在 data/aliases.json(registry.load_aliases), 旧版是 Python 字典。"""
    try:
        from a_source_gateway.registry import load_aliases  # noqa: PLC0415
        return dict(load_aliases())
    except Exception:
        pass
    try:
        from a_source_gateway.registry import ALIASES  # noqa: PLC0415
        return dict(ALIASES)
    except Exception:
        return {}


def _find_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "data", here.parent.parent / "data", Path.cwd() / "data"):
        if cand.is_dir():
            return cand
    raise FileNotFoundError("找不到 data/ 目录")


def load_registry(data_dir: str | Path | None = None) -> dict[str, Any]:
    """构建与 src/registry.py build_registry 同构的注册表。"""
    ddir = Path(data_dir).resolve() if data_dir else None
    try:
        from a_source_gateway.config import load_config  # noqa: PLC0415
        from a_source_gateway.registry import build_registry  # noqa: PLC0415
        cfg = load_config()
        if ddir is not None:
            cfg.data_dir = ddir
        return build_registry(cfg)
    except Exception:
        pass
    return _load_registry_local(ddir)


def _load_registry_local(ddir: Path | None) -> dict[str, Any]:
    root = ddir or _find_data_dir(None)
    with (root / "source_inventory.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    doc = json.loads((root / "endpoints.json").read_text(encoding="utf-8"))
    defs = doc.get("sources", {})
    aliases_map = _load_aliases()

    # 国内清单(规则 3.1):只有已有端点定义的国内源进运行时, 候选源不进
    cn_path = root / "source_inventory_cn.csv"
    if cn_path.is_file():
        with cn_path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                name = (r.get(C_NAME) or "").strip()
                if name and name in defs and name not in {x.get(C_NAME) for x in rows}:
                    rows = rows + [r]
    else:
        print("[mirror] 警告: 找不到国内清单 source_inventory_cn.csv; "
              "已接入的国内源不会出现在运行时(检查发布/安装流程是否漏装该文件)")

    sources: list[dict[str, Any]] = []
    used: dict[str, int] = {}
    for i, r in enumerate(rows):
        name = (r.get(C_NAME) or "").strip()
        if not name:
            continue
        aliases = aliases_map.get(name, [])
        sid = slugify(name, i, aliases)
        if sid in used:
            used[sid] += 1
            sid = f"{sid}_{used[sid]}"
        else:
            used[sid] = 1
        eps = defs.get(name) or [{
            "id": "entry", "name": "官方入口(通用)", "method": "GET",
            "url_template": (r.get(C_ENTRY) or "").strip(),
            "response": {"shape": "object", "media_type": "*/*", "fields": []},
        }]
        auth_txt = (r.get(C_AUTH) or "").strip()
        auth_type, auth_opt = classify_auth(auth_txt)
        blob = " ".join([auth_txt, (r.get(C_LIMITS) or ""), (r.get(C_CONTENT) or "")])
        scale_text = (r.get(C_SCALE) or "").strip()
        proxy_txt = (r.get(C_PROXY) or "").strip()
        sources.append({
            "id": sid, "name": name,
            "family": (r.get(C_FAMILY) or "").strip(),
            "org": (r.get(C_ORG) or "").strip(),
            "country": (r.get(C_COUNTRY) or "").strip(),
            "summary": (r.get(C_CONTENT) or "").strip(),
            "homepage": (r.get(C_ENTRY) or "").strip(),
            "link_status": (r.get(C_LINK) or "").strip(),
            "auth": {"type": auth_type, "optional": auth_opt, "note": auth_txt},
            "limits": {"note": (r.get(C_LIMITS) or "").strip()},
            "scale": {"total": parse_total(scale_text), "unit": parse_unit(scale_text),
                      "basis": scale_text},
            "increment": {"frequency": (r.get(C_FREQ) or "").strip(),
                          "daily": (r.get(C_INCREMENT) or "").strip(),
                          "strategy": (r.get(C_CRAWL) or "").strip()},
            "crawl": {"mode": "query", "batch_files": [],
                      "notes": (r.get(C_BULK) or "").strip()},
            "proxy_policy": "never" if proxy_txt.startswith("不走") else "fallback",
            "risk": (r.get(C_RISK) or "").strip() or "low",
            "tags": [], "aliases": aliases,
            "ua_requires_contact": any(k in blob for k in ("邮箱", "mailto", "UA")),
            "endpoints": eps,
        })
    return {"schema_version": "1.1", "count": len(sources), "families": [], "sources": sources}


# ── 源解析(与 Resolver.resolve_source 一致) ──────────────────────────────
def resolve_source(sources: dict[str, dict], key: str) -> str | None:
    if not key:
        return None
    if key in sources:
        return key
    norm = re.sub(r"[^a-z0-9]+", "", key.lower())
    if not norm:
        return None
    for sid, s in sources.items():
        if norm in [re.sub(r"[^a-z0-9]+", "", a.lower()) for a in s.get("aliases", [])]:
            return sid
    for sid in sources:
        if re.sub(r"[^a-z0-9]+", "", sid.lower()) == norm:
            return sid
    for sid, src in sources.items():
        if re.sub(r"[^a-z0-9]+", "", src["name"].lower()) == norm:
            return sid
    hits = [sid for sid, s in sources.items()
            if norm and (norm in re.sub(r"[^a-z0-9]+", "", s["name"].lower())
                         or norm in re.sub(r"[^a-z0-9]+", "", s["id"].lower()))]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return min(hits, key=lambda h: len(sources[h]["name"]))
    return None


# ── URL 渲染(与 Resolver._render 一致) ──────────────────────────────────
def render_url(ep: dict, params: dict | None, page_size: int | None = None,
               contact: str = "") -> str:
    params = dict(params or {})
    for p in ep.get("params", []):
        if p["name"] not in params and p.get("default") is not None:
            params[p["name"]] = p["default"]
        if p["name"] in ("email", "mailto") and not params.get(p["name"]) and contact:
            params[p["name"]] = contact
    missing = [p["name"] for p in ep.get("params", [])
               if p.get("required") and p["name"] not in params]
    if missing:
        raise KeyError(", ".join(missing))
    params.setdefault("url", "")
    params.pop("_cb", None)
    page_size = int(page_size if page_size is not None
                     else (ep.get("page_size") or DEFAULT_PAGE_SIZE))
    for k in list(params):
        if k in PAGE_SIZE_PARAMS and params[k] not in (None, ""):
            params[k] = page_size

    raw = ep["url_template"]
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
    used = set(re.findall(r"\{([a-zA-Z0-9_]+)\}", raw))
    extra = {k: v for k, v in params.items()
             if k not in used and k != "url" and v is not None and v != ""}
    if extra:
        sep = "&" if "?" in url else "?"
        url += sep + urllib.parse.urlencode(extra, doseq=True)
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


# ── 响应解析与投影(与 Resolver._parse / _project 一致) ───────────────────
NOT_JSON_PREVIEW_CHARS = 240


class NotJSONError(Exception):
    """上游正文不是 JSON(常见于上游错误页 / WAF 拦截页)。

    与运行时的 `a_source_gateway.resolver.UpstreamNotJSONError` 同义:
    静默兜底成 {"_text": 原文} 会让调用方把"上游报错"误读成"没有数据"。
    """

    def __init__(self, preview: str):
        # 空正文不追加 "; 正文片段: " 后缀 —— 与 Go 侧 NotJSONError 的行为
        # 逐字一致(Go 侧在片段为空时省略后缀)。
        msg = "上游返回的不是 JSON(疑似 HTML 错误页)"
        if preview:
            msg += "; 正文片段: " + preview
        super().__init__(msg)
        self.preview = preview


# ── 形状不匹配(csv/tsv/jsonl 拿到的是 XML/HTML 错误页) ─────────────────
# 下面这几个常量与判定函数在 resolver.py / parser_worker.py 里逐字重复:
# 三份解析实现必须给出同一个结论, 少改一份就会出现"同一份正文,三个地方不同结论"。
# 判定口径见 docs/meta-schema.md 的 parse_error。
MARKUP_PREFIXES = ("<!doctype", "<html", "<?xml")   # 标记语言的开头特征(小写比较)
MARKUP_SCAN_BYTES = 512        # "正文前 N 字节内出现 </" —— 闭合标签标记的扫描窗口

# 形状 -> (正文里说的形状名, 疑似是什么错误页); 文案与 Go 侧逐字相同
SHAPE_MISMATCH_TEXT: dict[str, tuple[str, str]] = {
    "csv": ("CSV", "XML/HTML 错误页"),
    "tsv": ("TSV", "XML/HTML 错误页"),
    "jsonl": ("JSONL", "HTML 错误页"),
}


def strip_bom_and_space(body: str) -> str:
    """去掉 UTF-8 BOM 与首尾空白(判定标记语言前先归一化)。"""
    s = body.strip()
    if s.startswith("\ufeff"):
        s = s[1:].strip()
    return s


def looks_like_markup(body: str) -> bool:
    """正文是不是标记语言(XML/HTML)—— csv/tsv 拿到这种正文就不是表格。

    命中条件(满足任一):
      - 归一化后以 <!doctype / <html / <?xml 开头(大小写不敏感);
      - 归一化后以 < 开头, 且正文前 512 字节内出现 "</"。

    最后一条刻意保守: 合法 CSV 的字段可能以 < 开头(如 <unknown>),
    但单个 <unknown> 不会带来闭合标签标记 "</", 所以不会误伤。
    """
    s = strip_bom_and_space(body)
    if not s:
        return False
    if s.lower().startswith(MARKUP_PREFIXES):
        return True
    if s.startswith("<"):
        return b"</" in s.encode("utf-8")[:MARKUP_SCAN_BYTES]
    return False


def shape_mismatch_message(shape: str, preview: str) -> str:
    """形状不匹配的报错文案(两个平面逐字相同)。

    正文为空时不带 "; 正文片段: " 后缀 —— 与 Go 侧 NotJSONError 的行为一致。
    """
    label, suspect = SHAPE_MISMATCH_TEXT.get(shape, (shape.upper(), "XML/HTML 错误页"))
    msg = f"上游返回的不是 {label}(疑似 {suspect})"
    if preview:
        msg += f"; 正文片段: {preview}"
    return msg


class NotShapeError(NotJSONError):
    """上游正文不是端点声明的形状(csv/tsv/jsonl 却拿到 XML/HTML 错误页)。

    csv/tsv 以前把错误页当**数据行**返回, jsonl 跳过坏行得到空列表 —— 调用方
    看到的是一次"成功", 分不清"确实没有数据"和"上游给了错误页"。

    继承 NotJSONError: 两者同属"上游给的不是声明的那种正文"; scripts/_asg_bootstrap.py
    的 parse_body 只捕获 NotJSONError —— 形状不匹配也必须能被它接住, 否则
    verify_source.py 会直接抛栈, 而不是报出一条断言。
    """

    def __init__(self, shape: str, preview: str):
        # 不用父类的 __init__: 它的文案是"不是 JSON", 这里要说的是"不是声明的形状"。
        Exception.__init__(self, shape_mismatch_message(shape, preview))
        self.shape = shape
        self.preview = preview


def body_preview(body: str, limit: int = NOT_JSON_PREVIEW_CHARS) -> str:
    """正文前若干字符: 换行/制表符压成空格、限长。

    与运行时 `resolver.body_preview` 的处理一致(240 字符、单行)。
    """
    flat = " ".join(body[: limit * 4].split())
    return flat[:limit]


def parse_body(body: str, shape: str, strict: bool = False) -> Any:
    """把上游正文解析成中间结构。

    `strict=True` 时,json 形状解析失败会抛 NotJSONError(而不是静默兜底) ——
    证据关卡据此把"上游给了错误页"与"元数据写错了"区分开。

    `strict=True` 时,csv/tsv 拿到 XML/HTML 错误页、jsonl 整批行都解析不出来,
    会抛 NotShapeError(而不是把错误页当数据行 / 静默给出空列表) ——
    证据关卡据此把"上游给了错误页"与"确实没有数据"区分开。
    `strict=False` 时保持宽松: 把解析到的行原样返回(demo 要的是"上游给的东西")。
    """
    if shape in ("csv", "tsv"):
        if strict and looks_like_markup(body):
            raise NotShapeError(shape, body_preview(body))
        delim = "\t" if shape == "tsv" else ","
        rows = []
        for i, row in enumerate(csv.reader(io.StringIO(body), delimiter=delim)):
            if i >= MAX_PARSE_ROWS:
                break
            rows.append(row)
        return rows
    if shape == "jsonl":
        out = []
        seen = 0                   # 非空行数(用来区分"没有数据"和"全军覆没")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            seen += 1
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= MAX_PARSE_ROWS:
                break
        if strict and seen and not out:
            # 有内容却一行都解析不出来 -> 这不是 JSONL, 是上游的 HTML 错误页
            raise NotShapeError("jsonl", body_preview(body))
        return out
    if shape == "xml":
        return {"_xml": body[:JSON_LIMIT_BYTES]}
    if shape == "text":
        return {"_text": body[:200_000]}
    try:
        return json.loads(body)
    except Exception:
        if strict:
            # 证据关卡要区分"上游给了错误页"和"元数据写错了" —— 不能静默兜底。
            raise NotJSONError(body_preview(body)) from None
        return {"_text": body[:200_000]}


def project(payload: Any, ep: dict, limit: int = 20, fields: list[str] | None = None):
    total = None
    resp = ep.get("response", {})
    total_field = resp.get("total_field")
    item_path = resp.get("item_path")

    if total_field:
        node: Any = payload
        for part in total_field.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.isdigit():
                idx = int(part)
                node = node[idx] if idx < len(node) else None
            else:
                node = None
                break
        if isinstance(node, int | float):
            total = int(node)

    data: Any = payload
    if resp.get("item_shape") == "map" and isinstance(payload, dict):
        data = list(payload.values())
    elif item_path:
        # 顶层是数组时: 路径首段是数字才按下标定位(如 "0.data");
        # 否则保持整个数组(旧行为, 不能让"顶层数组"变成空列表)。
        head = item_path.split(".")[0]
        if isinstance(payload, dict) or head.isdigit():
            node = payload
            for part in item_path.split("."):
                if isinstance(node, dict):
                    node = node.get(part)
                elif isinstance(node, list) and part.isdigit():
                    idx = int(part)
                    node = node[idx] if idx < len(node) else None
                else:
                    node = None
                    break
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
        if limit and len(data) > limit:
            data = data[:limit]
            truncated = True
        elif total is not None and total > len(data):
            truncated = True
        if fields and data and isinstance(data[0], dict):
            keep = set(fields)
            data = [{k: v for k, v in row.items() if k in keep} for row in data]
    elif isinstance(payload, dict) and "_xml" in payload:
        data = {"_xml_bytes": len(payload["_xml"].encode("utf-8")),
                "_truncated": len(payload["_xml"]) >= JSON_LIMIT_BYTES}
        truncated = payload.get("_truncated", False)
    return data, total, truncated
