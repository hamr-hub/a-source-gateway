"""
test_validate_guards.py - `validate` 的护栏测试。

这里守的是两类"看起来没问题、实际上会让清单变脏"的问题:
  1. scaffold_source.py 生成的 TODO(...) 占位符 —— 占位符不是结论, 不能入库;
  2. aliases.json 的键与 CSV 的「数据源」原文对不上 —— 这类别名永远不生效(历史 bug:
     "HM Land Registry Price Paid" vs CSV 里的 "HMLR Price Paid Data");
     以及同一个别名被两个源占用 —— 解析会有歧义。
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
VALIDATE = [sys.executable, "-m", "a_source_gateway.cli", "validate"]


def _run_validate(data_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, A_GATEWAY_DATA_DIR=str(data_dir))
    return subprocess.run(VALIDATE, cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True)


@pytest.fixture()
def data_copy(tmp_path: Path) -> Path:
    """data/ 的一份可改副本 —— 绝不碰真实数据文件。"""
    dst = tmp_path / "data"
    shutil.copytree(DATA_DIR, dst)
    return dst


def _write_row(data_dir: Path, row: dict[str, str]) -> None:
    """在 CSV 末尾追加一行, 保持原有列顺序与换行符。"""
    path = data_dir / "source_inventory.csv"
    raw = path.read_bytes()
    terminator = "\r\n" if b"\r\n" in raw else "\n"
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()), lineterminator=terminator)
        w.writerow(row)


def _base_row() -> dict[str, str]:
    return {
        "分类": "七、学术与文献",
        "数据源": "Test Source 验证用源",
        "机构": "Test Org",
        "国别": "全球",
        "数据内容": "仅用于 validate 护栏测试的临时条目",
        "官方入口": "https://example.org/api/records",
        "认证方式": "无key",
        "官方限速口径": "10次/秒",
        "全量下载": "JSON 导出",
        "更新频率": "daily",
        "实测规模": "实测 1234 条(接口 total 字段)",
        "实测增量": "日增约 10 条(按 created 字段)",
        "代理需求": "兜底",
        "合规风险": "低",
        "建议采集方式": "按 created 字段增量",
        "链接状态": "200",
        "探测耗时ms": "100",
    }


# ── 真实数据必须是干净的 ──────────────────────────────────────────────
def test_real_data_has_no_problems():
    r = _run_validate(DATA_DIR)
    assert r.returncode == 0, f"真实数据校验失败:\n{r.stdout}\n{r.stderr}"
    assert "校验通过" in r.stdout


# ── 护栏 1:TODO 占位符 ────────────────────────────────────────────────
def test_todo_placeholder_is_rejected(data_copy: Path):
    row = _base_row()
    row["实测增量"] = "TODO(待填:日增量 + 取法)"
    _write_row(data_copy, row)
    r = _run_validate(data_copy)
    assert r.returncode == 1
    assert "占位符" in r.stdout
    assert "实测增量" in r.stdout


def test_todo_placeholder_in_any_column_is_rejected(data_copy: Path):
    row = _base_row()
    row["合规风险"] = "TODO(待填:人工定级: 低/中/高)"
    _write_row(data_copy, row)
    r = _run_validate(data_copy)
    assert r.returncode == 1
    assert "合规风险" in r.stdout


def test_filled_row_is_accepted(data_copy: Path):
    """同样的行,占位符补齐后就该通过 —— 证明护栏拦的是占位符,不是新行。"""
    _write_row(data_copy, _base_row())
    r = _run_validate(data_copy)
    assert r.returncode == 0, f"补齐后仍校验失败:\n{r.stdout}\n{r.stderr}"
    assert "校验通过" in r.stdout


# ── 护栏 2:别名键与 CSV 源名对不上 ────────────────────────────────────
def test_orphan_alias_key_is_rejected(data_copy: Path):
    p = data_copy / "aliases.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["aliases"]["这个源在 CSV 里不存在"] = ["ghostalias"]
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    r = _run_validate(data_copy)
    assert r.returncode == 1
    assert "aliases.json" in r.stdout


def test_alias_collision_between_sources_is_rejected(data_copy: Path):
    p = data_copy / "aliases.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    keys = list(doc["aliases"])
    assert len(keys) >= 2
    doc["aliases"][keys[1]].append(doc["aliases"][keys[0]][0])
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    r = _run_validate(data_copy)
    assert r.returncode == 1
    assert "占用" in r.stdout


def test_real_aliases_all_bind_to_a_csv_row():
    """真实别名表里的每个键都必须能在某份清单里找到同名的「数据源」。"""
    names: set[str] = set()
    for p in (DATA_DIR / "source_inventory.csv", DATA_DIR / "source_inventory_cn.csv"):
        if not p.is_file():
            continue
        with p.open(encoding="utf-8", newline="") as f:
            names |= {r["数据源"].strip() for r in csv.DictReader(f)}
    doc = json.loads((DATA_DIR / "aliases.json").read_text(encoding="utf-8"))
    orphans = [k for k in doc["aliases"] if k not in names]
    assert not orphans, f"别名键与清单源名对不上(别名不生效): {orphans}"


# ── 护栏 3:两份清单的分工(规则 3.1) ────────────────────────────────────
def test_cn_inventory_shape_matches_overseas_contract():
    """国内清单:前 17 列与海外清单同名同序,第 18 列是 优先级。"""
    with (DATA_DIR / "source_inventory.csv").open(encoding="utf-8", newline="") as f:
        overseas_header = next(csv.reader(f))
    cn_path = DATA_DIR / "source_inventory_cn.csv"
    assert cn_path.is_file(), "国内清单必须存在"
    with cn_path.open(encoding="utf-8", newline="") as f:
        cn_rows = list(csv.reader(f))
    assert cn_rows[0][:len(overseas_header)] == overseas_header, "前 17 列必须与海外清单一致"
    assert cn_rows[0][len(overseas_header):] == ["优先级"], "第 18 列必须是 优先级"
    for r in cn_rows[1:]:
        assert r[3].strip() == "中国", f"{r[1]}: 国内清单里只允许『中国』国别"
        assert r[-1] in ("P0", "P1", "P2"), f"{r[1]}: 优先级只能是 P0/P1/P2"


def test_overseas_inventory_has_no_chinese_rows(data_copy: Path):
    """海外清单里出现『中国』国别 → 报错(该源应落国内清单)。"""
    row = _base_row()
    row["数据源"] = "Test Source 误放国内源"
    row["国别"] = "中国"
    _write_row(data_copy, row)
    r = _run_validate(data_copy)
    assert r.returncode == 1
    assert "中国" in r.stdout
    assert "source_inventory_cn.csv" in r.stdout


def test_same_source_in_both_inventories_is_rejected(data_copy: Path):
    """同一个源同时出现在两份清单 → 报错(只能有一份, 否则字段会漂)。"""
    with (data_copy / "source_inventory_cn.csv").open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader)                      # 表头
        first = next(reader)              # 国内清单的第一行
    # 把国内清单的第一行(去掉第 18 列 优先级)塞进海外清单 → 同一个源出现在两份清单
    raw = (data_copy / "source_inventory.csv").read_bytes()
    terminator = "\r\n" if b"\r\n" in raw else "\n"
    with (data_copy / "source_inventory.csv").open("a", encoding="utf-8", newline="") as f:
        csv.writer(f, lineterminator=terminator).writerow(first[:-1])
    r = _run_validate(data_copy)
    assert r.returncode == 1
    assert "同时出现" in r.stdout
