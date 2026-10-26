"""
registry.py - 数据源元数据注册表

三份事实源, 都是**声明式数据文件**, 贡献者不需要改 Python 代码:

  data/source_inventory.csv  海外源的基本信息(机构/入口/限速/规模/增量/代理策略/合规风险)
  data/source_inventory_cn.csv 国内源清单(同一套 17 列 + 第 18 列 优先级; 见 AGENTS.md 规则 3.1)
  data/endpoints.json         可调用端点(请求参数 + 响应结构)
  data/aliases.json           源名 → 常用简称(MCP/LLM 调用方常这么叫)

注册表在运行时由这些文件构建(实测约 4ms), 因此不存在"生成物与源文件不同步"的问题。
国内清单里**只有已在 endpoints.json 有端点定义**的行会进注册表(其余是候选, 不进运行时)。
若需要离线快照, 用 `a-source-gateway build-registry --out registry.json` 导出。

**加源不改代码**: 新增别名只要往 data/aliases.json 加一行(键 = CSV 里的"数据源"原文)。

**搜索索引在注册表构建期一次算好**(见 `build_search_index`): 源检索/别名解析
都用它, 请求期不再逐源跑正则。索引是运行时内部结构, 不进导出的注册表 JSON ——
导出格式是两平面的对齐契约(`make parity` 逐字段比对), Go 侧同样在运行时自建索引。
"""
from __future__ import annotations

import csv
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ALIASES_JSON, CN_INVENTORY_CSV, INVENTORY_CSV, Config, load_config
from .schema import (
    UPSTREAM_BUDGET_MS,
    AuthSpec,
    CrawlSpec,
    EndpointSpec,
    FieldSpec,
    IncrementSpec,
    LimitsSpec,
    ParamSpec,
    ResponseSpec,
    ScaleSpec,
    SourceSpec,
)

# ── CSV 列名(契约, 改动需同步 docs/meta-schema.md) ───────────────────────
C_FAMILY, C_NAME, C_ORG, C_COUNTRY = "分类", "数据源", "机构", "国别"
C_CONTENT, C_ENTRY = "数据内容", "官方入口"
C_AUTH, C_LIMITS, C_BULK, C_FREQ = "认证方式", "官方限速口径", "全量下载", "更新频率"
C_SCALE, C_INCREMENT, C_PROXY = "实测规模", "实测增量", "代理需求"
C_RISK, C_CRAWL = "合规风险", "建议采集方式"
C_LINK, C_LINK_MS = "链接状态", "探测耗时ms"
# 最后验证: 这条元数据最近一次被真实请求核对过的日期(ISO)。
# 空着表示"还没核过" —— 留空比填一个自己没跑过的日期诚实。
C_VERIFIED = "最后验证"

# 清单的列契约(两份清单共用同一套 18 列, 国内清单多一列 优先级)
INVENTORY_COLUMNS = [
    C_FAMILY, C_NAME, C_ORG, C_COUNTRY, C_CONTENT, C_ENTRY, C_AUTH, C_LIMITS,
    C_BULK, C_FREQ, C_SCALE, C_INCREMENT, C_PROXY, C_RISK, C_CRAWL, C_LINK,
    C_LINK_MS, C_VERIFIED,
]
CN_EXTRA_COLUMNS = ["优先级"]

# 清单里 最后验证 列为空时的取值: 表示"这条元数据还没被真实请求核对过"。
# 它是一个明确的哨兵, 不是某个具体的历史日期 —— 具体日期会让人误以为核过。
DEFAULT_CHECKED_AT = "unverified"
CN_PRIORITY_VALUES = ("P0", "P1", "P2")

# 归入"国内清单"的国别取值(规则 3.1:只看国别列分流)。
# 注意「中国香港」是单独法域, 留在海外清单 —— 所以这里用白名单而不是 startswith。
CN_COUNTRIES = frozenset({"中国", "中国大陆", "中华人民共和国"})


def is_cn_country(country: str) -> bool:
    """国别是否属于「国内」—— 决定这个源落在哪份清单(规则 3.1)。"""
    return (country or "").strip() in CN_COUNTRIES


