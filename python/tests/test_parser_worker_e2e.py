"""
test_parser_worker_e2e.py - 解析面 NDJSON 协议的端到端测试(真子进程 + 真管道)。

背景: Go 网关把上游原始响应丢给 parser_worker 子进程, 靠 stdin/stdout 上的换行分隔
JSON(NDJSON)拿回规范化条目 —— 这是两个语言平面之间**唯一**的接缝。此前这条接缝没有任何
自动化覆盖:

  - Go 侧没有 PythonEngine 的用例(每个 Go 用例都强制走 Go 引擎);
  - Python 侧的用例全部在进程内直接调 handle_line(), 从不经过真实管道。

于是协议不变量一条都没被真正验证过: 每个请求是否恰好一行响应 / stdout 是否只有协议 /
未知 op 是否致命 / 流水线是否错位 / ping 与 shutdown 是否可用。

本文件起**真实子进程**(`sys.executable -m a_source_gateway.parser_worker`), 用**真实管道**
收发 NDJSON, 把上面这些不变量逐条钉死; 解析结果则按 Go 引擎同样实现的那套语义断言
(item_path 点号遍历 / total_field 数字下标 / item_shape=map / limit 截断与 truncated
标志 / fields 投影 / _xml 与 _text 兜底 / _xml_bytes 投影), 两平面因此不会悄悄漂移。

全部离线: 只喂固定正文, 不发任何网络请求; 也不需要任何新依赖(仅标准库 + pytest)。
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "python" / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "onboarding"

# stdout 关闭标记(与任何协议行都不相等)
EOF = object()

# 协议不变量: 响应里必须有这些字段
RESPONSE_FIELDS = {"id", "ok"}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _fixture(name: str) -> str:
    """读固定正文(离线, 不触网)。"""
    with (FIXTURES / name).open(encoding="utf-8", newline="") as f:
        return f.read()


class Worker:
    """一个真实的 parser_worker 子进程: 真 stdin/stdout 管道, 真 NDJSON。

    stdout / stderr 各有一个后台线程在抽干(避免管道写满造成死锁);
    stdout 的行进队列, 由测试线程按需取 —— 于是"有没有多出一行"也能被断言。
    """

    def __init__(self) -> None:
        env = dict(os.environ)
        # 让子进程直接用仓库里的源码树: 测的就是被测的那份代码, 不依赖是否装过包
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + prev if prev else "")
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "a_source_gateway.parser_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._out: queue.Queue[Any] = queue.Queue()
        self._err: list[str] = []
        self._t_out = threading.Thread(target=self._pump_out, daemon=True)
        self._t_err = threading.Thread(target=self._pump_err, daemon=True)
        self._t_out.start()
        self._t_err.start()

    # ── 后台抽干 ────────────────────────────────────────────────────────
    def _pump_out(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._out.put(line.rstrip("\n"))
        self._out.put(EOF)

    def _pump_err(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._err.append(line.rstrip("\n"))

    # ── 收发 ────────────────────────────────────────────────────────────
    def send(self, req: dict[str, Any]) -> None:
        self.send_raw(json.dumps(req, ensure_ascii=False))

    def send_raw(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_line(self, timeout: float = 20.0) -> str | None:
        """取一行协议行; None 表示 stdout 已关闭(EOF)。"""
        try:
            item = self._out.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"等待 worker 响应超时({timeout}s): 没有协议行回来") from None
        if item is EOF:
            return None
        return str(item)

    def read_response(self, timeout: float = 20.0) -> dict[str, Any]:
        line = self.read_line(timeout)
        assert line is not None, "worker 没回任何一行(协议要求每个请求恰好一行响应)"
        resp = json.loads(line)
        assert isinstance(resp, dict), f"响应必须是 JSON 对象: {line!r}"
        return resp

    def assert_no_extra_response(self, wait: float = 0.3) -> None:
        """断言没有多出响应行(协议要求每个请求恰好一行响应)。"""
        try:
            extra = self._out.get(timeout=wait)
        except queue.Empty:
            return
        raise AssertionError(f"多出一行响应(协议要求每个请求恰好一行): {extra!r}")

    # ── 收尾 ────────────────────────────────────────────────────────────
    def stderr_text(self) -> str:
        return "\n".join(self._err)

    def wait_for_exit(self, timeout: float = 10.0) -> int:
        code = self.proc.wait(timeout=timeout)
        self._t_out.join(timeout=5)
        self._t_err.join(timeout=5)
        return code

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._t_out.join(timeout=5)
        self._t_err.join(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


@pytest.fixture
def worker() -> Worker:
    w = Worker()
    try:
        yield w
    finally:
        w.close()


def _parse_req(rid: str, body: str, shape: str = "object",
               media_type: str = "application/json", **spec: Any) -> dict[str, Any]:
    return {"id": rid, "op": "parse", "shape": shape, "media_type": media_type,
            "body_b64": _b64(body), "spec": spec}


# ── 协议不变量 ────────────────────────────────────────────────────────────
def test_ping_returns_ok_with_version(worker: Worker) -> None:
    worker.send({"id": "p-1", "op": "ping"})
    resp = worker.read_response()
    assert resp["id"] == "p-1"
    assert resp["ok"] is True
    assert resp["version"] == "1.0"
    assert resp["python"].startswith("3.")
    worker.assert_no_extra_response()


def test_parse_returns_exactly_one_response_line_with_matching_id(worker: Worker) -> None:
    body = json.dumps({"message": {"items": [{"a": 1}, {"a": 2}]}, "meta": {"total": 17}})
    worker.send(_parse_req("req-1", body, item_path="message.items", total_field="meta.total"))
    resp = worker.read_response()
    assert resp["id"] == "req-1", "响应 id 必须与请求 id 对上"
    assert resp["ok"] is True
    assert set(resp) >= RESPONSE_FIELDS
    assert resp["items"] == [{"a": 1}, {"a": 2}]
    assert resp["total"] == 17
    assert resp["truncated"] is True
    assert isinstance(resp["elapsed_ms"], float)
    # 恰好一行: 不能再多出第二行
    worker.assert_no_extra_response()


def test_ten_back_to_back_requests_do_not_desynchronise(worker: Worker) -> None:
    ids = [f"req-{i:02d}" for i in range(10)]
    for i, rid in enumerate(ids):
        body = json.dumps({"items": [{"i": i}], "meta": {"total": 100}})
        worker.send(_parse_req(rid, body, item_path="items", total_field="meta.total"))
    seen = [worker.read_response() for _ in range(10)]
    # 10 个请求 -> 10 个响应, 顺序一一对应, 没有交错/错位
    assert [r["id"] for r in seen] == ids
    assert [r["items"][0]["i"] for r in seen] == list(range(10))
    assert all(r["ok"] is True for r in seen)
    worker.assert_no_extra_response()


def test_unknown_op_returns_error_and_worker_survives(worker: Worker) -> None:
    worker.send({"id": "u-1", "op": "frobnicate"})
    resp = worker.read_response()
    assert resp["id"] == "u-1"
    assert resp["ok"] is False
    assert resp["error"] == "unknown op"
    assert resp["error_type"] == "BadRequest"
    # 未知 op 不能弄死 worker: 下一个正常请求照样有响应
    worker.send({"id": "p-2", "op": "ping"})
    after = worker.read_response()
    assert after["id"] == "p-2"
    assert after["ok"] is True


def test_malformed_request_line_does_not_kill_the_worker(worker: Worker) -> None:
    worker.send_raw("this is not json")
    resp = worker.read_response()
    assert resp["ok"] is False
    assert resp["error_type"] == "BadRequest"
    assert "invalid json" in resp["error"]
    # 合法 JSON 但不是对象: 同样回一行错误, 不崩
    worker.send_raw("[1,2,3]")
    resp2 = worker.read_response()
    assert resp2["ok"] is False
    assert resp2["error_type"] == "BadRequest"
    # 坏行之后 worker 依然可用
    worker.send({"id": "after-bad", "op": "ping"})
    assert worker.read_response()["id"] == "after-bad"


def test_unknown_top_level_fields_are_ignored(worker: Worker) -> None:
    """Go 侧会多发一个 trace_id; worker 必须忽略它而不是报错。"""
    req = _parse_req("t-1", json.dumps({"items": [{"a": 1}]}), item_path="items")
    req["trace_id"] = "abc-123"
    req["unexpected"] = {"nested": True}
    worker.send(req)
    resp = worker.read_response()
    assert resp["ok"] is True
    assert resp["id"] == "t-1"
    assert resp["items"] == [{"a": 1}]


def test_shutdown_makes_the_worker_exit_cleanly(worker: Worker) -> None:
    worker.send({"id": "s-1", "op": "shutdown"})
    resp = worker.read_response()
    assert resp == {"id": "s-1", "ok": True}
    assert worker.wait_for_exit(timeout=10) == 0, "shutdown 后必须干净退出(退出码 0)"


def test_stdout_carries_only_protocol_lines_and_logs_go_to_stderr(worker: Worker) -> None:
    worker.send({"id": "p-1", "op": "ping"})
    worker.send({"id": "u-1", "op": "frobnicate"})
    worker.send({"id": "s-1", "op": "shutdown"})
    lines: list[str] = []
    while True:
        line = worker.read_line()
        if line is None:
            break
        lines.append(line)
    assert worker.wait_for_exit(timeout=10) == 0
    assert len(lines) == 3, f"3 个请求 -> 3 行响应, 实际 {len(lines)} 行"
    for line in lines:
        obj = json.loads(line)                 # stdout 上每一行都必须是合法 JSON
        assert isinstance(obj, dict)
        assert set(obj) >= RESPONSE_FIELDS
        assert "[parser-worker]" not in line, "日志不能混进 stdout(协议专用通道)"
    # 日志只走 stderr
    err = worker.stderr_text()
    assert "[parser-worker]" in err
    assert "未知 op" in err


# ── 与 Go 引擎同款的解析语义(跨语言契约) ────────────────────────────────
# 下面每条语义 Go 侧也实现了一遍(gateway/internal/parser/parser.go 的 Extract/Trim,
# gateway/internal/server/resolver.go 的投影), 两边必须给出同样的结果。
# 这里不 import Go 代码, 而是按文档化的语义直接断言结果。
def test_item_path_dotted_traversal_and_total_field(worker: Worker) -> None:
    body = _fixture("crossref_works.json")
    worker.send(_parse_req("c-1", body, item_path="message.items",
                           total_field="message.total-results"))
    resp = worker.read_response()
    assert resp["ok"] is True
    assert len(resp["items"]) == 1
    assert resp["items"][0]["DOI"] == "10.1051/epjconf/s20130018002"
    assert resp["total"] == 187948954
    # 上游总数远大于本页条数 -> 还有更多, 标记可继续翻页
    assert resp["truncated"] is True


def test_openalex_fixture_matches_the_go_contract(worker: Worker) -> None:
    """OpenAlex 形状(results + meta.count): 与 Go 引擎给出的条目/总数一致。"""
    body = _fixture("openalex_works.json")
    worker.send(_parse_req("c-10", body, item_path="results", total_field="meta.count"))
    resp = worker.read_response()
    assert resp["ok"] is True
    assert len(resp["items"]) == 1
    assert resp["items"][0]["doi"] == "https://doi.org/10.7717/peerj.4375"
    assert resp["total"] == 328367407
    assert resp["truncated"] is True


def test_total_field_traversal_with_numeric_index(worker: Worker) -> None:
    body = json.dumps({"data": [{"total": 42, "x": 1}, {"total": 7}]})
    worker.send(_parse_req("c-2", body, item_path="data", total_field="data.0.total"))
    resp = worker.read_response()
    assert resp["total"] == 42
    assert len(resp["items"]) == 2
    assert resp["truncated"] is True


def test_item_shape_map_projects_object_values(worker: Worker) -> None:
    body = json.dumps({"0": {"a": 1}, "1": {"a": 2}})
    worker.send(_parse_req("c-3", body, item_shape="map"))
    resp = worker.read_response()
    assert resp["items"] == [{"a": 1}, {"a": 2}]
    assert resp["total"] == 2
    assert resp["truncated"] is False


def test_limit_truncates_and_sets_truncated_flag(worker: Worker) -> None:
    body = json.dumps({"items": [{"a": i} for i in range(5)]})
    worker.send(_parse_req("c-4", body, item_path="items", limit=2))
    resp = worker.read_response()
    assert len(resp["items"]) == 2
    assert resp["items"] == [{"a": 0}, {"a": 1}]
    assert resp["truncated"] is True
    assert resp["total"] == 5            # total 取截断前的长度


def test_fields_projection_keeps_only_requested_keys(worker: Worker) -> None:
    body = json.dumps({"items": [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]})
    worker.send(_parse_req("c-5", body, item_path="items", fields=["a"]))
    resp = worker.read_response()
    assert resp["items"] == [{"a": 1}, {"a": 4}]
    assert resp["fields"] == ["a"]


def test_xml_fallback_projects_xml_bytes(worker: Worker) -> None:
    body = "<root><a>1</a></root>"
    worker.send(_parse_req("c-6", body, shape="xml", media_type="application/xml"))
    resp = worker.read_response()
    assert resp["ok"] is True
    assert resp["items"] == []            # items 永远是数组
    assert resp["data"] == {"_xml_bytes": len(body.encode("utf-8")), "_truncated": False}
    assert resp["truncated"] is False


def test_non_json_body_is_reported_as_not_json(worker: Worker) -> None:
    """非 JSON 正文走错误通道(error_type=NotJSON), 不再静默兜底成 _text。

    Go 网关按 error_type 映射成信封里的 upstream_not_json 降级。
    """
    body = "not json at all"
    worker.send(_parse_req("c-7", body, shape="object"))
    resp = worker.read_response()
    assert resp["ok"] is False
    assert resp["error_type"] == "NotJSON"
    assert resp["body_preview"] == body
    assert body in resp["error"]
    # worker 本身不能被这条请求弄死: 下一条照样有响应
    worker.send({"id": "after-not-json", "op": "ping"})
    assert worker.read_response()["id"] == "after-not-json"


def test_csv_shape_parses_rows(worker: Worker) -> None:
    body = _fixture("sample.csv")
    worker.send(_parse_req("c-8", body, shape="csv", media_type="text/csv"))
    resp = worker.read_response()
    assert resp["ok"] is True
    assert resp["items"][0] == ["id", "title", "published", "doi"]
    assert resp["items"][1] == ["A-001", "First record", "2024-01-15", "10.1000/first"]
    assert resp["total"] == 4             # 表头也计入行数


def test_top_level_array_body_keeps_whole_payload(worker: Worker) -> None:
    """item_path 只支持对象键: 顶层是数组时, 整个数组就是条目列表(与 Go 引擎一致)。"""
    body = _fixture("list_body.json")
    worker.send(_parse_req("c-9", body, item_path="items"))
    resp = worker.read_response()
    assert resp["ok"] is True
    assert len(resp["items"]) == 2
    assert resp["items"][0]["id"] == "W1"
    assert resp["total"] == 2
