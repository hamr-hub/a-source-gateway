"""parser_worker 的单元测试(进程内直接调用, 不起子进程, 不联网)。"""
from __future__ import annotations

import base64
import json

import pytest

from a_source_gateway.parser_worker import (
    NotJSONError,
    handle_line,
    handle_request,
    parse_body,
    project,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _parse(body: str, shape: str = "object", **spec):
    """发一条 parse 请求, 返回响应对象。"""
    return handle_request({"id": "t", "op": "parse", "shape": shape,
                           "body_b64": _b64(body), "spec": spec})


# ── JSON: item_path + total_field ────────────────────────────────────────
def test_json_item_path_and_total_field():
    body = json.dumps({"message": {"items": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]},
                       "meta": {"total": 17}})
    r = _parse(body, item_path="message.items", total_field="meta.total")
    assert r["ok"] is True
    assert r["items"] == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert r["total"] == 17
    # 上游总数 17 > 本页 2 条 -> 还有更多, 标记可继续翻页
    assert r["truncated"] is True
    assert r["fields"] == ["a", "b"]          # 没指定 fields -> 取条目键并集


def test_total_not_greater_than_items_is_not_truncated():
    body = json.dumps({"message": {"items": [{"a": 1}, {"a": 2}]}, "meta": {"total": 2}})
    r = _parse(body, item_path="message.items", total_field="meta.total")
    assert r["total"] == 2
    assert r["truncated"] is False


def test_total_defaults_to_item_count():
    body = json.dumps({"items": [{"a": 1}, {"a": 2}]})
    r = _parse(body, item_path="items")
    assert r["total"] == 2


def test_total_field_with_numeric_index():
    body = json.dumps({"data": [{"total": 42, "x": 1}, {"total": 7}]})
    r = _parse(body, item_path="data", total_field="data.0.total")
    assert r["total"] == 42
    # 上游总数 > 本页条数 -> 标记可继续翻页
    assert r["truncated"] is True


def test_item_shape_map_turns_object_into_value_list():
    body = json.dumps({"0": {"a": 1}, "1": {"a": 2}})
    r = _parse(body, item_shape="map")
    assert r["items"] == [{"a": 1}, {"a": 2}]
    assert r["total"] == 2


def test_limit_truncates_and_marks_truncated():
    body = json.dumps({"items": [{"a": i} for i in range(5)]})
    r = _parse(body, item_path="items", limit=2)
    assert len(r["items"]) == 2
    assert r["truncated"] is True
    assert r["total"] == 5                     # total 取截断前的长度


def test_fields_projection_keeps_only_listed_keys():
    body = json.dumps({"items": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]})
    r = _parse(body, item_path="items", fields=["a"])
    assert r["items"] == [{"a": 1}, {"a": 3}]
    assert r["fields"] == ["a"]


# ── CSV / TSV / JSONL ────────────────────────────────────────────────────
def test_csv_rows_include_header():
    r = _parse("a,b\n1,2\n3,4\n", shape="csv")
    assert r["items"] == [["a", "b"], ["1", "2"], ["3", "4"]]


def test_tsv_uses_tab_delimiter():
    r = _parse("a\tb\n1\t2\n", shape="tsv")
    assert r["items"] == [["a", "b"], ["1", "2"]]


def test_csv_respects_max_rows():
    body = "\n".join(f"{i},{i}" for i in range(10))
    r = _parse(body, shape="csv", max_rows=3)
    assert len(r["items"]) == 3


def test_jsonl_skips_blank_and_malformed_lines():
    body = '{"a":1}\n\nnot json\n{"a":2}\n'
    r = _parse(body, shape="jsonl")
    assert r["items"] == [{"a": 1}, {"a": 2}]


def test_jsonl_respects_max_rows():
    body = "\n".join(f'{{"a":{i}}}' for i in range(10))
    r = _parse(body, shape="jsonl", max_rows=4)
    assert len(r["items"]) == 4


# ── XML / text / 坏 JSON ─────────────────────────────────────────────────
def test_xml_produces_xml_bytes_projection():
    body = "<root><a>1</a></root>"
    r = _parse(body, shape="xml")
    assert r["ok"] is True
    assert r["items"] == []                    # items 永远是数组
    assert r["data"] == {"_xml_bytes": len(body.encode("utf-8")), "_truncated": False}
    assert r["truncated"] is False


def test_text_shape_is_truncated_to_200k():
    r = _parse("x" * 300_000, shape="text")
    assert len(r["data"]["_text"]) == 200_000


def test_invalid_json_is_reported_as_not_json():
    """json 形状解析失败不再静默兜底 —— 必须报错, 否则调用方分不清
    "确实没有数据"和"上游给了 HTML 错误页"。"""
    r = _parse("not json at all", shape="json")
    assert r["ok"] is False
    assert r["error_type"] == "NotJSON"
    assert "not json at all" in r["error"]
    assert r["body_preview"] == "not json at all"
    assert r["id"] == "t"          # 请求 id 要带回去, 调用方靠它对账


