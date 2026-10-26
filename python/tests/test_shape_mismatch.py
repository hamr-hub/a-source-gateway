"""
test_shape_mismatch.py - 形状不匹配必须显式降级(csv/tsv/jsonl 拿到 XML/HTML 错误页)。

背景: 1.3.0 给 `json` 形状修过"上游返回的不是 JSON"(upstream_not_json),
但 csv/tsv/jsonl 没跟上 —— 网关对这两种形状是**静默兜底**的:

    csv/tsv -> 把错误页当**数据行**返回(degraded=false, data=[["<!DOCTYPE html>"], ...])
    jsonl   -> 跳过坏行得到**空列表**(degraded=false, data=[])

于是调用方看到的是一次"成功": 分不清"确实没有数据"和"上游给了 HTML 错误页"。
本文件盯住这条规则的三条边界:

  1. 命中就必须报错(R3: degraded + parse_error + 直连指引 + data=[]);
  2. 不能误伤合法数据(R8: 字段以 < 开头、部分行坏、空正文都不是命中);
  3. 文案逐字相同(R4)—— 两个平面给调用方看的话必须是同一句。

三份解析实现(参照面 resolver / parser worker / stdlib 镜像)在这里都要过;
它们之间的"同一份正文同一个结论"由 test_mirror_parity.py 守住。
"""
from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from a_source_gateway.config import load_config
from a_source_gateway.parser_worker import NotShapeError as WorkerNotShapeError
from a_source_gateway.parser_worker import handle_request
from a_source_gateway.parser_worker import parse_body as worker_parse_body
from a_source_gateway.resolver import Resolver, UpstreamNotShapeError
from a_source_gateway.schema import QueryRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIRROR_PATH = REPO_ROOT / "scripts" / "_asg_mirror.py"

# 上游给的典型错误页(WAF / 网关错误页 / 维护页)
HTML_BODY = "<!DOCTYPE html>\n<html><body>503 Service Unavailable</body></html>"
# 短正文: 三份实现的正文片段算法在它上面给出同样的结果, 文案可以逐字断言
SHORT_HTML = "<!DOCTYPE html>\n<html>"


def load_mirror() -> ModuleType:
    """按路径加载 scripts/_asg_mirror.py(它刻意不是包)。"""
    key = "_t_asg_mirror_shape"
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


@pytest.fixture(scope="module")
def resolver() -> Resolver:
    """不需要注册表数据的 Resolver: 解析只吃正文与形状。"""
    return Resolver({"sources": []}, cfg=load_config())


# R4 —— 文案逐字固定(两个平面给调用方看的话必须是同一句)
EXPECTED_MESSAGE = {
    "csv": "上游返回的不是 CSV(疑似 XML/HTML 错误页); 正文片段: <!DOCTYPE html> <html>",
    "tsv": "上游返回的不是 TSV(疑似 XML/HTML 错误页); 正文片段: <!DOCTYPE html> <html>",
    "jsonl": "上游返回的不是 JSONL(疑似 HTML 错误页); 正文片段: <!DOCTYPE html> <html>",
}
# 固定文案 + 正文片段(HTML_BODY 的片段: 换行压成空格)
EXPECTED_PREFIX = {
    "csv": "上游返回的不是 CSV(疑似 XML/HTML 错误页); 正文片段: ",
    "tsv": "上游返回的不是 TSV(疑似 XML/HTML 错误页); 正文片段: ",
    "jsonl": "上游返回的不是 JSONL(疑似 HTML 错误页); 正文片段: ",
}
HTML_BODY_PREVIEW = HTML_BODY.replace("\n", " ")


def _fake_registry(shape: str) -> dict[str, Any]:
    """一个只有一个端点的假注册表 —— 查询路径的用例不依赖 data/ 的内容。"""
    return {"sources": [{
        "id": "fake", "name": "假源", "family": "测试", "org": "测试", "country": "测试",
        "summary": "测试用", "homepage": "https://example.org/", "link_status": "",
        "auth": {}, "limits": {}, "scale": {}, "increment": {}, "crawl": {},
        "proxy_policy": "never", "risk": "low", "tags": [], "aliases": [],
        "ua_requires_contact": False,
        "endpoints": [{"id": "ep", "name": "假端点", "method": "GET",
                       "url_template": "https://example.org/data", "params": [],
                       "response": {"shape": shape, "media_type": "*/*", "fields": []}}],
    }]}


def _html_handler(body: str, status: int = 200):
    def _h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body,
                              headers={"content-type": "text/html; charset=utf-8"})
    return _h


