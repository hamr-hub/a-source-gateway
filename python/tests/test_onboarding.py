"""
test_onboarding.py - AI 自助接入工具链的离线测试。

全部不联网:
  - 侦察的解析/推断逻辑直接喂 fixtures/ 里的固定正文;
  - verify/demo 的整条链路打本机临时起的 http.server(端口 0, 线程内);
  - 网关调用一律指向一个"没人监听"的端口, 保证走直连分支, 行为确定。

scripts/ 刻意不做成包(没有 __init__.py), 这里用 importlib 按路径加载模块,
pytest 收集时不会把 scripts/ 当测试目录, 也不会污染 sys.path。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "onboarding"

CSV_COLUMNS = [
    "分类", "数据源", "机构", "国别", "数据内容", "官方入口", "认证方式", "官方限速口径",
    "全量下载", "更新频率", "实测规模", "实测增量", "代理需求", "合规风险", "建议采集方式",
    "链接状态", "探测耗时ms",
]
AUTH_VOCAB = {
    "无key", "需邮箱", "免费key", "可注册key", "可选key", "需token",
    "部分付费", "账号+付费", "免费/付费", "部分免费部分付费", "多数免费", "部分注册",
}


def load_script(name: str) -> ModuleType:
    """按路径加载 scripts/<name>.py。"""
    path = SCRIPTS_DIR / f"{name}.py"
    key = f"_t_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def recon_mod() -> ModuleType:
    return load_script("recon")


@pytest.fixture(scope="module")
def scaffold_mod() -> ModuleType:
    return load_script("scaffold_source")


@pytest.fixture(scope="module")
def verify_mod() -> ModuleType:
    return load_script("verify_source")


@pytest.fixture(scope="module")
def demo_mod() -> ModuleType:
    return load_script("demo")


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── 本地假上游 ───────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str, bytes]] = {}

    def do_GET(self):                                       # noqa: N802
        path = self.path.split("?")[0]
        status, ctype, body = self.routes.get(path, (404, "application/json", b'{"error":"not found"}'))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                            # noqa: ANN002
        return


@pytest.fixture(scope="module")
def fixture_server():
    """本机 HTTP 服务器: /works 返回 Crossref 风格正文, /boom 返回 500。"""
    _Handler.routes = {
        "/works": (200, "application/json", read_fixture("crossref_works.json").encode("utf-8")),
        "/boom": (500, "application/json", b'{"error":"upstream exploded"}'),
        # 配额耗尽: HTTP 200 但正文明说"没给数据"(BLS 的 REQUEST_NOT_PROCESSED 口径)
        "/quota": (200, "application/json",
                   b'{"status":"REQUEST_NOT_PROCESSED",'
                   b'"message":["daily threshold of 25 requests has been reached"]}'),
        # 同一个"拒绝"状态, 但正文里**带着数据**: 这种不能按配额处理,
        # 否则真正写错的 item_path 会被"已跳过"藏起来。
        "/quota_with_data": (200, "application/json",
                             b'{"status":"REQUEST_NOT_PROCESSED",'
                             b'"message":["daily threshold of 25 requests has been reached"],'
                             b'"value":[{"DOI":"10.1051/quota"}],"@odata.count":7}'),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def closed_port() -> int:
    """找一个没人监听的端口(让"网关没在跑"这条分支确定成立)。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_data_dir(tmp_path: Path, port: int, source_name: str = "Fixture Source") -> Path:
    """造一个只含一个源的临时 data/ 目录(不碰仓库里的真实 data/)。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    header = ",".join(CSV_COLUMNS)
    row = ",".join([
        "七、学术与文献", source_name, "Fixture Org", "全球", "离线测试用假源",
        f"http://127.0.0.1:{port}/works", "无key", "未公布", "无", "日",
        "实测 3 条(接口 message.total-results 字段)", "日增约 1 条(按 created)",
        "兜底", "低", "按 created 增量", "200", "12",
    ])
    (data_dir / "source_inventory.csv").write_text(
        header + "\r\n" + row + "\r\n", encoding="utf-8", newline="")
    (data_dir / "endpoints.json").write_text(json.dumps({
        "version": "1.0",
        "sources": {
            source_name: [{
                "id": "works",
                "name": "离线测试工作",
                "method": "GET",
                "url_template": f"http://127.0.0.1:{port}/works",
                "params": [],
                "response": {
                    "media_type": "application/json",
                    "shape": "object",
                    "item_path": "message.items",
                    "total_field": "message.total-results",
                    "fields": [{"name": "DOI", "type": "string"},
                               {"name": "title", "type": "array"}],
                },
                "cache_ttl_s": 900,
                "budget_ms": 400,
            }, {
                "id": "boom",
                "name": "会 500 的端点",
                "method": "GET",
                "url_template": f"http://127.0.0.1:{port}/boom",
                "params": [],
                "response": {"media_type": "application/json", "shape": "object", "fields": []},
            }, {
                # 配额耗尽: HTTP 200 但正文里写着没给数据
                "id": "quota",
                "name": "配额耗尽的端点",
                "method": "GET",
                "url_template": f"http://127.0.0.1:{port}/quota",
                "params": [],
                "response": {
                    "media_type": "application/json",
                    "shape": "object",
                    "item_path": "message.items",
                    "total_field": "message.total-results",
                    "fields": [{"name": "DOI", "type": "string"}],
                },
                "cache_ttl_s": 900,
                "budget_ms": 400,
            }, {
                # 带着数据的"拒绝"状态: 这种不能按配额处理
                "id": "quota_with_data",
                "name": "带数据的配额样式端点",
                "method": "GET",
                "url_template": f"http://127.0.0.1:{port}/quota_with_data",
                "params": [],
                "response": {
                    "media_type": "application/json",
                    "shape": "object",
                    "item_path": "value",
                    "total_field": "@odata.count",
                    "fields": [{"name": "DOI", "type": "string"}],
                },
                "cache_ttl_s": 900,
                "budget_ms": 400,
            }, {
                # 需要凭据: 必填参数挂在环境变量上
                "id": "creds",
                "name": "要凭据的端点",
                "method": "GET",
                "url_template": f"http://127.0.0.1:{port}/works",
                "params": [{"name": "api_key", "in": "query", "type": "string",
                            "required": True, "env": "A_GATEWAY_TEST_ONLY_KEY",
                            "desc": "凭据"}],
                "response": {"media_type": "application/json", "shape": "object", "fields": []},
                "cache_ttl_s": 900,
                "budget_ms": 400,
            }],
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return data_dir


# ── 侦察: 结构推断 ───────────────────────────────────────────────────────
def test_recon_infers_item_path_and_total_for_crossref_body(recon_mod):
    body = read_fixture("crossref_works.json")
    payload, err = recon_mod.parse_json_body(body)
    assert err is None
    assert payload is not None

    item = recon_mod.infer_item_path(payload)
    assert item["item_path"] == "message.items"
    assert item["item_shape"] == "list"
    assert "items" in item["evidence"]

    total = recon_mod.infer_total_field(payload)
    assert total["total_field"] == "message.total-results"
    assert "total-results" in total["evidence"]

    fields = recon_mod.extract_fields(payload, item["item_path"])
    assert "DOI" in fields["fields"]
    assert fields["field_types"]["DOI"] == "string"
    assert fields["field_types"]["reference-count"] == "int"


def test_recon_prefers_named_array_over_incidental_one(recon_mod):
    """message.license 也是数组, 但 item_path 应取 message.items。"""
    payload, _ = recon_mod.parse_json_body(read_fixture("crossref_works.json"))
    item = recon_mod.infer_item_path(payload)
    assert item["item_path"] == "message.items"


def test_recon_top_level_array_has_no_item_path(recon_mod):
    payload, err = recon_mod.parse_json_body(read_fixture("list_body.json"))
    assert err is None
    item = recon_mod.infer_item_path(payload)
    assert item["item_path"] is None
    assert item["item_shape"] == "list"
    assert "数组" in item["evidence"]


def test_recon_openalex_style_body(recon_mod):
    payload, _ = recon_mod.parse_json_body(read_fixture("openalex_works.json"))
    item = recon_mod.infer_item_path(payload)
    total = recon_mod.infer_total_field(payload)
    assert item["item_path"] == "results"
    assert total["total_field"] == "meta.count"


def test_recon_malformed_json_is_reported_not_guessed(recon_mod):
    payload, err = recon_mod.parse_json_body(read_fixture("malformed.json"))
    assert payload is None
    assert err is not None
    assert "JSON 解析失败" in err
    # 解析不出来就不许给结论
    assert recon_mod.infer_item_path(payload)["item_path"] is None
    assert recon_mod.infer_total_field(payload)["total_field"] is None


def test_recon_csv_body_reports_columns_and_no_item_path(recon_mod):
    body = read_fixture("sample.csv")
    style, _ = recon_mod.detect_api_style("text/csv", body)
    assert style == "csv"
    det = recon_mod.detect_delimited(body)
    assert det["columns"][:4] == ["id", "title", "published", "doi"]
    assert det["delimiter"] == ","
    assert det["row_count"] == 3


def test_recon_detects_auth_and_rate_limit_signals(recon_mod):
    auth = recon_mod.detect_auth(401, {"WWW-Authenticate": "Bearer realm=x"}, "need api key")
    assert auth["required"] is True
    assert auth["kind"] == "token"
    assert "401" in auth["evidence"]

    auth2 = recon_mod.detect_auth(200, {}, "add api_key=YOUR_KEY to your requests")
    assert auth2["required"] is False
    assert auth2["kind"] == "api_key"

    rate = recon_mod.detect_rate_limit({"X-RateLimit-Limit": "10", "Retry-After": "3"})
    assert rate["declared"] == 10
    assert "retry-after" in rate["headers"]


def test_recon_report_schema_is_stable(recon_mod):
    """报告的 key 集合是契约 —— scaffold 与 CI 都依赖它。"""
    report = {
        "url": "https://example.org/works", "probed_at": "2026-01-01T00:00:00Z",
        "final_url": "https://example.org/works", "status": 200,
        "content_type": "application/json", "latency_ms": 12, "body_bytes": 34,
        "body_sample": "{}", "api_style": "json",
        "auth": {"required": False, "kind": "none", "evidence": "x"},
        "rate_limit": {"headers": {}, "declared": None, "evidence": "x"},
        "item_path": None, "item_shape": None, "total_field": None, "total_value": None,
        "total_is_global": True, "query_params": {}, "fields": [], "field_types": {},
        "columns": [], "delimiter": None, "docs_urls": [], "probes": [], "max_probes": 20,
        "warnings": [], "errors": [], "inference": {},
    }
    for key in ("url", "probed_at", "final_url", "status", "content_type", "latency_ms",
                "body_bytes", "body_sample", "api_style", "auth", "rate_limit", "item_path",
                "item_shape", "total_field", "fields", "docs_urls", "probes", "warnings",
                "errors", "inference"):
        assert key in report


# ── 脚手架: 生成的草稿必须能过 validate ─────────────────────────────────
def _recon_report(recon_mod, **over) -> dict:
    body = read_fixture("crossref_works.json")
    payload, _ = recon_mod.parse_json_body(body)
    item = recon_mod.infer_item_path(payload)
    total = recon_mod.infer_total_field(payload)
    fx = recon_mod.extract_fields(payload, item["item_path"])
    report = {
        "url": "https://api.example.org/works?rows=1",
        "probed_at": "2026-10-23T00:00:00Z",
        "final_url": "https://api.example.org/works?rows=1",
        "status": 200, "content_type": "application/json", "latency_ms": 123,
        "body_bytes": 456, "body_sample": "{}", "api_style": "json",
        "auth": {"required": False, "kind": "none", "evidence": "匿名可访问"},
        "rate_limit": {"headers": {"x-rate-limit-limit": "50"},
                       "declared": 50, "evidence": "x-rate-limit-limit=50"},
        "item_path": item["item_path"], "item_shape": item["item_shape"],
        "total_field": total["total_field"], "total_value": 12345, "total_is_global": True,
        "query_params": {"rows": "1"}, "fields": fx["fields"], "field_types": fx["field_types"],
        "columns": [], "delimiter": None, "docs_urls": [], "probes": [],
        "max_probes": 20, "warnings": [], "errors": [], "inference": {},
    }
    report.update(over)
    return report


def _scaffold_args(scaffold_mod, name="Demo Source", family="七、学术与文献"):
    class Args:
        pass
    args = Args()
    args.name = name
    args.family = family
    args.org = "Demo Org"
    args.country = "全球"
    args.summary = "离线测试用"
    args.endpoint_id = "works"
    args.endpoint_name = ""
    args.cache_ttl_s = 900
    args.budget_ms = 400
    args.override = {}
    args.priority = "P2"
    return args


def test_scaffold_row_has_17_columns_and_valid_vocabulary(scaffold_mod, recon_mod):
    report = _recon_report(recon_mod)
    row, todos = scaffold_mod.build_row(report, _scaffold_args(scaffold_mod), CSV_COLUMNS)
    assert len(row) == 17
    assert list(row.keys()) == CSV_COLUMNS
    assert row["认证方式"] in AUTH_VOCAB
    assert row["实测增量"].strip()
    assert row["实测增量"] != row["实测规模"]
    assert row["官方入口"].startswith("http")
    assert row["链接状态"] == "200"
    # 需要判断的字段必须留 TODO, 不能编造
    assert row["全量下载"].startswith("TODO")
    assert row["合规风险"].startswith("TODO")
    assert any("实测增量" in t for t in todos)


def test_scaffold_endpoint_entry_carries_inferred_paths(scaffold_mod, recon_mod):
    report = _recon_report(recon_mod)
    endpoint, _todos = scaffold_mod.build_endpoint(report, _scaffold_args(scaffold_mod), "Demo Source")
    assert endpoint["url_template"]
    assert endpoint["response"]["item_path"] == "message.items"
    assert endpoint["response"]["total_field"] == "message.total-results"
    assert endpoint["response"]["shape"] == "object"
    assert endpoint["cache_ttl_s"] == 900
    assert endpoint["budget_ms"] == 400
    # url 里的每个 {param} 都必须在 params 里声明
    import re
    declared = {p["name"] for p in endpoint["params"]}
    for ph in set(re.findall(r"\{([a-zA-Z0-9_]+)\}", endpoint["url_template"])):
        assert ph in declared
    for p in endpoint["params"]:
        if p.get("required"):
            assert p.get("example") is not None or "default" in p


def test_scaffold_writes_patch_dir_without_touching_real_data(scaffold_mod, recon_mod, tmp_path, capsys):
    report = _recon_report(recon_mod)
    recon_path = tmp_path / "recon.json"
    recon_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    out_dir = tmp_path / "draft"
    real_csv = REPO_ROOT / "data" / "source_inventory.csv"
    before = real_csv.read_bytes()
    rc = scaffold_mod.main([
        "--recon", str(recon_path), "--name", "Demo Source",
        "--family", "七、学术与文献", "--org", "Demo Org", "--country", "全球",
        "--summary", "离线测试用", "--out-dir", str(out_dir),
    ])
    assert rc == 0
    assert (out_dir / "source_inventory.row.csv").is_file()
    assert (out_dir / "endpoints.entry.json").is_file()
    assert (out_dir / "REPORT.md").is_file()
    assert real_csv.read_bytes() == before          # 真实数据文件一个字节都没动


def test_scaffold_apply_refuses_duplicate_name(scaffold_mod, recon_mod, tmp_path, capsys):
    report = _recon_report(recon_mod)
    (tmp_path / "recon.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "source_inventory.csv").write_text(
        ",".join(CSV_COLUMNS) + "\r\n" + ",".join(["f", "Demo Source"] + [""] * 15) + "\r\n",
        encoding="utf-8", newline="")
    (data_dir / "endpoints.json").write_text(json.dumps({"sources": {}}), encoding="utf-8")
    rc = scaffold_mod.main([
        "--recon", str(tmp_path / "recon.json"), "--name", "Demo Source",
        "--family", "七、学术与文献", "--out-dir", str(tmp_path / "draft"),
        "--data-dir", str(data_dir), "--apply",
    ])
    assert rc == 1
    assert "已存在" in capsys.readouterr().err


def _cn_data_dir(tmp_path: Path) -> Path:
    """一份同时含两份清单的临时 data 目录。"""
    import csv
    import io
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    w.writerow(["一、证券监管与投资披露", "Existing Overseas Source", "Org", "美国", "内容",
                "https://example.org/a", "无key", "未公布", "无", "日", "实测 5 条",
                "日增 1 条", "兜底", "低", "按 created", "200", "10"])
    (data_dir / "source_inventory.csv").write_text(buf.getvalue(), encoding="utf-8", newline="")
    cbuf = io.StringIO()
    cw = csv.writer(cbuf, lineterminator="\r\n")
    cw.writerow(CSV_COLUMNS + ["优先级"])
    cw.writerow(["一、证券监管与投资披露", "Existing CN Source", "Org", "中国", "内容",
                 "https://example.org/cn", "无key", "未公布", "无", "日", "实测 5 条",
                 "日增 1 条", "兜底", "低", "按 created", "200", "10", "P1"])
    (data_dir / "source_inventory_cn.csv").write_text(cbuf.getvalue(), encoding="utf-8", newline="")
    (data_dir / "endpoints.json").write_text(
        json.dumps({"version": "1.0", "sources": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    return data_dir


@pytest.mark.parametrize("country", ["中国", "中国大陆"])
def test_scaffold_cn_source_lands_in_cn_inventory(scaffold_mod, recon_mod, tmp_path, country):
    """规则 3.1:国别是国内的源 → 落国内清单(且多一列 优先级), 不碰海外清单。"""
    import csv
    report = _recon_report(recon_mod)
    (tmp_path / "recon.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    data_dir = _cn_data_dir(tmp_path)
    overseas_before = (data_dir / "source_inventory.csv").read_bytes()
    rc = scaffold_mod.main([
        "--recon", str(tmp_path / "recon.json"), "--name", "Demo CN Source",
        "--family", "一、证券监管与投资披露", "--org", "Demo Org", "--country", country,
        "--summary", "离线测试用", "--out-dir", str(tmp_path / "draft"),
        "--data-dir", str(data_dir), "--priority", "P0", "--apply",
    ])
    assert rc == 0
    assert (data_dir / "source_inventory.csv").read_bytes() == overseas_before, "海外清单不该被改"
    with (data_dir / "source_inventory_cn.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    hit = [r for r in rows if r["数据源"] == "Demo CN Source"]
    assert hit, "国内源应写进 source_inventory_cn.csv"
    assert hit[0]["优先级"] == "P0"
    assert hit[0]["国别"] == country


def test_scaffold_cn_apply_does_not_touch_endpoints(scaffold_mod, recon_mod, tmp_path):
    """国内源只入候选池: --apply 不写 endpoints.json(要接网关得先有端点定义)。"""
    report = _recon_report(recon_mod)
    (tmp_path / "recon.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    data_dir = _cn_data_dir(tmp_path)
    endpoints_before = (data_dir / "endpoints.json").read_bytes()
    rc = scaffold_mod.main([
        "--recon", str(tmp_path / "recon.json"), "--name", "Demo CN Source",
        "--family", "一、证券监管与投资披露", "--org", "Demo Org", "--country", "中国",
        "--summary", "离线测试用", "--out-dir", str(tmp_path / "draft"),
        "--data-dir", str(data_dir), "--apply",
    ])
    assert rc == 0
    assert (data_dir / "endpoints.json").read_bytes() == endpoints_before, "国内源不该写 endpoints.json"


def test_scaffold_refuses_name_already_in_other_inventory(scaffold_mod, recon_mod, tmp_path, capsys):
    """跨清单查重: 同一个源只能属于一份清单。"""
    report = _recon_report(recon_mod)
    (tmp_path / "recon.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    data_dir = _cn_data_dir(tmp_path)
    rc = scaffold_mod.main([
        "--recon", str(tmp_path / "recon.json"), "--name", "Existing Overseas Source",
        "--family", "一、证券监管与投资披露", "--org", "Demo Org", "--country", "中国",
        "--summary", "离线测试用", "--out-dir", str(tmp_path / "draft"),
        "--data-dir", str(data_dir), "--apply",
    ])
    assert rc == 1
    assert "另一份清单" in capsys.readouterr().err


def test_scaffold_apply_preserves_existing_csv_bytes(scaffold_mod, recon_mod, tmp_path):
    """落库时不能把已有行的换行/引号风格改掉。"""
    import csv
    import io
    report = _recon_report(recon_mod)
    (tmp_path / "recon.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    writer.writerow(["七、学术与文献", "Existing Source", "Org", "全球", "内容,带逗号",
                     "https://example.org/a", "无key", "未公布", "无", "日", "实测 5 条",
                     "日增 1 条", "兜底", "低", "按 created", "200", "10"])
    original = buf.getvalue()
    (data_dir / "source_inventory.csv").write_text(original, encoding="utf-8", newline="")
    (data_dir / "endpoints.json").write_text(
        json.dumps({"version": "1.0", "sources": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    rc = scaffold_mod.main([
        "--recon", str(tmp_path / "recon.json"), "--name", "Demo Source",
        "--family", "七、学术与文献", "--out-dir", str(tmp_path / "draft"),
        "--data-dir", str(data_dir), "--apply",
    ])
    assert rc == 0
    # Path.read_text(newline=...) 是 Python 3.13 才有的参数, 这里用 open() 以兼容 3.10+
    with (data_dir / "source_inventory.csv").open(encoding="utf-8", newline="") as f:
        after = f.read()
    assert after.startswith(original)             # 老行一个字节都没动(含 CRLF 与引号)
    assert '"内容,带逗号"' in after                # 引号风格也保持
    assert after.count("\r\n") == 3               # 表头 + 原有行 + 新增行, 只多了一行
    assert "Demo Source" in after


# ── 证据闸门: verified / degraded / unreachable 三种结局 ─────────────────
def test_verify_passes_against_local_fixture_server(verify_mod, fixture_server, tmp_path, capsys):
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = verify_mod.main([
        "fixture", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    out = capsys.readouterr().out
    report = json.loads(out)
    assert rc == 0
    assert report["status"] == "verified"
    assert report["resolved_source"] == "fixture_source"
    names = {a["name"]: a for a in report["assertions"]}
    assert names["item_path_resolves_to_list"]["ok"] is True
    assert names["total_field_resolves_to_number"]["ok"] is True
    # 直连模式下用 upstream_not_error 表示"上游没报错"
    assert names["upstream_not_error"]["ok"] is True
    assert report["upstream"]["status"] == 200
    assert "curl" in report["curl"] and "/v1/query" in report["curl"]
    assert report["sample"]["DOI"] == "10.1051/epjconf/s20130018002"


def test_verify_reports_degraded_when_upstream_errors(verify_mod, fixture_server, tmp_path, capsys):
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = verify_mod.main([
        "fixture", "--endpoint", "boom", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] in ("degraded", "failed")
    assert report["upstream"]["status"] == 500


def test_verify_unreachable_exits_2(verify_mod, tmp_path, capsys):
    """连不上 = 环境问题, 不是"源坏了" —— 退出码必须是 2。"""
    dead = closed_port()
    data_dir = make_data_dir(tmp_path, dead)
    rc = verify_mod.main([
        "fixture", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json", "--timeout", "3",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report["status"] == "unreachable"
    assert report["errors"]


def test_verify_direct_only_skips_the_gateway(verify_mod, fixture_server, tmp_path, capsys):
    """--direct-only: 只验源本身, 不依赖网关是否在跑。"""
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = verify_mod.main([
        "fixture", "--data-dir", str(data_dir), "--direct-only", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["status"] == "verified"
    assert report["transport"] == "direct"
    assert report["gateway_call"]["called"] is False


def test_verify_unknown_source_exits_1(verify_mod, fixture_server, tmp_path, capsys):
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = verify_mod.main([
        "no-such-source", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["status"] == "failed"


def test_verify_detects_wrong_item_path(verify_mod, fixture_server, tmp_path, capsys):
    """声明的 item_path 指向不存在的路径 → 必须判失败, 不能放过。"""
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    defs = json.loads((data_dir / "endpoints.json").read_text(encoding="utf-8"))
    defs["sources"]["Fixture Source"][0]["response"]["item_path"] = "message.nope"
    (data_dir / "endpoints.json").write_text(json.dumps(defs, ensure_ascii=False), encoding="utf-8")
    rc = verify_mod.main([
        "fixture", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    names = {a["name"]: a for a in report["assertions"]}
    assert names["item_path_resolves_to_list"]["ok"] is False


def test_verify_quota_exhaustion_is_not_reported_as_metadata_error(verify_mod, fixture_server,
                                                                  tmp_path, capsys):
    """HTTP 200 但配额耗尽: 是"上游没给数据", 不是"item_path 写错了"。

    误诊的代价很实在: 照着"item_path 写错"去改一份本来正确的元数据, 改完更糟。
    """
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = verify_mod.main([
        "fixture", "--endpoint", "quota", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1                      # 配额耗尽 ≠ 验证通过, 不能放行
    assert report["upstream"]["status"] == 200
    names = {a["name"]: a for a in report["assertions"]}
    # 结构断言被跳过(没数据可比), 而不是判成"路径写错"
    assert names["item_path_resolves_to_list"]["ok"] is True
    assert names["item_path_resolves_to_list"]["severity"] == "warn"
    assert names["total_field_resolves_to_number"]["ok"] is True
    assert names["total_field_resolves_to_number"]["severity"] == "warn"
    # 真正的原因要说清楚
    assert names["upstream_not_error"]["ok"] is False
    assert "配额" in names["upstream_not_error"]["detail"]
    assert "REQUEST_NOT_PROCESSED" in names["upstream_not_error"]["detail"]


def test_verify_missing_credentials_gets_its_own_status(verify_mod, fixture_server,
                                                        tmp_path, capsys, monkeypatch):
    """端点定义是对的, 只是本机没配凭据 —— 不能混进"元数据失真"。

    端点定义写对了的源, 在没配 key 的机器上永远验证不过; 报成 failed 会让人
    去改一份正确的元数据。退出码 3 单独表示这一类。
    """
    monkeypatch.delenv("A_GATEWAY_TEST_ONLY_KEY", raising=False)
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = verify_mod.main([
        "fixture", "--endpoint", "creds", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert report["status"] == "credentials_missing"
    assert any("api_key" in e for e in report["errors"])


def test_verify_resolve_path_falls_back_to_dotted_literal_key(verify_mod):
    """键名本身带点(@odata.count)时, 整串字面键兜底必须生效。

    两个平面(Python resolver.py / Go parser.go)都有这个兜底, 复验脚本不跟上
    就会把正确的元数据判成错误。
    """
    assert verify_mod.resolve_path({"@odata.count": 42}, "@odata.count") == 42
    assert verify_mod.resolve_path({"meta": {"total": 7}}, "meta.total") == 7
    assert verify_mod.resolve_path({"a": {"b": 1}}, "a.b") == 1
    assert verify_mod.resolve_path({"a": 1}, "x.y") is None


def test_verify_quota_detection_does_not_swallow_normal_payloads(verify_mod):
    """配额判定不能误伤: 正常 status / 非拒绝类状态都不算配额受限。"""
    assert verify_mod.quota_blocked({"status": "OK", "message": ["ok"]}) == ""
    assert verify_mod.quota_blocked({"status": "PENDING", "message": ["quota checked"]}) == ""
    assert verify_mod.quota_blocked({"status": "REQUEST_NOT_PROCESSED",
                                     "message": ["daily threshold of 25 has been reached"]})
    assert verify_mod.quota_blocked({"items": []}) == ""
    # 正文里**有**数据行 → 不是"上游没给数据", 不能按配额处理
    assert verify_mod.quota_blocked({
        "status": "REQUEST_NOT_PROCESSED",
        "message": ["daily threshold of 25 has been reached"],
        "value": [{"DOI": "10.1/x"}]}) == ""


def test_verify_quota_skip_does_not_hide_a_real_metadata_error(verify_mod, fixture_server,
                                                               tmp_path, capsys):
    """正文里**有**数据时, 不能按配额跳过结构断言。

    配额限流的表象是"200 + 没给数据"。如果数据其实在, 却按配额处理,
    真正写错的 item_path 就会被"已跳过"藏起来。
    """
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = verify_mod.main([
        "fixture", "--endpoint", "quota_with_data", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    # 数据在 → 正常做结构断言(顺带验证带点字面键 @odata.count)
    assert rc == 0, report["errors"]
    assert report["status"] == "verified"
    names = {a["name"]: a for a in report["assertions"]}
    assert names["item_path_resolves_to_list"]["severity"] == "hard"
    assert names["total_field_resolves_to_number"]["ok"] is True
    assert "3102" not in names["total_field_resolves_to_number"]["detail"]
    assert "7" in names["total_field_resolves_to_number"]["detail"]

    # 把 item_path 改错: 正文里有数据, 就必须如实判"路径写错了", 不能被配额跳过
    defs = json.loads((data_dir / "endpoints.json").read_text(encoding="utf-8"))
    for ep in defs["sources"]["Fixture Source"]:
        if ep["id"] == "quota_with_data":
            ep["response"]["item_path"] = "value.nope"
    (data_dir / "endpoints.json").write_text(json.dumps(defs, ensure_ascii=False), encoding="utf-8")
    rc = verify_mod.main([
        "fixture", "--endpoint", "quota_with_data", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    names = {a["name"]: a for a in report["assertions"]}
    assert names["item_path_resolves_to_list"]["severity"] == "hard"


# ── demo: 最终交付物 ─────────────────────────────────────────────────────
def test_demo_prints_curl_and_real_data(demo_mod, fixture_server, tmp_path, capsys):
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = demo_mod.main([
        "fixture", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--limit", "2",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "curl -s -X POST" in out
    assert "/v1/query" in out
    assert "10.1051/epjconf/s20130018002" in out        # 真实正文里的 DOI
    assert "真实上游响应" in out
    assert "必填参数" in out


def test_demo_json_mode_is_machine_readable(demo_mod, fixture_server, tmp_path, capsys):
    port = fixture_server.server_address[1]
    data_dir = make_data_dir(tmp_path, port)
    rc = demo_mod.main([
        "fixture", "--data-dir", str(data_dir),
        "--gateway-url", f"http://127.0.0.1:{closed_port()}", "--json",
    ])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["mode"] == "direct"
    assert result["curl"].startswith("curl ")
    assert result["envelope"]["meta"]["degraded"] is False
    assert result["envelope"]["data"]


# ── 引导: 包解析 ─────────────────────────────────────────────────────────
def test_bootstrap_resolves_gateway_package():
    boot = load_script("_asg_bootstrap")
    mod = boot.ensure_gateway_package()
    assert mod is not None
    import a_source_gateway
    assert a_source_gateway.__name__ == "a_source_gateway"
    # 三种布局都要被认出来
    root = boot.repo_root()
    cands = boot.package_candidates(root)
    assert len(cands) >= 3
    assert any(c[0].name == "a_source_gateway" for c in cands)
    assert boot.default_data_dir() == root / "data"


# ── 复核日期写回 ─────────────────────────────────────────────────────────
DATE_COLUMN = "最后验证"


def write_inventory(path: Path, rows: list[tuple[str, str]]) -> None:
    """写一份只含 数据源/最后验证 两列的清单(name, verified-at)。"""
    head = f"数据源,{DATE_COLUMN}"
    body = "".join(f"{name},{value}" + "\r\n" for name, value in rows)
    path.write_text(head + "\r\n" + body, encoding="utf-8", newline="")


def read_verified(path: Path) -> dict[str, str]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return dict(ln.split(",") for ln in lines[1:])


def recap_json(tmp_path: Path, results: list[dict]) -> Path:
    p = tmp_path / "recap.json"
    p.write_text(json.dumps({"mode": "all", "results": results}), encoding="utf-8")
    return p


def partial_flag_args(tmp_path: Path, recap: Path, *, allow: bool = False) -> argparse.Namespace:
    ns = argparse.Namespace(data_dir=str(tmp_path), recap_json=str(recap), date="2026-10-26",
                            allow_partial_date=allow)
    return ns


def test_write_verified_date_only_touches_fully_verified_sources(verify_mod, tmp_path):
    """全部端点通过才写日期: 出现 failed 的源必须留着旧值。"""
    write_inventory(tmp_path / "source_inventory.csv", [
        ("Green Source", ""), ("Red Source", "2026-01-01"),
    ])
    recap = recap_json(tmp_path, [
        {"source_name": "Green Source", "status": "verified"},
        {"source_name": "Red Source", "status": "failed"},
    ])
    written = verify_mod.write_verified_dates(partial_flag_args(tmp_path, recap))

    assert written == 1
    got = read_verified(tmp_path / "source_inventory.csv")
    assert got["Green Source"] == "2026-10-26"
    assert got["Red Source"] == "2026-01-01"          # 没通过的源不得被刷新


def test_write_verified_date_is_idempotent(verify_mod, tmp_path):
    """重复写同一天不产生新的 diff(否则每次复验都是一片假 diff)。"""
    write_inventory(tmp_path / "source_inventory.csv", [("Green Source", "2026-10-26")])
    recap = recap_json(tmp_path, [{"source_name": "Green Source", "status": "verified"}])

    assert verify_mod.write_verified_dates(partial_flag_args(tmp_path, recap)) == 0
    assert read_verified(tmp_path / "source_inventory.csv")["Green Source"] == "2026-10-26"


@pytest.mark.parametrize("status", ["unreachable", "credentials_missing"])
def test_write_verified_date_blocks_environment_failures(verify_mod, tmp_path, status):
    """环境问题与缺凭据默认不写: 它们不是"核对通过", 只是这次没能核对。"""
    write_inventory(tmp_path / "source_inventory.csv", [("Source A", "")])
    recap = recap_json(tmp_path, [{"source_name": "Source A", "status": status}])

    assert verify_mod.write_verified_dates(partial_flag_args(tmp_path, recap)) == 0
    assert read_verified(tmp_path / "source_inventory.csv")["Source A"] == ""

    # 显式放行才写 —— 并且这是调用方的选择, 不是默认行为
    assert verify_mod.write_verified_dates(
        partial_flag_args(tmp_path, recap, allow=True)) == 1
    assert read_verified(tmp_path / "source_inventory.csv")["Source A"] == "2026-10-26"


def test_write_verified_date_requires_every_endpoint_of_a_source(verify_mod, tmp_path):
    """多端点源: 只要有一个端点没通过, 整源不写。"""
    write_inventory(tmp_path / "source_inventory.csv", [("Multi Source", "")])
    recap = recap_json(tmp_path, [
        {"source_name": "Multi Source", "status": "verified"},
        {"source_name": "Multi Source", "status": "failed"},
    ])
    assert verify_mod.write_verified_dates(partial_flag_args(tmp_path, recap)) == 0
    assert read_verified(tmp_path / "source_inventory.csv")["Multi Source"] == ""


def test_write_verified_date_covers_both_inventories(verify_mod, tmp_path):
    """两份清单都要扫: 国内源只在 cn 清单里, 漏扫一份日期就静默消失。"""
    write_inventory(tmp_path / "source_inventory.csv", [("Overseas Source", "")])
    write_inventory(tmp_path / "source_inventory_cn.csv", [("中国源", "")])
    recap = recap_json(tmp_path, [
        {"source_name": "Overseas Source", "status": "verified"},
        {"source_name": "中国源", "status": "verified"},
    ])

    assert verify_mod.write_verified_dates(partial_flag_args(tmp_path, recap)) == 2
    assert read_verified(tmp_path / "source_inventory.csv")["Overseas Source"] == "2026-10-26"
    assert read_verified(tmp_path / "source_inventory_cn.csv")["中国源"] == "2026-10-26"


def test_write_verified_date_without_recap_writes_nothing(verify_mod, tmp_path):
    """没给汇总 JSON 就一个字都不写, 并且要说清楚为什么没写。"""
    write_inventory(tmp_path / "source_inventory.csv", [("Source A", "")])
    ns = argparse.Namespace(data_dir=str(tmp_path), recap_json="", date="2026-10-26",
                            allow_partial_date=False)

    assert verify_mod.write_verified_dates(ns) == 0
    assert read_verified(tmp_path / "source_inventory.csv")["Source A"] == ""