_log = logging.getLogger(__name__)

# ── 常见简称/别名: 从 data/aliases.json 加载(见 load_aliases) ───────────
# 历史遗留: 这里曾硬编码一份 ALIASES 字典, 现已迁到 data/aliases.json ——
# 加别名不再需要改 Python 代码。

UNIT_MAP = [("取值", "values"), ("张表", "tables"), ("表", "tables"),
             ("份", "filings"), ("项", "items"), ("篇", "papers"), ("条", "records"),
             ("家", "entities"), ("个", "records"), ("种", "types"), ("笔", "transactions"),
             ("件", "items"), ("国", "countries"), ("位", "people")]
COUNT_UNITS = "份条项个家种笔件国位机构标目页版款座套"


# ── 解析辅助 ─────────────────────────────────────────────────────────────
def parse_total(text: str) -> int | None:
    """从实测描述里取主数量(处理"50 万"与小数; 不取分数)。"""
    if not text:
        return None
    # 归一化千分位: "9,011,130,327" -> "9011130327"
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


# 认证方式取值 -> (type, optional)。顺序敏感: 先匹配更具体的。
AUTH_RULES: list[tuple[str, str, bool]] = [
    ("无key", "none", False),
    ("无 key", "none", False),
    ("无需", "none", False),
    ("需邮箱", "email_param", False),
    ("邮箱", "email_param", False),
    ("可选key", "api_key", True),
    ("可注册key", "api_key", False),
    ("免费key", "api_key", False),
    ("api key", "api_key", False),
    ("需token", "token", False),
    ("token", "oauth2", False),
    ("oauth", "oauth2", False),
    ("部分付费", "paid", False),
    ("需付费", "paid", False),
    ("账号+付费", "paid", False),
    ("免费/付费", "mixed", False),
    ("部分免费部分付费", "mixed", False),
    ("多数免费", "mixed", False),
    ("部分注册", "mixed", False),
]


def classify_auth(text: str) -> tuple[str, bool]:
    """把 CSV 的"认证方式"原文映射成 (type, optional)。

    坑: 直接用 "key" in text 判定会把 "无key" 误判成需要 key(清单里 65 行)。
    """
    t = (text or "").strip().lower()
    if not t or t in ("无", "none", "-"):
        return "none", False
    # "无key(可选)" 这类: key 可选 -> api_key + optional, 必须先于 "无key" 判定
    if "可选" in t and "key" in t:
        return "api_key", True
    for needle, typ, opt in AUTH_RULES:
        if needle.lower() in t:
            return typ, opt
    return "none", False


def stable_slug(name: str, aliases: list[str] | None = None) -> str | None:
    """与清单顺序**无关**的稳定 slug: 名字自带 ASCII, 或别名里有 ASCII。

    没有它时 slugify 会退化成 `src_<行号>` —— 那种 id 会随清单增删而平移,
    已接入的源不能接受(对外是 API 契约)。接入前先在 aliases.json 里备好 ASCII 别名。
    """
    s = unicodedata.normalize("NFKD", name)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if len(s) >= 3:
        return s
    for a in (aliases or []):
        a = a.strip().lower()
        if re.fullmatch(r"[a-z0-9_]{3,}", a):
            return a
    return None


def slugify(name: str, idx: int, aliases: list[str] | None = None) -> str:
    """稳定的源 id: ASCII 化 -> slug; ASCII 不足时回退到别名, 再不行用序号。"""
    s = stable_slug(name, aliases)
    if s:
        return s
    return f"src_{idx:03d}"


# ── 归一化与搜索索引(注册表构建期一次算好) ───────────────────────────────
def normalize_key(s: str) -> str:
    """检索键归一化: 小写后只保留 [a-z0-9]。

    与 Go 侧 `NormalizeKey`(gateway/internal/registry/registry.go)严格一致 ——
    两边必须逐字符相同, 否则同一个键在两个平面上会解析到不同的源。
    纯中文等无 ASCII 字符的键归一化后为空串, 调用方据此放弃模糊匹配。
    """
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# 精确命中时的优先级: 越小越优先(与旧的解析顺序一致: 别名 -> id -> name)
_RANK_ALIAS, _RANK_ID, _RANK_NAME = 0, 1, 2