# ── R1: csv/tsv 的标记语言检测 ───────────────────────────────────────────
@pytest.mark.parametrize("shape", ["csv", "tsv"])
def test_csv_tsv_reject_a_markup_body(resolver: Resolver, shape: str) -> None:
    """csv/tsv 拿到 XML/HTML 错误页 -> 形状不匹配, 不是"数据行"。"""
    with pytest.raises(UpstreamNotShapeError) as ei:
        resolver._parse(HTML_BODY, shape, strict=True)
    assert ei.value.shape == shape
    assert "正文片段: <!DOCTYPE html>" in str(ei.value)


@pytest.mark.parametrize("shape", ["csv", "tsv"])
def test_csv_tsv_markup_message_is_verbatim(resolver: Resolver, shape: str) -> None:
    with pytest.raises(UpstreamNotShapeError) as ei:
        resolver._parse(SHORT_HTML, shape, strict=True)
    assert str(ei.value) == EXPECTED_MESSAGE[shape]


@pytest.mark.parametrize("body", [
    "<!DOCTYPE html><html></html>",
    "<!doctype html><html></html>",          # 大小写不敏感
    "<html><body>x</body></html>",
    "<HTML><body>x</body></html>",            # 大小写不敏感
    "<?xml version='1.0'?><root/>",
    "<?XML version='1.0'?><root/>",           # 大小写不敏感
    "<root><a>1</a></root>",                  # 泛 XML: 以 < 开头且前 512 字节内有 </>
])
def test_csv_tsv_markup_prefixes_are_detected(resolver: Resolver, body: str) -> None:
    with pytest.raises(UpstreamNotShapeError):
        resolver._parse(body, "csv", strict=True)


def test_bom_and_whitespace_do_not_hide_the_markup(resolver: Resolver) -> None:
    """BOM 与首尾空白要先去掉 —— 上游的 BOM + 换行很常见。"""
    with pytest.raises(UpstreamNotShapeError):
        resolver._parse("\ufeff  \n<!DOCTYPE html>\n<html>\n", "csv", strict=True)


def test_closing_tag_beyond_the_scan_window_is_not_markup(resolver: Resolver) -> None:
    """512 字节窗口: 窗口外的 </ 不算 —— 避免把长正文里的偶然内容误判成错误页。"""
    far = "<" + "x" * 600 + "</a>"
    assert resolver._parse(far, "csv", strict=True) == [[far]]     # 不报
    near = "<" + "x" * 100 + "</a>"
    with pytest.raises(UpstreamNotShapeError):
        resolver._parse(near, "csv", strict=True)


# ── R2: jsonl 的"全军覆没"检测 ───────────────────────────────────────────
def test_jsonl_all_lines_bad_is_a_shape_mismatch(resolver: Resolver) -> None:
    """有非空行却一行都解析不出来 -> 这不是 JSONL。"""
    with pytest.raises(UpstreamNotShapeError) as ei:
        resolver._parse(SHORT_HTML, "jsonl", strict=True)
    assert ei.value.shape == "jsonl"
    assert str(ei.value) == EXPECTED_MESSAGE["jsonl"]


def test_jsonl_partial_bad_lines_are_kept(resolver: Resolver) -> None:
    """部分行坏不算命中: 保持既有的"跳过坏行"宽松语义。"""
    body = '{"a":1}\nnot json\n{"a":2}\n'
    assert resolver._parse(body, "jsonl", strict=True) == [{"a": 1}, {"a": 2}]


def test_jsonl_all_good_is_kept(resolver: Resolver) -> None:
    body = '{"a":1}\n{"a":2}\n'
    assert resolver._parse(body, "jsonl", strict=True) == [{"a": 1}, {"a": 2}]


def test_jsonl_blank_lines_only_is_not_a_shape_mismatch(resolver: Resolver) -> None:
    """只有空行 -> 没有内容, 不是"全军覆没"。"""
    assert resolver._parse("\n\n   \n", "jsonl", strict=True) == []


# ── R8: 不得误伤 ─────────────────────────────────────────────────────────
def test_legit_csv_field_starting_with_lt_is_not_flagged(resolver: Resolver) -> None:
    """合法 CSV 的字段以 < 开头(如 <unknown>) -> 不误伤。"""
    body = "name,value\n<unknown>,1\n"
    assert resolver._parse(body, "csv", strict=True) == [["name", "value"], ["<unknown>", "1"]]


def test_empty_body_is_not_flagged(resolver: Resolver) -> None:
    """空正文 -> 不触发(没有"上游给了错误页"这回事)。"""
    assert resolver._parse("", "csv", strict=True) == []
    assert resolver._parse("", "tsv", strict=True) == []
    assert resolver._parse("", "jsonl", strict=True) == []


