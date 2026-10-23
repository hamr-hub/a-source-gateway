"""
test_ai_contract.py - 智能体主路径上的契约测试。

这里守的是**"智能体照着 AGENTS.md 做就能做对"**这件事本身:
文档写错一个格式, 智能体就会照着做错, 而且往往不报错 —— 所以要用测试把文档钉住。

覆盖:
  F1  data/aliases.json 的别名必须嵌在 "aliases" 对象里。
      两个加载器(Go / Python)只读 doc["aliases"]; 写成顶层键会被**静默忽略**,
      validate 照样通过 —— 这是最难自查的一类错。
  F7  scaffold_source.py --override 已经填好的列, 不该再出现在 TODO 清单里。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _load(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(f"_asg_test_{path.stem}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── F1: 别名必须嵌在 aliases 对象里 ─────────────────────────────────────
def test_aliases_must_be_nested_under_the_aliases_key(tmp_path: Path):
    """顶层别名键会被两个加载器都忽略 —— 必须明确拒绝, 而不是静默丢弃。"""
    from a_source_gateway.registry import load_aliases

    p = tmp_path / "aliases.json"
    p.write_text(
        json.dumps({"version": "1.0", "aliases": {"Real Source": ["real"]}}, ensure_ascii=False),
        encoding="utf-8",
    )

    # 正确的嵌套形式: 能读出来
    import os

    old = os.environ.get("A_GATEWAY_DATA_DIR")
    os.environ["A_GATEWAY_DATA_DIR"] = str(tmp_path)
    try:
        from a_source_gateway.config import load_config
        cfg = load_config()
        got = load_aliases(cfg)
        assert "Real Source" in got, "嵌套形式读不出来"
        assert got["Real Source"] == ["real"]

        # 错误的顶层形式: 必须读不到(这是 load_aliases 的契约, 用例把它钉住)
        p.write_text(
            json.dumps({"version": "1.0", "Real Source": ["real"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        got2 = load_aliases(cfg)
        assert "Real Source" not in got2, \
            "别名必须写在 aliases 对象里; 顶层键不该被当作别名(否则文档写错也察觉不到)"
    finally:
        if old is None:
            os.environ.pop("A_GATEWAY_DATA_DIR", None)
        else:
            os.environ["A_GATEWAY_DATA_DIR"] = old


def test_real_aliases_file_uses_the_nested_shape():
    """仓库里的 data/aliases.json 本身必须是嵌套的 —— 文档照着它写。"""
    doc = json.loads((REPO_ROOT / "data" / "aliases.json").read_text(encoding="utf-8"))
    assert "aliases" in doc, "aliases.json 顶层必须有 aliases 键"
    assert isinstance(doc["aliases"], dict)
    # 顶层不应残留任何"源名 -> 列表"的裸键
    for key, val in doc.items():
        if key in ("version", "aliases"):
            continue
        assert not (isinstance(val, list) and all(isinstance(x, str) for x in val)), \
            f"aliases.json 顶层出现了裸别名键 {key!r}; 会被加载器静默忽略"


def test_alias_keys_all_bind_to_an_inventory_row():
    """每个别名键都必须能落到某份清单的「数据源」原文上, 否则别名不生效。"""
    names: set[str] = set()
    for csv_name in ("source_inventory.csv", "source_inventory_cn.csv"):
        p = REPO_ROOT / "data" / csv_name
        if not p.is_file():
            continue
        with p.open(encoding="utf-8", newline="") as f:
            names |= {r["数据源"].strip() for r in csv.DictReader(f)}
    doc = json.loads((REPO_ROOT / "data" / "aliases.json").read_text(encoding="utf-8"))
    orphans = [k for k in doc["aliases"] if k not in names]
    assert not orphans, f"别名键在清单里找不到对应行(别名不生效): {orphans}"


# ── F7: --override 填好的列不该再列为 TODO ─────────────────────────────
def test_overridden_columns_are_not_reported_as_todo(tmp_path: Path, monkeypatch):
    """填过的列还出现在 TODO 清单里, 会让人以为自己的输入没生效。"""
    scaffold = _load("scaffold_source.py")

    report = {
        "url": "https://example.org/api/records",
        "final_url": "https://example.org/api/records",
        "status": 200,
        "latency_ms": 120,
        "media_type": "application/json",
        "api_style": "json",
        "auth": {"required": False, "kind": "none", "evidence": ""},
        "rate_limit": {"headers": {}, "declared": None},
        "item_path": "data",
        "item_shape": "list",
        "total_field": "total",
        "fields": ["id"],
        "inference": {},
        "warnings": [],
    }
    recon_path = tmp_path / "recon.json"
    recon_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    class Args:
        family = "七、学术与文献"
        name = "Override Contract 源"
        org = "Org"
        country = "全球"
        summary = "摘要"
        priority = "P2"
        override = {"实测增量": "日增约 10 条(按 created 字段)", "代理需求": "不走代理"}

    columns = ["分类", "数据源", "机构", "国别", "数据内容", "官方入口", "认证方式",
               "官方限速口径", "全量下载", "更新频率", "实测规模", "实测增量",
               "代理需求", "合规风险", "建议采集方式", "链接状态", "探测耗时ms"]
    row, todos = scaffold.build_row(report, Args(), columns)

    # 值确实落进了行里
    assert row["实测增量"] == "日增约 10 条(按 created 字段)"
    assert row["代理需求"] == "不走代理"
    # 但不该再被列为待办
    joined = "\n".join(todos)
    assert "实测增量" not in joined, f"已 override 的列仍被列为 TODO:\n{joined}"
    assert "代理需求" not in joined, f"已 override 的列仍被列为 TODO:\n{joined}"
    # 没 override 的列仍然要提醒
    assert any("别名" in t for t in todos)
