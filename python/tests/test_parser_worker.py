"""parser_worker 的单元测试(进程内直接调用, 不起子进程, 不联网)。"""
from __future__ import annotations

import base64
import json

from a_source_gateway.parser_worker import (
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


def test_invalid_json_falls_back_to_text():
    r = _parse("not json at all", shape="json")
    assert r["ok"] is True
    assert r["items"] == []
    assert r["data"] == {"_text": "not json at all"}


def test_parse_body_never_raises_on_garbage():
    assert parse_body("<<<>>>", "json") == {"_text": "<<<>>>"}
    assert parse_body("", "jsonl") == []
    assert isinstance(parse_body("\x00\x01", "csv"), list)   # 不抛错即可


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