@pytest.mark.parametrize("shape", ["xml", "text"])
def test_xml_and_text_shapes_are_out_of_scope(resolver: Resolver, shape: str) -> None:
    """xml/text 的契约就是"把原文给你" —— 不给它们加形状检测。"""
    payload = resolver._parse(HTML_BODY, shape, strict=True)
    assert payload  # 非空: 原文照给


# ── R6: strict 语义 ──────────────────────────────────────────────────────
def test_lenient_mode_keeps_the_old_behaviour(resolver: Resolver, mirror: ModuleType) -> None:
    """strict=False: 不抛错, 把解析到的行原样返回(demo 要的是"上游给的东西")。"""
    assert resolver._parse(HTML_BODY, "csv", strict=False)
    assert mirror.parse_body(HTML_BODY, "csv", strict=False)
    assert resolver._parse(HTML_BODY, "jsonl", strict=False) == []
    assert mirror.parse_body(HTML_BODY, "jsonl", strict=False) == []


def test_worker_always_detects(resolver: Resolver) -> None:
    """worker 路径没有 strict 开关: 它服务 Go 网关的解析路径, 判定始终生效。"""
    with pytest.raises(WorkerNotShapeError):
        worker_parse_body(HTML_BODY, "csv")
    with pytest.raises(WorkerNotShapeError):
        worker_parse_body(HTML_BODY, "jsonl")


def test_worker_protocol_reports_the_shape_mismatch() -> None:
    """worker 的错误通道: ok=false + error_type=NotShape + 逐字文案。"""
    b64 = base64.b64encode(SHORT_HTML.encode("utf-8")).decode("ascii")
    resp = handle_request({"id": "t", "op": "parse", "shape": "csv", "body_b64": b64})
    assert resp["ok"] is False
    assert resp["error_type"] == "NotShape"
    assert resp["error"] == EXPECTED_MESSAGE["csv"]
    assert resp["body_preview"] == "<!DOCTYPE html> <html>"
    assert resp["id"] == "t"


# ── R4: 文案细节 ─────────────────────────────────────────────────────────
def test_empty_preview_has_no_snippet_suffix(resolver: Resolver) -> None:
    """正文为空时不带 "; 正文片段: " 后缀(与 Go 侧 NotJSONError 一致)。"""
    with pytest.raises(UpstreamNotShapeError) as ei:
        resolver._parse(HTML_BODY, "csv", strict=True)
    empty = type(ei.value)("csv", "")
    assert str(empty) == "上游返回的不是 CSV(疑似 XML/HTML 错误页)"


# ── R3/R7: 查询路径的信封 ────────────────────────────────────────────────
@pytest.mark.parametrize("shape", ["csv", "tsv", "jsonl"])
async def test_query_envelope_degrades_on_shape_mismatch(shape: str) -> None:
    """查询路径必须降级: degraded + parse_error + 直连指引 + data=[]。

    关键是**不能**报成 upstream_error 之类: 形状不匹配不是传输层失败,
    它落到通用分支就说明异常链接错了。
    """
    cfg = load_config(contact="test@example.com")
    r = Resolver(_fake_registry(shape), transport=httpx.MockTransport(_html_handler(HTML_BODY)),
                 cfg=cfg)
    await r.startup()
    env = await r.query(QueryRequest(source="fake", endpoint="ep", limit=2))
    await r.shutdown()

    assert env.meta.degraded is True
    assert env.meta.errors, "降级必须给出原因"
    assert env.meta.errors[0]["type"] == "parse_error", env.meta.errors
    assert env.meta.errors[0]["message"] == EXPECTED_PREFIX[shape] + HTML_BODY_PREVIEW
    assert env.meta.fallback and env.meta.fallback.get("upstream_url")
    assert env.data == [], "形状不匹配时不返回垃圾行"


async def test_shape_mismatch_is_not_a_transport_error() -> None:
    """形状不匹配不能落到通用异常分支(那会把 transport_error/upstream_error 报出来)。"""
    cfg = load_config(contact="test@example.com")
    r = Resolver(_fake_registry("csv"), transport=httpx.MockTransport(_html_handler(HTML_BODY)),
                 cfg=cfg)
    await r.startup()
    env = await r.query(QueryRequest(source="fake", endpoint="ep", limit=2))
    await r.shutdown()

    types = {e.get("type") for e in env.meta.errors}
    assert types == {"parse_error"}, types
    assert not any(t in ("upstream_error", "connect_error", "read_error") for t in types)
