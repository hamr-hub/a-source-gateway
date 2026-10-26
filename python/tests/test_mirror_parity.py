"""
test_mirror_parity.py - scripts/_asg_mirror.py 与 a_source_gateway.resolver.Resolver 的语义一致性。

背景: 网关运行时语义一共有三份副本:
  1. Go 网关(gateway/internal/server/resolver.go)—— 主运行时;
  2. Python 参照实现(python/src/a_source_gateway/resolver.py);
  3. stdlib-only 镜像(scripts/_asg_mirror.py)—— verify_source.py / demo.py 的兜底实现。

第 3 份是"抄"出来的, 一旦漂移就会出现最难查的那类问题: 同一份数据文件在 Go 网关能跑,
在 MCP 本地回落或 verify/demo 脚本里却跑不出同样的结果。镜像之前就继承了裸查询参数的
IndexError bug —— 同一份模板, Go 侧正常, Python 侧直接崩。

本文件把**同一批**渲染/投影用例同时喂给参照实现与镜像, 断言两件事:
  1. 两边结果一致(漂移检测);
  2. 两边结果等于文档化的期望值(语义检测)—— 只有第 2 条才抓得住"两边一起错"。

第 2 条的期望值按 Go 网关的语义手写(不是从实现里抄的), 所以语义被改坏时这里会红。

解析侧还有第三份副本: parser_worker.py(Go 网关把解析外包给 Python 时走它)。
"形状不匹配"(csv/tsv/jsonl 拿到 XML/HTML 错误页)在三份实现里都必须给出**同一个结论** ——
少改一份就会出现"同一份正文,三个地方不同结论"这种最难查的问题, 所以下面最后一节
把三份实现喂同一批正文, 逐条比对。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from a_source_gateway.config import load_config
from a_source_gateway.parser_worker import NotShapeError as WorkerNotShapeError
from a_source_gateway.parser_worker import body_preview as worker_body_preview
from a_source_gateway.parser_worker import parse_body as worker_parse_body
from a_source_gateway.resolver import Resolver, UpstreamNotShapeError
from a_source_gateway.resolver import body_preview as resolver_body_preview
from a_source_gateway.schema import QueryRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIRROR_PATH = REPO_ROOT / "scripts" / "_asg_mirror.py"


def load_mirror() -> ModuleType:
    """按路径加载 scripts/_asg_mirror.py。

    scripts/ 刻意不做成包(没有 __init__.py), 所以用 importlib 按文件路径加载,
    pytest 收集时不会把 scripts/ 当测试目录, 也不会污染 sys.path。
    """
    key = "_t_asg_mirror"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, MIRROR_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mirror() -> ModuleType:
    return load_mirror()


def _resolver(contact: str = "") -> Resolver:
    """一个不需要注册表数据的 Resolver: 渲染/投影只吃 endpoint 与请求。"""
    return Resolver({"sources": []}, cfg=load_config(contact=contact))


def _ep(url_template: str, params: list[dict[str, Any]] | None = None,
        response: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "ep", "method": "GET", "url_template": url_template,
            "params": params or [], "response": response or {"shape": "object"}}


# ── 渲染用例 ──────────────────────────────────────────────────────────────
# 期望值按 Go 网关(gateway/internal/server/resolver.go)的语义手写。
RENDER_CASES: list[dict[str, Any]] = [
    {
        "name": "裸查询参数(没有 = 的键)必须保留, 不能崩",
        "ep": _ep("https://example.org/v1/items?pretty&limit={size}",
                  [{"name": "size", "in": "query"}]),
        "params": {"size": 10},
        "page_size": 10,
        "expected": "https://example.org/v1/items?pretty&limit=10",
    },
    {
        "name": "{url} 必须百分号编码(不能注入额外查询参数)",
        "ep": _ep("https://example.org/v1/proxy?target={url}&rows=10",
                  [{"name": "url", "in": "query", "required": True}]),
        "params": {"url": "http://169.254.169.254/latest/meta-data/&admin=true"},
        "expected": ("https://example.org/v1/proxy?target=http%3A%2F%2F169.254.169.254"
                     "%2Flatest%2Fmeta-data%2F%26admin%3Dtrue&rows=10"),
    },
    {
        "name": "路径参数保留 /",
        "ep": _ep("https://example.org/v2/{route}/data?size={size}",
                  [{"name": "route", "in": "path"}, {"name": "size", "in": "query"}]),
        "params": {"route": "a/b/c", "size": 10},
        "page_size": 10,
        "expected": "https://example.org/v2/a/b/c/data?size=10",
    },
    {
        "name": "页大小归一化: rows=3 与 rows=50 必须渲染成同一个 URL",
        "ep": _ep("https://example.org/v1/items?rows={rows}",
                  [{"name": "rows", "in": "query"}]),
        "params_variants": [{"rows": 3}, {"rows": 50}],
        "expected": "https://example.org/v1/items?rows=25",
    },
    {
        "name": "_cb 缓存穿透参数不上报上游",
        "ep": _ep("https://example.org/v1/items?rows={rows}",
                  [{"name": "rows", "in": "query"}]),
        "params": {"rows": 10, "_cb": "abc123"},
        "expected": "https://example.org/v1/items?rows=25",
    },
    {
        "name": "空值查询参数被丢弃(mailto=)",
        "ep": _ep("https://example.org/v1/items?mailto={email}&rows={rows}",
                  [{"name": "email", "in": "query"}, {"name": "rows", "in": "query"}]),
        "params": {"email": "", "rows": 10},
        "expected": "https://example.org/v1/items?rows=25",
    },
    {
        "name": "模板外的参数追加到查询串",
        "ep": _ep("https://example.org/v1/items", []),
        "params": {"filter": "from-index-date:2026-10-21"},
        "expected": "https://example.org/v1/items?filter=from-index-date%3A2026-10-21",
    },
    {
        "name": "contact 自动填邮箱参数",
        "ep": _ep("https://example.org/v1/items?mailto={email}",
                  [{"name": "email", "in": "query"}]),
        "params": {},
        "contact": "test@example.com",
        "expected": "https://example.org/v1/items?mailto=test%40example.com",
    },
]


@pytest.mark.parametrize("case", RENDER_CASES, ids=[c["name"] for c in RENDER_CASES])
def test_resolver_and_mirror_render_identically(case: dict[str, Any], mirror: ModuleType) -> None:
    ep = case["ep"]
    contact = case.get("contact", "")
    resolver = _resolver(contact)
    variants = case.get("params_variants") or [case["params"]]

    for params in variants:
        rendered_resolver = resolver._render(
            ep,
            QueryRequest(source="s", endpoint=ep["id"], params=dict(params)),
            page_size=case.get("page_size"),
        )
        rendered_mirror = mirror.render_url(
            ep, dict(params), page_size=case.get("page_size"), contact=contact)

        assert rendered_resolver == rendered_mirror, (
            f"参照实现与镜像漂移了: resolver={rendered_resolver!r} mirror={rendered_mirror!r}")
        assert rendered_resolver == case["expected"], (
            f"渲染结果不符合 Go 网关语义: {rendered_resolver!r} != {case['expected']!r}")


def test_both_sides_reject_a_missing_required_param(mirror: ModuleType) -> None:
    ep = _ep("https://example.org/v1/items/{id}",
             [{"name": "id", "in": "path", "required": True}])
    resolver = _resolver()
    with pytest.raises(KeyError):
        resolver._render(ep, QueryRequest(source="s", endpoint="ep", params={}))
    with pytest.raises(KeyError):
        mirror.render_url(ep, {})


# ── 投影用例 ──────────────────────────────────────────────────────────────
# expected = (items, total, truncated)
PROJECTION_CASES: list[dict[str, Any]] = [
    {
        "name": "item_path 点号遍历 + total_field",
        "payload": {"message": {"items": [{"a": 1}, {"a": 2}]}, "meta": {"total": 17}},
        "ep": _ep("https://example.org/v1/items",
                  response={"shape": "object", "item_path": "message.items",
                            "total_field": "meta.total"}),
        "limit": 0,
        "expected": ([{"a": 1}, {"a": 2}], 17, True),
    },
    {
        "name": "total_field 支持数字下标",
        "payload": {"data": [{"total": 42, "x": 1}, {"total": 7}]},
        "ep": _ep("https://example.org/v1/items",
                  response={"shape": "object", "item_path": "data",
                            "total_field": "data.0.total"}),
        "limit": 0,
        "expected": ([{"total": 42, "x": 1}, {"total": 7}], 42, True),
    },
    {
        "name": "total_field 是带点的字面键(@odata.count)",
        # 真实端点: WHO GHO / GHED / ICTRP 的 indicator 端点就是这么声明的。
        # 键名本身带点, 按 "." 切永远取不到 —— 三个实现都得有整串字面键兜底。
        "payload": {"value": [{"a": 1}], "@odata.count": 42},
        "ep": _ep("https://example.org/v1/items",
                  response={"shape": "object", "item_path": "value",
                            "total_field": "@odata.count"}),
        "limit": 0,
        "expected": ([{"a": 1}], 42, True),
    },
    {
        "name": "item_shape=map 按值域投影",
        "payload": {"0": {"a": 1}, "1": {"a": 2}},
        "ep": _ep("https://example.org/v1/items",
                  response={"shape": "object", "item_shape": "map"}),
        "limit": 0,
        "expected": ([{"a": 1}, {"a": 2}], 2, False),
    },
    {
        "name": "limit 截断并置 truncated",
        "payload": {"items": [{"a": i} for i in range(5)]},
        "ep": _ep("https://example.org/v1/items",
                  response={"shape": "object", "item_path": "items"}),
        "limit": 2,
        "expected": ([{"a": 0}, {"a": 1}], 5, True),
    },
    {
        "name": "fields 只保留请求的字段",
        "payload": {"items": [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]},
        "ep": _ep("https://example.org/v1/items",
                  response={"shape": "object", "item_path": "items"}),
        "limit": 0,
        "fields": ["a"],
        "expected": ([{"a": 1}, {"a": 4}], 2, False),
    },
    {
        "name": "上游没给总数时 total 取条目数",
        "payload": {"items": [{"a": 1}, {"a": 2}, {"a": 3}]},
        "ep": _ep("https://example.org/v1/items",
                  response={"shape": "object", "item_path": "items"}),
        "limit": 0,
        "expected": ([{"a": 1}, {"a": 2}, {"a": 3}], 3, False),
    },
    {
        "name": "XML 兜底投影 _xml_bytes",
        "payload": {"_xml": "<root><a>1</a></root>"},
        "ep": _ep("https://example.org/v1/items", response={"shape": "xml"}),
        "limit": 0,
        # _xml_bytes 是 XML 原文的 UTF-8 字节数: "<root><a>1</a></root>" = 21 字节
        "expected": ({"_xml_bytes": 21, "_truncated": False}, None, False),
    },
    {
        "name": "非 JSON 正文兜底成 _text",
        "payload": {"_text": "not json at all"},
        "ep": _ep("https://example.org/v1/items", response={"shape": "object"}),
        "limit": 0,
        "expected": ({"_text": "not json at all"}, None, False),
    },
]


@pytest.mark.parametrize("case", PROJECTION_CASES, ids=[c["name"] for c in PROJECTION_CASES])
def test_resolver_and_mirror_project_identically(case: dict[str, Any], mirror: ModuleType) -> None:
    ep = case["ep"]
    payload = case["payload"]
    limit = case.get("limit", 0)
    fields = case.get("fields")

    projected_resolver = _resolver()._project(
        payload, ep, QueryRequest(source="s", endpoint=ep["id"], limit=limit, fields=fields or []))
    projected_mirror = mirror.project(payload, ep, limit=limit, fields=fields)

    assert projected_resolver == projected_mirror, (
        f"参照实现与镜像漂移了: resolver={projected_resolver!r} mirror={projected_mirror!r}")
    assert projected_resolver == case["expected"], (
        f"投影结果不符合 Go 网关语义: {projected_resolver!r} != {case['expected']!r}")


# ── 形状不匹配: 三份解析实现必须给出同一个结论 ──────────────────────────
# 三份实现: 参照面(resolver) / parser worker(Go 网关的解析外包路径) /
# stdlib 镜像(scripts/_asg_mirror.py)。同一份正文, 三处必须同进同退。
#
# 命中 = 上游给的正文不是端点声明的形状(csv/tsv 拿到 XML/HTML 错误页,
# jsonl 整批行都解析不出来)。命中时三处都要报错, 未命中时三处都要给出同样的数据。
SHAPE_CASES: list[dict[str, Any]] = [
    {"name": "csv 拿到 HTML 错误页", "shape": "csv",
     "body": "<!DOCTYPE html>\n<html><body>503</body></html>", "mismatch": True},
    {"name": "csv 拿到 XML 错误页", "shape": "csv",
     "body": "<?xml version='1.0'?><error/>", "mismatch": True},
    {"name": "csv 拿到泛 XML", "shape": "csv",
     "body": "<root><a>1</a></root>", "mismatch": True},
    {"name": "csv 拿到小写 html", "shape": "csv",
     "body": "<html></html>", "mismatch": True},
    {"name": "csv 拿到带 BOM 的 HTML", "shape": "csv",
     "body": "﻿<!DOCTYPE html><html></html>", "mismatch": True},
    {"name": "csv 拿到闭合标签在扫描窗口内", "shape": "csv",
     "body": "<" + "x" * 100 + "</a>", "mismatch": True},
    {"name": "csv 的闭合标签在扫描窗口外(不命中)", "shape": "csv",
     "body": "<" + "x" * 600 + "</a>", "mismatch": False},
    {"name": "csv 正常表格", "shape": "csv", "body": "a,b\n1,2\n", "mismatch": False},
    {"name": "csv 的字段以 < 开头(不命中)", "shape": "csv",
     "body": "name,value\n<unknown>,1\n", "mismatch": False},
    {"name": "csv 空正文(不命中)", "shape": "csv", "body": "", "mismatch": False},
    {"name": "csv 空白正文(不命中)", "shape": "csv", "body": "   \n  \n", "mismatch": False},
    {"name": "tsv 拿到 HTML 错误页", "shape": "tsv",
     "body": "<!DOCTYPE html>\n<html></html>", "mismatch": True},
    {"name": "tsv 正常表格", "shape": "tsv", "body": "a\tb\n1\t2\n", "mismatch": False},
    {"name": "jsonl 拿到 HTML 错误页", "shape": "jsonl",
     "body": "<!DOCTYPE html>\n<html></html>", "mismatch": True},
    {"name": "jsonl 整批行都坏", "shape": "jsonl",
     "body": "not json\nalso not json\n", "mismatch": True},
    {"name": "jsonl 部分行坏(不命中)", "shape": "jsonl",
     "body": '{"a":1}\nnope\n{"a":2}\n', "mismatch": False},
    {"name": "jsonl 全部行正常", "shape": "jsonl",
     "body": '{"a":1}\n{"a":2}\n', "mismatch": False},
    {"name": "jsonl 空正文(不命中)", "shape": "jsonl", "body": "", "mismatch": False},
    {"name": "jsonl 只有空行(不命中)", "shape": "jsonl",
     "body": "\n\n  \n", "mismatch": False},
]

# 固定文案(手写, 不从实现里抄): 两个平面给调用方看的话必须是同一句
SHAPE_FIXED_WORDING = {
    "csv": "上游返回的不是 CSV(疑似 XML/HTML 错误页)",
    "tsv": "上游返回的不是 TSV(疑似 XML/HTML 错误页)",
    "jsonl": "上游返回的不是 JSONL(疑似 HTML 错误页)",
}


def _shape_conclusions(body: str, shape: str, mirror: ModuleType
                       ) -> dict[str, tuple[bool, Any]]:
    """三份实现对同一份正文给出的结论: {实现名: (是否命中, 文案或数据)}。"""
    resolver = _resolver()
    out: dict[str, tuple[bool, Any]] = {}
    try:
        out["resolver"] = (False, resolver._parse(body, shape, strict=True))
    except UpstreamNotShapeError as exc:
        out["resolver"] = (True, str(exc))
    try:
        out["mirror"] = (False, mirror.parse_body(body, shape, strict=True))
    except mirror.NotShapeError as exc:
        out["mirror"] = (True, str(exc))
    try:
        out["worker"] = (False, worker_parse_body(body, shape))
    except WorkerNotShapeError as exc:
        out["worker"] = (True, str(exc))
    return out


@pytest.mark.parametrize("case", SHAPE_CASES, ids=[c["name"] for c in SHAPE_CASES])
def test_three_parsers_agree_on_shape_mismatch(case: dict[str, Any], mirror: ModuleType) -> None:
    """同一份正文, 三份实现必须同进同退 —— 少改一份就会出现最难查的那类问题。"""
    shape = case["shape"]
    body = case["body"]
    results = _shape_conclusions(body, shape, mirror)

    for impl, (hit, _) in results.items():
        assert hit is case["mismatch"], (
            f"{impl} 的结论与文档化口径不符: 命中={hit}, 期望={case['mismatch']} "
            f"(shape={shape}, body={body[:60]!r})")

    if not case["mismatch"]:
        # 不命中时三处给的数据也要一样
        values = [value for _, value in results.values()]
        assert values[0] == values[1] == values[2], f"三份实现给出的数据不一致: {results}"
        return

    # 命中时三处的文案逐字相同(各自的正文片段算法给出同样的片段时)
    previews = {
        "resolver": resolver_body_preview(body),
        "mirror": mirror.body_preview(body),
        "worker": worker_body_preview(body),
    }
    for impl, (_hit, message) in results.items():
        expected = SHAPE_FIXED_WORDING[shape]
        if previews[impl]:
            expected += "; 正文片段: " + previews[impl]
        assert message == expected, f"{impl} 的文案不对: {message!r} != {expected!r}"
    if len(set(previews.values())) == 1:
        messages = {message for _, message in results.values()}
        assert len(messages) == 1, f"三份实现的文案不一致: {results}"