@dataclass
class SearchIndex:
    """注册表构建期算好的搜索索引 —— 请求期零正则分配。

    - `haystack`: source_id -> 小写可搜索文本(canonical id、name、org、country、
      family、summary、全部别名、该源全部端点 id, 用 "\\n" 分隔)。子串匹配用。
    - `alias_index`: 归一化 token -> source_id。精确命中用(canonical id、每个别名、
      name 的归一化形式)。
    - `norm_keys`: (source_id, 归一化 name, 归一化 id), 保持注册表顺序 ——
      供"包含"式兜底(多命中时取 name 最短的源)。

    三块内容的字段与拼接顺序都逐段对齐 Go 侧 `registry.BuildSearchIndex`,
    两个平面的检索结果因此完全一致。
    """

    haystack: dict[str, str] = field(default_factory=dict)
    alias_index: dict[str, str] = field(default_factory=dict)
    norm_keys: list[tuple[str, str, str]] = field(default_factory=list)


def build_search_index(sources: list[dict[str, Any]]) -> SearchIndex:
    """在注册表构建期把搜索索引一次算好。

    之前 `resolve_source` 每个请求都要逐源跑 O(n) 次正则(归一化 id/别名/name),
    源一多就成为热路径上的固定开销; 现在全部前移到这一步。

    索引是**运行时内部结构**, 不进导出的注册表 JSON —— 导出格式是两平面的
    对齐契约(`make parity` 会逐字段比对), 索引在 Go 侧同样是运行时自建的。
    """
    haystack: dict[str, str] = {}
    alias_index: dict[str, str] = {}
    norm_keys: list[tuple[str, str, str]] = []
    rank: dict[str, int] = {}          # token -> 已登记的优先级

    def _put(token: str, sid: str, r: int) -> None:
        """登记一个归一化 token。

        优先级: 别名 > canonical id > name —— 与旧的解析顺序一致(旧实现先扫别名,
        再扫 id, 再扫 name)。同一优先级冲突时**先登记的赢**(注册表顺序),
        与旧实现"线性扫描取第一个命中"完全一致, 也与 Go 侧 putToken 一致。
        """
        if not token:
            return
        cur = rank.get(token)
        if cur is None:
            rank[token] = r
            alias_index[token] = sid
            return
        if r < cur:                      # 优先级更高: 覆盖
            rank[token] = r
            alias_index[token] = sid

    for s in sources:
        sid = str(s.get("id", ""))
        name = str(s.get("name", ""))
        aliases = [str(a) for a in (s.get("aliases") or [])]
        for a in aliases:
            _put(normalize_key(a), sid, _RANK_ALIAS)
        _put(normalize_key(sid), sid, _RANK_ID)
        _put(normalize_key(name), sid, _RANK_NAME)

        # 拼接顺序与 Go 侧逐段一致(id, name, org, country, family, summary,
        # 别名, 端点 id), 两个平面的检索结果因此完全相同。
        parts = [sid, name,
                 str(s.get("org", "")), str(s.get("country", "")),
                 str(s.get("family", "")), str(s.get("summary", "")),
                 *aliases]
        parts.extend(str(e.get("id", "")) for e in (s.get("endpoints") or []))
        haystack[sid] = "\n".join(parts).lower()
        norm_keys.append((sid, normalize_key(name), normalize_key(sid)))

    return SearchIndex(haystack=haystack, alias_index=alias_index, norm_keys=norm_keys)


