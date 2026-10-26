"""
parser_worker.py - 解析面 worker(两平面架构的 Python 半边)

架构定位: **Python 做解析, Go 做网关**。
Go 网关把上游原始响应丢给本 worker, 本 worker 把它解析成规范化条目再回传,
于是"取数/调度/限速/缓存"只有一份实现(在 Go 侧), Python 只负责"内容与结构"。

## 协议: stdin/stdout 上的换行分隔 JSON(NDJSON)

- stdin: 每行一个请求 JSON 对象。
- stdout: **只**写响应, 每行一个 JSON 对象(协议专用通道)。
- stderr: 所有日志(worker 的 stdout 不能混入任何非协议内容)。

请求:
    {"id":"req-1","op":"parse","shape":"object|array|json|jsonl|csv|tsv|xml|text",
     "media_type":"application/json","body_b64":"<base64 of the raw upstream bytes>",
     "spec":{"item_path":"message.items","total_field":"meta.total","item_shape":"list|map",
             "limit":20,"fields":["a","b"],"max_rows":15000}}

响应:
    {"id":"req-1","ok":true,"items":[...],"total":123,"truncated":false,
     "fields":["a","b"],"elapsed_ms":3.2}
    {"id":"req-1","ok":false,"error":"boom","error_type":"ParseError"}

上游正文不是 JSON 时(**只针对 json 形状**):
    {"id":"req-1","ok":false,"error":"上游返回的不是 JSON(疑似 HTML 错误页); 正文片段: <正文前若干字符>",
     "error_type":"NotJSON","body_preview":"<正文前若干字符>"}

其他 op:
    {"id":"x","op":"ping"}      -> {"id":"x","ok":true,"version":"1.0","python":"3.12.10"}
    {"id":"x","op":"shutdown"}  -> {"id":"x","ok":true}(然后干净退出)
    未知 op                    -> {"id":"x","ok":false,"error":"unknown op"}(继续服务)

## 响应字段约定

- `items`: 规范化后的条目列表。**永远是数组**。
- `data`: 只有当投影结果不是数组时才出现(例如 XML 的 `{"_xml_bytes":N,"_truncated":false}`),
  此时 `items` 为 `[]`。这样 Go 侧可以放心把 `items` 反序列化成 `[]any`。
- `total`: 上游声明的总数; 上游没给时取条目数(列表投影时)。
- `truncated`: 条目被 limit 截断, 或上游总数大于本次返回条数。
- `fields`: 调用方请求的字段列表; 没请求且条目是对象时, 取所有对象条目的键并集
  (按首次出现顺序); 其余情况为 `[]`。
- `elapsed_ms`: 解析墙钟(毫秒, 浮点)。

## 语义来源

`parse_body()` / `project()` 逐条对齐 `Resolver._parse` / `Resolver._project`
(见 resolver.py), 便于 Python 与 Go 两边给出同样的条目。
本模块**只依赖标准库**, 不引入任何新运行时依赖。
"""
from __future__ import annotations

import base64
import csv
import io
import json
import platform
import sys
import time
from typing import Any

WORKER_VERSION = "1.0"

# 与 resolver.py 保持一致的上限
JSON_LIMIT_BYTES = 4 * 1024 * 1024      # XML 原文上限
TEXT_LIMIT_CHARS = 200_000             # 纯文本/解析失败时的兜底上限
DEFAULT_MAX_ROWS = 15_000              # CSV/TSV/JSONL 最多解析的行数

# 上游正文不是 JSON 时, 回带的正文前若干字符(去换行、限长)
# 与 Go 侧 parser.NotJSONSnippetRunes 一致, 两个平面回出的片段长度相同。
NOT_JSON_PREVIEW_CHARS = 240

# 防御性上限: 单个请求的解码后正文超过这个体积就截断(避免把内存吃光)
MAX_BODY_BYTES = 64 * 1024 * 1024

# media_type -> shape(请求没给 shape 时的兜底)
SHAPE_BY_MEDIA_TYPE: dict[str, str] = {
    "application/json": "object",
    "text/json": "object",
    "application/x-ndjson": "jsonl",
    "application/jsonl": "jsonl",
    "text/csv": "csv",
    "text/tab-separated-values": "tsv",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "text",
}


