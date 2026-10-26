"""注册表构建与元数据契约的测试(纯离线)。"""
from __future__ import annotations

import json

import pytest

from a_source_gateway.config import load_config
from a_source_gateway.registry import (
    build_registry,
    load_cn_inventory,
    load_endpoint_defs,
    load_inventory,
    parse_total,
    parse_unit,
)

REQUIRED_COLUMNS = [
    "分类", "数据源", "机构", "国别", "数据内容", "官方入口", "认证方式", "官方限速口径",
    "全量下载", "更新频率", "实测规模", "实测增量", "代理需求", "合规风险", "建议采集方式",
]


@pytest.fixture(scope="module")
def registry():
    return build_registry(load_config())


@pytest.fixture(scope="module")
def inventory():
    return load_inventory(load_config())


def test_inventory_has_required_columns(inventory):
    assert inventory, "source_inventory.csv 不能为空"
    for col in REQUIRED_COLUMNS:
        assert col in inventory[0], f"缺列: {col}"


def test_inventory_entries_are_wellformed(inventory):
    names = [r["数据源"] for r in inventory]
    assert len(names) == len(set(names)), "数据源名不能重复"
    for r in inventory:
        assert r["官方入口"].startswith("http"), f"{r['数据源']}: 官方入口必须是 URL"
        assert r["实测增量"].strip(), f"{r['数据源']}: 实测增量不能为空"
        assert r["代理需求"].strip(), f"{r['数据源']}: 代理需求不能为空"
        assert r["实测增量"] != r["实测规模"], f"{r['数据源']}: 增量列不能是规模的复制"


def test_registry_shape(registry):
    assert registry["count"] >= 90
    assert len(registry["families"]) >= 8
    assert registry["schema_version"] == "1.1"


def test_every_source_has_full_meta(registry):
    for s in registry["sources"]:
        for key in ("id", "name", "family", "org", "country", "summary", "homepage",
                    "link_status", "auth", "limits", "scale", "increment", "crawl",
                    "proxy_policy", "risk", "endpoints"):
            assert key in s, f"{s.get('id')} 缺字段 {key}"
        assert s["endpoints"], f"{s['id']} 至少要有一个端点"
        assert s["proxy_policy"] in ("never", "fallback")


def test_every_endpoint_has_request_and_response_spec(registry):
    for s in registry["sources"]:
        for e in s["endpoints"]:
            assert e["id"] and e["method"] in ("GET", "POST")
            assert e["url_template"].startswith("http"), f"{s['id']}/{e['id']} url 必须完整"
            assert e["response"]["shape"] in ("object", "array", "csv", "xml", "tsv", "jsonl", "text")
            for p in e["params"]:
                assert p["in"] in ("path", "query", "header", "body")
                assert p["type"] in ("string", "int", "float", "bool", "string[]")
                if p.get("required"):
                    assert p.get("example") is not None or p.get("default") is not None, \
                        f"{s['id']}/{e['id']} 必填参数 {p['name']} 缺 example/default"


def test_endpoint_defs_keys_exist_in_inventory():
    """endpoints.json 的源名必须在**两份清单合计**里找到(国内源在 source_inventory_cn.csv)。"""
    cfg = load_config()
    names = {r["数据源"] for r in load_inventory(cfg)}
    names |= {r["数据源"] for r in load_cn_inventory(cfg)}
    for name in load_endpoint_defs(cfg):
        assert name in names, f"endpoints.json 的 {name} 在两份 CSV 里没有对应行"


def test_cn_inventory_rows_are_only_loaded_when_they_have_endpoint_defs():
    """国内清单是候选池:只有 endpoints.json 里有定义的国内源才进运行时。"""
    cfg = load_config()
    defs = load_endpoint_defs(cfg)
    cn_names = {r["数据源"] for r in load_cn_inventory(cfg)}
    reg = build_registry(cfg)
    runtime_names = {s["name"] for s in reg["sources"]}
    for name in cn_names:
        if name in defs:
            assert name in runtime_names, f"{name} 有端点定义却没进运行时"
        else:
            assert name not in runtime_names, f"{name} 只是候选,不该进运行时"


def test_parse_total_handles_chinese_magnitudes():
    assert parse_total("实测 19471 个 sdnEntry") == 19471
    assert parse_total("约 50 万项") == 500_000
    assert parse_total("约 1.8 万个") == 18_000
    assert parse_total("实测 9,011,130,327 条三元组") == 9_011_130_327
    assert parse_total("规模约其 1/8") is None
    assert parse_total("") is None


def test_parse_unit():
    assert parse_unit("实测 19471 个 sdnEntry") == "records"
    assert parse_unit("实测 340 张表") == "tables"
    assert parse_unit("") == ""


def test_alias_resolution_covers_common_short_names(registry):
    from a_source_gateway.resolver import Resolver
    r = Resolver(registry)
    for alias, expect in [("sec", "sec_edgar"), ("ctgov", "clinicaltrials_gov_v2"),
                          ("ofac", "ofac_sdn"), ("ch", "uk_companies_house"),
                          ("gleif", "gleif_lei"), ("wdi", "world_bank_open_data_wdi_wgi")]:
        assert r.resolve_source(alias) == expect, f"{alias} 解析错误"
    assert r.resolve_source("根本不存在的源") is None
    assert r.resolve_source("") is None


def test_registry_json_is_serialisable(registry):
    text = json.dumps(registry, ensure_ascii=False)
    assert len(text) > 50_000
    assert json.loads(text)["count"] == registry["count"]