# ── 端点定义 → EndpointSpec ──────────────────────────────────────────────
def build_endpoint(d: dict[str, Any]) -> EndpointSpec:
    resp = d.get("response", {})
    params: list[ParamSpec] = []
    for p in d.get("params", []):
        p = dict(p)
        if "in" in p:                       # "in" 是 Python 关键字, 内部用 in_
            p["in_"] = p.pop("in")
        params.append(ParamSpec(**p))
    return EndpointSpec(
        id=d["id"],
        name=d.get("name", d["id"]),
        method=d.get("method", "GET"),
        url_template=d.get("url_template", ""),
        params=params,
        headers=d.get("headers", {}),
        page_size=d.get("page_size"),
        response=ResponseSpec(
            media_type=resp.get("media_type", "application/json"),
            shape=resp.get("shape", "object"),
            fields=[FieldSpec(**f) for f in resp.get("fields", [])],
            item_path=resp.get("item_path"),
            item_shape=resp.get("item_shape", "list"),
            item_flatten=bool(resp.get("item_flatten", False)),
            total_field=resp.get("total_field"),
            notes=resp.get("notes", ""),
        ),
        cache_ttl_s=d.get("cache_ttl_s", 900),
        budget_ms=d.get("budget_ms") or UPSTREAM_BUDGET_MS,
        notes=d.get("notes", ""),
    )


def generic_endpoint(entry: str) -> EndpointSpec:
    """没有专用端点定义时的通用透传端点(形状按官方入口的扩展名推断)。"""
    shape = ("xml" if entry.endswith(".xml") else "csv" if entry.endswith(".csv")
             else "tsv" if entry.endswith(".tsv") else "object")
    return build_endpoint({
        "id": "entry", "name": "官方入口(通用)", "method": "GET", "url_template": entry,
        "response": {"shape": shape,
                     "media_type": {"xml": "application/xml", "csv": "text/csv",
                                    "tsv": "text/tab-separated-values"}.get(shape, "*/*"),
                     "fields": [],
                     "notes": "通用透传端点(官方入口直取);结构随官方文件而定, 建议先取该源的专用端点"},
    })