def _log(msg: str) -> None:
    """日志一律走 stderr —— stdout 是协议专用通道。"""
    print(f"[parser-worker] {msg}", file=sys.stderr, flush=True)


# ── 解析(等价于 Resolver._parse) ─────────────────────────────────────────
class NotJSONError(Exception):
    """上游正文不是 JSON(常见于 WAF/反爬返回的 HTML 错误页)。

    以前这里静默兜底成 {"_text": 原文}, 于是投影得到空列表, 而调用方(Go 网关)
    以为这是"成功但没数据" —— 分不清"确实没有数据"和"上游给了 HTML 错误页"。
    现在通过既有的 NDJSON 错误通道回传, error_type = "NotJSON"。
    """

    def __init__(self, preview: str):
        super().__init__(f"上游返回的不是 JSON(疑似 HTML 错误页); 正文片段: {preview}")
        self.preview = preview


def body_preview(body: str, limit: int = NOT_JSON_PREVIEW_CHARS) -> str:
    """正文前若干字符: 换行/制表符压成空格、限长。

    换行必须去掉 —— 错误消息会进 JSON 信封, 裸换行会把一行协议撑成多行。
    与 Go 侧 parser.notJSONSnippet 的处理一致。
    """
    flat = body[:limit * 4].replace("\n", " ").replace("\r", " ").replace("\t", " ").strip()
    if len(flat) > limit:
        return flat[:limit] + "…"
    return flat


def parse_body(body: str, shape: str, max_rows: int = DEFAULT_MAX_ROWS) -> Any:
    """把上游原始正文解析成中间结构。

    与 Resolver._parse 语义一致:
      csv/tsv  -> csv.reader, 首行(表头)也计入行数上限
      jsonl    -> 每行一个 JSON, 空行与坏行跳过
      xml      -> {"_xml": 原文[:4MB]}
      text     -> {"_text": 原文[:200000]}
      其它     -> json.loads; 失败抛 NotJSONError(**不再静默兜底成 _text**)

    xml/text 形状的 _xml/_text 兜底是**约定语义**(不是错误), 保持不变。
    """
    if shape in ("csv", "tsv"):
        delim = "\t" if shape == "tsv" else ","
        rows: list[list[str]] = []
        try:
            for i, row in enumerate(csv.reader(io.StringIO(body), delimiter=delim)):
                if i >= max_rows:        # 解析护栏: 别让大表把预算吃光
                    break
                rows.append(row)
        except csv.Error as e:          # 超长字段 / NUL 字节等: 保留已读到的行
            _log(f"csv 解析中断: {type(e).__name__}: {e}")
        return rows
    if shape == "jsonl":
        out: list[Any] = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:           # 坏行跳过, 不影响其余行
                continue
            if len(out) >= max_rows:
                break
        return out
    if shape == "xml":
        return {"_xml": body[:JSON_LIMIT_BYTES]}
    if shape == "text":
        return {"_text": body[:TEXT_LIMIT_CHARS]}
    try:
        return json.loads(body)
    except Exception:                    # 不是 JSON: 如实报错, 不静默兜底
        raise NotJSONError(body_preview(body)) from None


# ── 投影(等价于 Resolver._project) ───────────────────────────────────────
def _dig(node: Any, path: str) -> Any:
    """点号路径取值; 列表只支持数字下标(与 resolver 的 total_field 语义一致)。

    点号切分取不到时, 再按整串字面键试一次 —— 上游 OData 的 "@odata.count"
    这类键名本身带点, 按 "." 切永远取不到它(与 Go parser 的 lookupTotalPath 同语义)。
    """
    root = node
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                break
            node = node[part]
        elif isinstance(node, list) and part.isdigit():
            idx = int(part)
            if idx >= len(node):
                break
            node = node[idx]
        else:
            break
    else:
        return node
    if isinstance(root, dict) and path in root:
        return root[path]
    return None