def test_not_json_preview_is_one_line_and_capped():
    """回带的片段必须是单行(换行压成空格)且限长 —— 否则信封里会出现裸换行。"""
    html = "<!DOCTYPE html>\n<html>\n" + "<p>x</p>\n" * 200 + "</html>"
    r = _parse(html, shape="json")
    assert r["error_type"] == "NotJSON"
    preview = r["body_preview"]
    assert "\n" not in preview and "\r" not in preview and "\t" not in preview
    assert len(preview) <= 241          # 240 字 + 省略号
    assert preview.startswith("<!DOCTYPE html> <html>")
    # Go 网关是从 error 字段里再取片段的, 所以 error 里必须也带着正文
    assert "<!DOCTYPE html>" in r["error"]


def test_parse_body_only_raises_for_json_shapes():
    """json 形状解析失败抛 NotJSONError; xml/text/csv/jsonl 永不抛错。"""
    with pytest.raises(NotJSONError) as ei:
        parse_body("<<<>>>", "json")
    assert "<<<>>>" in ei.value.preview
    assert parse_body("", "jsonl") == []
    assert isinstance(parse_body("\x00\x01", "csv"), list)   # 不抛错即可
    # xml/text 的兜底是约定语义, 不是错误
    assert parse_body("<a/>", "xml") == {"_xml": "<a/>"}
    assert parse_body("hello", "text") == {"_text": "hello"}


# ── 协议层 ───────────────────────────────────────────────────────────────
def test_base64_body_is_decoded_as_utf8():
    body = json.dumps({"名称": "证监会"}, ensure_ascii=False)
    r = _parse(body)
    assert r["data"]["名称"] == "证监会"


def test_malformed_base64_returns_error_response():
    r = handle_request({"id": "x", "op": "parse", "shape": "object", "body_b64": "abc"})
    assert r["ok"] is False
    assert r["error_type"] == "DecodeError"
    assert r["id"] == "x"


def test_missing_body_returns_error_response():
    r = handle_request({"id": "x", "op": "parse", "shape": "object"})
    assert r["ok"] is False
    assert r["error_type"] == "DecodeError"


def test_ping_and_shutdown():
    ping = handle_request({"id": "p", "op": "ping"})
    assert ping["ok"] is True
    assert ping["version"] == "1.0"
    assert ping["python"].startswith("3.")

    bye = handle_request({"id": "s", "op": "shutdown"})
    assert bye == {"id": "s", "ok": True}


def test_unknown_op_keeps_worker_usable():
    r = handle_request({"id": "u", "op": "frobnicate"})
    assert r["ok"] is False
    assert r["error"] == "unknown op"
    # 未知 op 之后仍然能正常干活
    assert handle_request({"id": "p2", "op": "ping"})["ok"] is True


def test_every_request_yields_exactly_one_response_line():
    lines = [
        '{"id":"1","op":"ping"}',
        "not json at all",
        "",
        '{"id":"2","op":"parse","shape":"object","body_b64":"e30="}',
        '{"id":"3","op":"zzz"}',
        "[1,2,3]",
    ]
    responses = [handle_line(line) for line in lines]
    assert len(responses) == len(lines)
    for resp in responses:
        assert isinstance(resp, dict)
        assert "ok" in resp
        # 一行 JSON: 序列化后不含裸换行
        assert "\n" not in json.dumps(resp, ensure_ascii=False)


def test_response_has_elapsed_ms_and_is_serializable():
    r = _parse(json.dumps({"items": [{"a": 1}]}), item_path="items")
    assert isinstance(r["elapsed_ms"], float)
    assert r["elapsed_ms"] >= 0
    json.dumps(r, ensure_ascii=False)          # 不抛错 = 可序列化


def test_project_keeps_non_dict_rows_when_fields_given():
    payload = [{"a": 1}, "raw"]
    data, total, truncated = project(payload, limit=0, fields=["a"])
    assert data == [{"a": 1}, "raw"]            # 非字典行原样保留, 不炸
    assert total == 2
    assert truncated is False


# ── 顶层数组 / 列表套列表(国内源用到的两种形状) ─────────────────────────
def test_top_level_array_is_located_by_numeric_item_path():
    """顶层是数组时, item_path 用数字下标定位(深交所 A股列表的形状)。"""
    from a_source_gateway.parser_worker import project

    payload = [{"metadata": {"recordcount": 2906},
                "data": [{"agdm": "000001"}, {"agdm": "000002"}], "error": None}]
    data, total, _truncated = project(payload, item_path="0.data",
                                      total_field="0.metadata.recordcount", limit=10)
    assert total == 2906
    assert [row["agdm"] for row in data] == ["000001", "000002"]


def test_top_level_array_with_object_item_path_keeps_whole_payload():
    """顶层是数组且路径首段不是数字时, 保持整个数组(不能变成空列表)。"""
    from a_source_gateway.parser_worker import project

    payload = [{"id": "W1"}, {"id": "W2"}]
    data, total, _truncated = project(payload, item_path="items", limit=10)
    assert len(data) == 2
    assert total == 2


def test_item_flatten_flattens_one_level():
    """列表套列表时展平一层(上交所公告的形状)。"""
    from a_source_gateway.parser_worker import project

    payload = {"pageHelp": {"total": 2, "data": [[{"TITLE": "a"}], [{"TITLE": "b"}]]}}
    data, total, _truncated = project(payload, item_path="pageHelp.data",
                                      total_field="pageHelp.total",
                                      item_flatten=True, limit=10)
    assert total == 2
    assert [row["TITLE"] for row in data] == ["a", "b"]