# ── 注册表构建 ───────────────────────────────────────────────────────────
def load_inventory(cfg: Config | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    path = cfg.find_data(INVENTORY_CSV)
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_endpoint_defs(cfg: Config | None = None) -> dict[str, list[dict[str, Any]]]:
    cfg = cfg or load_config()
    path = cfg.find_data("endpoints.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc.get("sources", {})


def load_aliases(cfg: Config | None = None) -> dict[str, list[str]]:
    """读 data/aliases.json: {源名原文 -> [常用简称...]}。

    文件缺失或损坏时不抛错, 只告警并返回空表 —— 注册表必须永远能构建,
    否则一次数据文件的手误会让整个网关下线。
    """
    cfg = cfg or load_config()
    try:
        path = cfg.find_data(ALIASES_JSON)
    except FileNotFoundError:
        _log.warning("找不到别名文件 %s; 注册表将不带别名(按简称解析会失效)", ALIASES_JSON)
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _log.warning("别名文件 %s 读取失败(%s: %s); 注册表将不带别名",
                     ALIASES_JSON, type(e).__name__, e)
        return {}
    raw = doc.get("aliases") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        _log.warning("别名文件 %s 结构异常(缺 'aliases' 对象); 注册表将不带别名", ALIASES_JSON)
        return {}
    out: dict[str, list[str]] = {}
    for name, aliases in raw.items():
        if isinstance(aliases, list):
            out[str(name)] = [str(a) for a in aliases]
        else:
            _log.warning("别名文件 %s: %s 的别名不是数组, 已忽略", ALIASES_JSON, name)
    return out


def _read_csv_table(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """读清单 CSV, 返回 (表头, 行)。"""
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def load_cn_inventory_table(cfg: Config | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """国内清单的 (表头, 行)。

    文件缺失时返回 ([], []) —— 注册表必须永远能构建, 但要**告警**:
    缺这个文件意味着已接入的国内源会在运行时消失(发布/安装流程漏装数据文件的典型症状)。
    """
    cfg = cfg or load_config()
    try:
        path = cfg.find_data(CN_INVENTORY_CSV)
    except FileNotFoundError:
        _log.warning("找不到国内清单 %s; 已接入的国内源不会出现在运行时"
                     "(检查发布/安装流程是否漏装该文件)", CN_INVENTORY_CSV)
        return [], []
    return _read_csv_table(path)


def load_cn_inventory(cfg: Config | None = None) -> list[dict[str, Any]]:
    """读国内清单 data/source_inventory_cn.csv(规则 3.1)。"""
    return load_cn_inventory_table(cfg)[1]


def build_registry(cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    rows = load_inventory(cfg)
    defs = load_endpoint_defs(cfg)
    alias_map = load_aliases(cfg)
    sources: list[SourceSpec] = []
    used: dict[str, int] = {}

    # 国内清单(规则 3.1):只有已在 endpoints.json 里有端点定义的国内源才进运行时;
    # 纯候选源(还没接入的)留在 CSV 里, 不进注册表。
    seen_names = {(r.get(C_NAME) or "").strip() for r in rows}
    for r in load_cn_inventory(cfg):
        name = (r.get(C_NAME) or "").strip()
        if not name or name not in defs:
            continue
        if name in seen_names:
            # 同一个源出现在两份清单 → 字段会漂, 这里以海外清单为准并告警
            _log.warning("源 %s 同时出现在两份清单里, 运行时只采用海外清单那一行", name)
            continue
        seen_names.add(name)
        rows.append(r)

    for i, r in enumerate(rows):
        name = (r.get(C_NAME) or "").strip()
        if not name:
            continue
        aliases = alias_map.get(name, [])
        sid = slugify(name, i, aliases)
        if sid in used:
            used[sid] += 1
            sid = f"{sid}_{used[sid]}"
        else:
            used[sid] = 1

        scale_text = (r.get(C_SCALE) or "").strip()
        inc_text = (r.get(C_INCREMENT) or "").strip()
        entry = (r.get(C_ENTRY) or "").strip()

        eps = [build_endpoint(d) for d in defs.get(name, [])] or [generic_endpoint(entry)]

        auth_txt = (r.get(C_AUTH) or "").strip()
        auth_type, auth_opt = classify_auth(auth_txt)

        blob = " ".join([auth_txt, (r.get(C_LIMITS) or ""), (r.get(C_CONTENT) or "")])
        ua_requires_contact = any(k in blob for k in ("邮箱", "mailto", "UA"))

        proxy_txt = (r.get(C_PROXY) or "").strip()
        proxy_policy = "never" if proxy_txt.startswith("不走") else "fallback"

        sources.append(SourceSpec(
            id=sid,
            name=name,
            family=(r.get(C_FAMILY) or "").strip(),
            org=(r.get(C_ORG) or "").strip(),
            country=(r.get(C_COUNTRY) or "").strip(),
            summary=(r.get(C_CONTENT) or "").strip(),
            homepage=entry,
            link_status=(r.get(C_LINK) or "").strip(),
            # 以前这里是硬编码的 "2026-10-22", 于是不管多久没核过,
            # 对外都宣称是那天验证的。现在读清单里的 最后验证 列,
            # 没核过的源就空着 —— 空着比一个假日期有用。
            link_checked_at=(r.get(C_VERIFIED) or "").strip() or DEFAULT_CHECKED_AT,
            auth=AuthSpec(type=auth_type, optional=auth_opt, note=auth_txt),
            limits=LimitsSpec(note=(r.get(C_LIMITS) or "").strip()),
            scale=ScaleSpec(total=parse_total(scale_text), unit=parse_unit(scale_text),
                            measured_at="2026-10-22", basis=scale_text),
            increment=IncrementSpec(frequency=(r.get(C_FREQ) or "").strip(),
                                    daily=inc_text,
                                    strategy=(r.get(C_CRAWL) or "").strip()),
            crawl=CrawlSpec(mode="query", batch_files=[], notes=(r.get(C_BULK) or "").strip()),
            proxy_policy=proxy_policy,
            risk=(r.get(C_RISK) or "").strip() or "low",
            tags=[t for t in [(r.get(C_FAMILY) or "").strip()[:2], (r.get(C_COUNTRY) or "").strip()] if t],
            aliases=aliases,
            ua_requires_contact=ua_requires_contact,
            endpoints=eps,
        ))

    return {
        "schema_version": "1.2",
        "generated_at": "2026-10-22",
        "budget_ms": cfg.budget_ms,
        "count": len(sources),
        "families": sorted({s.family for s in sources}),
        "sources": [s.to_dict() for s in sources],
    }