def project(payload: Any, *, item_path: str | None = None, total_field: str | None = None,
            item_shape: str = "list", limit: int = 0,
            fields: list[str] | None = None,
            item_flatten: bool = False) -> tuple[Any, int | None, bool]:
    """从上游响应里取出列表、总数, 并按 fields/limit 裁剪。

    与 Resolver._project 语义一致, 唯一差别: 字段投影时遇到非字典行不会抛错,
    而是原样保留(worker 必须永不崩)。
    """
    total: int | None = None
    if total_field:
        node = _dig(payload, total_field)
        if isinstance(node, int | float):
            total = int(node)

    data: Any = payload
    if item_shape == "map" and isinstance(payload, dict):
        data = list(payload.values())          # 形如 {"0": {...}, "1": {...}}
    elif item_path:
        # 顶层是数组时: 路径首段是数字才按下标定位(如 "0.data");
        # 否则保持整个数组(旧行为, 不能让"顶层数组"变成空列表)。
        head = item_path.split(".")[0]
        if isinstance(payload, dict) or head.isdigit():
            node = _dig(payload, item_path)
            data = node if node is not None else []
        else:
            data = payload
        if item_flatten and isinstance(data, list) and data and isinstance(data[0], list):
            flat: list[Any] = []
            for sub in data:
                if isinstance(sub, list):
                    flat.extend(sub)
                else:
                    flat.append(sub)
            data = flat

    truncated = False
    if isinstance(data, list):
        if total is None:
            total = len(data)                    # 上游没给总数就用条目数
        if limit and len(data) > limit:
            data = data[:limit]
            truncated = True
        elif total is not None and total > len(data):
            truncated = True                     # 上游页取满且还有更多
        if fields and data and isinstance(data[0], dict):
            keep = set(fields)
            data = [{k: v for k, v in row.items() if k in keep} if isinstance(row, dict) else row
                    for row in data]
    elif isinstance(payload, dict) and "_xml" in payload:
        raw = payload["_xml"]
        text = raw if isinstance(raw, str) else str(raw)
        data = {"_xml_bytes": len(text.encode("utf-8")),
                "_truncated": len(text) >= JSON_LIMIT_BYTES}
        truncated = bool(payload.get("_truncated", False))

    return data, total, truncated


def infer_fields(items: list[Any]) -> list[str]:
    """没指定 fields 时, 取所有对象条目的键并集(按首次出现顺序)。"""
    out: list[str] = []
    seen: set[str] = set()
    for row in items:
        if not isinstance(row, dict):
            continue
        for k in row:
            if k not in seen:
                seen.add(k)
                out.append(k)
    return out


# ── 请求处理 ─────────────────────────────────────────────────────────────
def _decode_body(req: dict[str, Any]) -> str:
    """取原始正文: 优先 body_b64(base64), 退化到 body(明文)。"""
    b64 = req.get("body_b64")
    if isinstance(b64, str):
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception as e:
            raise ValueError(f"body_b64 不是合法 base64: {type(e).__name__}: {e}") from e
        if len(raw) > MAX_BODY_BYTES:
            _log(f"正文超过 {MAX_BODY_BYTES} 字节, 已截断")
            raw = raw[:MAX_BODY_BYTES]
        return raw.decode("utf-8", "replace")
    body = req.get("body")
    if isinstance(body, str):
        return body[:MAX_BODY_BYTES]
    raise ValueError("请求缺少 body_b64")


def handle_request(req: dict[str, Any]) -> dict[str, Any]:
    """处理一个请求, 返回一个响应对象(恰好一行 JSON)。绝不抛错。"""
    rid = str(req.get("id", "") or "")
    op = str(req.get("op", "parse") or "parse")

    if op == "ping":
        return {"id": rid, "ok": True, "version": WORKER_VERSION,
                "python": platform.python_version()}
    if op == "shutdown":
        return {"id": rid, "ok": True}
    if op != "parse":
        _log(f"未知 op: {op}")
        return {"id": rid, "ok": False, "error": "unknown op", "error_type": "BadRequest"}

    t0 = time.perf_counter()
    raw_spec = req.get("spec")
    spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    shape = str(req.get("shape") or "").strip()
    if not shape:
        mt = str(req.get("media_type") or "").split(";")[0].strip().lower()
        shape = SHAPE_BY_MEDIA_TYPE.get(mt, "object")

    try:
        body = _decode_body(req)
    except ValueError as e:
        return {"id": rid, "ok": False, "error": str(e), "error_type": "DecodeError"}

    try:
        max_rows = int(spec.get("max_rows") or DEFAULT_MAX_ROWS)
    except (TypeError, ValueError):
        max_rows = DEFAULT_MAX_ROWS
    try:
        limit = int(spec.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    fields = spec.get("fields") or []
    if not isinstance(fields, list):
        fields = []
    item_path = spec.get("item_path") or None
    total_field = spec.get("total_field") or None
    item_shape = str(spec.get("item_shape") or "list")
    item_flatten = bool(spec.get("item_flatten") or False)

    try:
        payload = parse_body(body, shape, max_rows)
        data, total, truncated = project(
            payload, item_path=item_path, total_field=total_field,
            item_shape=item_shape, limit=limit, fields=fields,
            item_flatten=item_flatten)
    except NotJSONError as e:
        # 上游正文不是 JSON: 走既有的 NDJSON 错误通道, 让 Go 网关能区分
        # "上游给了 HTML 错误页"与"确实没有数据"。
        _log(f"上游正文不是 JSON: {e.preview[:80]}")
        return {"id": rid, "ok": False, "error": str(e),
                "error_type": "NotJSON", "body_preview": e.preview}
    except Exception as e:                      # 兜底: 解析异常也要回一行
        _log(f"解析失败: {type(e).__name__}: {e}")
        return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}",
                "error_type": "ParseError"}

    items = data if isinstance(data, list) else []
    resp: dict[str, Any] = {
        "id": rid,
        "ok": True,
        "items": items,
        "total": total,
        "truncated": bool(truncated),
        "fields": [str(f) for f in fields] if fields else infer_fields(items),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3),
    }
    if not isinstance(data, list):
        resp["data"] = data                     # XML 投影等非数组结果
    return resp


def handle_line(line: str) -> dict[str, Any]:
    """一行请求 -> 一个响应对象。坏行也回一行错误响应, 绝不静默。"""
    line = line.strip()
    if not line:
        return {"id": "", "ok": False, "error": "empty request line", "error_type": "BadRequest"}
    try:
        req = json.loads(line)
    except ValueError as e:
        _log(f"请求不是合法 JSON: {e}")
        return {"id": "", "ok": False, "error": f"invalid json: {e}", "error_type": "BadRequest"}
    if not isinstance(req, dict):
        return {"id": "", "ok": False, "error": "request must be a JSON object",
                "error_type": "BadRequest"}
    try:
        return handle_request(req)
    except Exception as e:                      # 理论上不会到这(handle_request 已兜底)
        _log(f"未预期异常: {type(e).__name__}: {e}")
        return {"id": str(req.get("id", "") or ""), "ok": False,
                "error": f"{type(e).__name__}: {e}", "error_type": "ParseError"}


def _emit(resp: dict[str, Any]) -> None:
    """写一行响应到 stdout(协议专用通道), UTF-8 编码, 不夹带换行。"""
    line = json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n"
    buf = getattr(sys.stdout, "buffer", None)
    try:
        if buf is not None:
            buf.write(line.encode("utf-8"))
            buf.flush()
        else:
            sys.stdout.write(line)
            sys.stdout.flush()
    except BrokenPipeError:                     # 对端关掉了管道
        raise SystemExit(0) from None


def _peek_op(raw_line: str) -> str:
    """只取 op(用来判断 shutdown); 解析失败就当不是。"""
    try:
        req = json.loads(raw_line)
    except ValueError:
        return ""
    return str(req.get("op", "")) if isinstance(req, dict) else ""


def main(argv: list[str] | None = None) -> int:
    """读 stdin 的请求行, 逐个回响应行; 直到 EOF 或 shutdown。"""
    _log(f"启动 (python {platform.python_version()}, worker v{WORKER_VERSION})")
    for raw_line in sys.stdin:
        raw_line = raw_line.rstrip("\r\n")
        op = _peek_op(raw_line)
        _emit(handle_line(raw_line))             # 每个请求恰好一行响应
        if op == "shutdown":
            _log("收到 shutdown, 退出")
            break
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:                   # Ctrl-C: 干净退出
        sys.exit(0)
