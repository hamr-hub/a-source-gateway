"""
mcp_server.py - A 类数据源网关的 MCP 服务(stdio)

暴露 5 个工具, 与 HTTP API 一一对应:
    list_sources       列源(可按族/关键词过滤)
    get_source_meta    取单个源的完整 meta
    describe_endpoint  取端点的请求/响应结构
    query_source       执行查询, 返回信封
    get_meta_schema    取 meta 规范本身

两平面架构: **Go 做网关(传输与调度), Python 做解析(内容与结构)**。
因此本服务默认把工具调用**转发给 Go 网关的 HTTP API**(A_GATEWAY_URL, 默认
http://127.0.0.1:8848), 让"查询"只有一份实现; Go 网关连不上(连接被拒/超时)时
回落到进程内的 Python Resolver, 保证 MCP 端单独也能用。

回落是**可观测的**: 结果 meta.gateway = "go" | "python-fallback",
并且每次回落都会写一行 stderr 日志 —— 不静默降级。

用法(Claude Code / 任意 MCP 客户端):
    {
      "mcpServers": {
        "a-source-gateway": {
          "command": "python3",
          "args": ["-m", "a_source_gateway.mcp_server"],
          "cwd": "/Users/hyx/codespace/CloudHub_01"
        }
      }
    }
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server import Server as _LowLevelServer
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .api import SCHEMA_DOC
from .cache import BoundedCache
from .config import load_config
from .registry import build_registry
from .resolver import Resolver
from .schema import DEFAULT_BUDGET_MS, SCHEMA_VERSION, QueryRequest

CFG = load_config()
OFFLINE = CFG.offline

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8848"
GATEWAY_MODE = "go"
FALLBACK_MODE = "python-fallback"

# mcp 1.x 的低层 Server 有 list_tools()/call_tool() 装饰器; 2.x 换成了
# MCPServer + @server.tool()。两种都支持, 免得升级 SDK 就把 MCP 服务搞挂。
MCP_V2 = not hasattr(_LowLevelServer, "list_tools")

_INSTRUCTIONS = (
    "海外 A 类(法规强制公开)数据源网关。工具返回统一信封: "
    "{meta:{描述本次请求与上游状态}, data:[结果], fields:[字段]}。"
    "不存储业务数据;元数据本地索引,数据按需实时取,单次调用硬预算 ≤500ms。"
    "先用 list_sources / get_source_meta 定位源与端点, 再用 query_source 取数。"
)

if MCP_V2:
    from mcp.server.mcpserver import MCPServer

    server: Any = MCPServer(name="a-source-gateway", version="1.0", instructions=_INSTRUCTIONS)
else:
    server = _LowLevelServer("a-source-gateway", version="1.0", instructions=_INSTRUCTIONS)

_resolver: Resolver | None = None


async def _get_resolver() -> Resolver:
    """本地回落用的 resolver(只有回落到 Python 路径时才会真的建)。"""
    global _resolver
    if _resolver is None:
        reg = build_registry(CFG)
        _resolver = Resolver(reg, offline=OFFLINE, cache=BoundedCache(
            max_entries=CFG.cache_entries, max_bytes=CFG.cache_bytes), cfg=CFG)
        await _resolver.startup()
    return _resolver


def _payload(obj: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(obj, ensure_ascii=False, indent=2))]


def _log(msg: str) -> None:
    """MCP 的 stdout 是协议通道, 日志一律走 stderr。"""
    print(f"[mcp-server] {msg}", file=sys.stderr, flush=True)


# ── Go 网关客户端(标准库 urllib, 不给 MCP 路径加新依赖) ──────────────────
class GatewayUnavailable(Exception):
    """Go 网关连不上(连接被拒/超时/域名解析失败) —— 触发本地回落。"""


class GatewayError(Exception):
    """网关可达, 但返回了错误状态码(业务错误, 不回落)。"""

    def __init__(self, status: int, body: str = ""):
        super().__init__(f"gateway {status}")
        self.status = status
        self.body = body


def _gateway_base_url() -> str:
    """Go 网关地址: 环境变量优先, 其次配置文件, 最后默认值。"""
    url = os.environ.get("A_GATEWAY_URL") or CFG.gateway_url or DEFAULT_GATEWAY_URL
    return url.rstrip("/")


def _http_json(method: str, path: str, payload: dict[str, Any] | None = None,
               timeout: float | None = None) -> Any:
    """调 Go 网关的 HTTP API; 连不上抛 GatewayUnavailable, 错误码抛 GatewayError。"""
    url = f"{_gateway_base_url()}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout or CFG.gateway_timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:          # 网关活着, 但是 4xx/5xx
        body = e.read().decode("utf-8", "replace")[:300]
        raise GatewayError(e.code, body) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GatewayUnavailable(f"{type(e).__name__}: {e}") from e
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        raise GatewayUnavailable(f"网关返回的不是 JSON: {e}") from e


async def _call_gateway(name: str, args: dict[str, Any]) -> Any:
    """MCP 工具 -> Go 网关路由。"""
    if name == "list_sources":
        query = urllib.parse.urlencode({
            "family": args.get("family", "") or "",
            "q": args.get("q", "") or "",
            "limit": int(args.get("limit", 50)),
        })
        return await asyncio.to_thread(_http_json, "GET", f"/v1/sources?{query}")
    if name == "get_source_meta":
        sid = urllib.parse.quote(str(args.get("source", "") or ""), safe="")
        return await asyncio.to_thread(_http_json, "GET", f"/v1/sources/{sid}")
    if name == "describe_endpoint":
        sid = urllib.parse.quote(str(args.get("source", "") or ""), safe="")
        eid = urllib.parse.quote(str(args.get("endpoint", "") or ""), safe="")
        return await asyncio.to_thread(_http_json, "GET", f"/v1/sources/{sid}/endpoints/{eid}")
    if name == "query_source":
        body = {
            "source": args.get("source", "") or "",
            "endpoint": args.get("endpoint", "") or "",
            "params": args.get("params") or {},
            "fields": args.get("fields") or [],
            "limit": int(args.get("limit", 20)),
            "since": args.get("since"),
            "timeout_ms": int(args.get("timeout_ms", DEFAULT_BUDGET_MS)),
        }
        return await asyncio.to_thread(_http_json, "POST", "/v1/query", body)
    if name == "get_meta_schema":
        return await asyncio.to_thread(_http_json, "GET", "/v1/schema")
    raise GatewayError(404, f"未知工具 {name}")


# ── 本地回落(进程内 Resolver) ───────────────────────────────────────────
async def _call_python(name: str, args: dict[str, Any]) -> Any:
    """Go 网关不可达时的本地实现 —— 与改造前的行为完全一致。"""
    r = await _get_resolver()
    if name == "list_sources":
        items = r.list_sources(family=args.get("family", "") or "", q=args.get("q", "") or "")
        limit = int(args.get("limit", 50))
        return {"meta": {"count": len(items), "schema_version": SCHEMA_VERSION},
                "data": items[:limit]}
    if name == "get_source_meta":
        s = r.get_source(args.get("source", "") or "")
        if not s:
            return {"meta": {"error": f"未知源 {args.get('source')}"}, "data": None}
        return {"meta": {"source": s["id"], "schema_version": SCHEMA_VERSION}, "data": s}
    if name == "describe_endpoint":
        d = r.describe(args.get("source", "") or "", args.get("endpoint", "") or "")
        if not d:
            return {"meta": {"error": "未知源或端点"}, "data": None}
        return {"meta": {"source": args.get("source"), "endpoint": args.get("endpoint")},
                "data": d}
    if name == "query_source":
        req = QueryRequest(
            source=args.get("source", "") or "",
            endpoint=args.get("endpoint", "") or "",
            params=args.get("params") or {},
            fields=args.get("fields") or [],
            limit=int(args.get("limit", 20)),
            since=args.get("since"),
            timeout_ms=int(args.get("timeout_ms", DEFAULT_BUDGET_MS)),
        )
        env = await r.query(req)
        return env.to_dict()
    if name == "get_meta_schema":
        return SCHEMA_DOC
    return {"meta": {"error": f"未知工具 {name}"}, "data": None}


# 5 个工具的显式定义(单一事实源): 名字/标题/描述/入参 schema
_TOOL_SPECS: list[dict[str, Any]] = [
        {
            "name": "list_sources",
            "title": "列出 A 类数据源",
            "description": "按分类或关键词列出可用数据源(轻量投影, 不含端点详情)。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "family": {"type": "string", "description": "分类关键词, 如 '制裁'"},
                    "q": {"type": "string",
                          "description": "关键词, 搜名称/别名/源 id/端点 id/描述/机构/国别"},
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                },
            },
        },
        {
            "name": "get_source_meta",
            "title": "取数据源元数据",
            "description": "返回单个源的完整 meta: 认证方式、官方限速、规模、增量、抓取方式、代理策略、合规风险与端点列表。",
            "inputSchema": {
                "type": "object",
                "properties": {"source": {"type": "string", "description": "源 id"}},
                "required": ["source"],
            },
        },
        {
            "name": "describe_endpoint",
            "title": "描述端点请求与响应",
            "description": "返回端点的请求参数(名称/位置/类型/必填/正则/示例)与响应结构(字段/列表路径/总数字段)。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "endpoint": {"type": "string"},
                },
                "required": ["source", "endpoint"],
            },
        },
        {
            "name": "query_source",
            "title": "查询数据源",
            "description": "按端点定义发起查询, 返回信封 {meta, data, fields}。硬预算 ≤500ms, 超时自动降级并标注 degraded=true。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "params": {"type": "object", "description": "端点参数, 键为参数名"},
                    "fields": {"type": "array", "items": {"type": "string"}, "description": "只返回这些字段"},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 500},
                    "since": {"type": "string", "description": "增量窗口起点, 如 2026-10-01"},
                    "timeout_ms": {"type": "integer", "default": DEFAULT_BUDGET_MS,
                                   "minimum": 50, "maximum": DEFAULT_BUDGET_MS},
                },
                "required": ["source", "endpoint"],
            },
        },
        {
            "name": "get_meta_schema",
            "title": "取 meta 规范",
            "description": "返回网关自身的 meta 规范(源/端点/请求/响应信封的字段定义)。",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]




def _make_tool(spec: dict[str, Any]) -> Any:
    """按当前 SDK 版本构造 Tool(1.x 叫 inputSchema, 2.x 叫 input_schema)。"""
    data = dict(spec)
    if MCP_V2:
        data["input_schema"] = data.pop("inputSchema")
    return Tool(**data)


def _tool_defs() -> list[Any]:
    """5 个工具的显式定义(mcp 1.x 直接用它; 2.x 用同一份 title/description)。"""
    return [_make_tool(spec) for spec in _TOOL_SPECS]


# ── 派发: 先打 Go 网关, 打不通就回落本地 resolver ────────────────────────
async def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = await _call_gateway(name, args)
        mode = GATEWAY_MODE
    except GatewayUnavailable as e:
        _log(f"Go 网关不可达({_gateway_base_url()}): {e}; 回落到本地 Python resolver")
        payload = await _call_python(name, args)
        mode = FALLBACK_MODE
    except GatewayError as e:
        _log(f"Go 网关返回错误状态 {e.status}: {e.body}")
        payload = {"meta": {"error": f"网关返回 {e.status}", "detail": e.body}, "data": None}
        mode = GATEWAY_MODE

    if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
        payload["meta"]["gateway"] = mode      # 回落路径可观测, 不静默降级
    _log(f"tool={name} gateway={mode}")
    return payload


class _CompatTool:
    """mcp 2.x 的工具字段叫 input_schema(下划线), 1.x 叫 inputSchema(驼峰)。

    对外统一按 1.x 的字段名暴露, 调用方不必跟着 SDK 版本改代码。
    """

    def __init__(self, tool: Any):
        self._tool = tool
        self.name = tool.name
        self.title = getattr(tool, "title", None)
        self.description = tool.description or ""
        self.inputSchema = (getattr(tool, "input_schema", None)
                            or {"type": "object", "properties": {}})
        self.input_schema = self.inputSchema
        self.outputSchema = getattr(tool, "output_schema", None)
        self.output_schema = self.outputSchema


async def list_tools() -> list[Any]:
    """工具清单。mcp 1.x 返回显式 Tool; 2.x 从 SDK 注册表取并包一层兼容壳。"""
    if MCP_V2:
        return [_CompatTool(t) for t in await server.list_tools()]
    return _tool_defs()


async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """执行一个工具, 返回 TextContent 列表(mcp 1.x 的处理器同签名)。"""
    return _payload(await _dispatch(name, arguments or {}))


# ── SDK 注册 ─────────────────────────────────────────────────────────────
async def _t_list_sources(family: str = "", q: str = "", limit: int = 50) -> dict[str, Any]:
    return await _dispatch("list_sources", {"family": family, "q": q, "limit": limit})


async def _t_get_source_meta(source: str) -> dict[str, Any]:
    return await _dispatch("get_source_meta", {"source": source})


async def _t_describe_endpoint(source: str, endpoint: str) -> dict[str, Any]:
    return await _dispatch("describe_endpoint", {"source": source, "endpoint": endpoint})


async def _t_query_source(source: str, endpoint: str, params: dict[str, Any] | None = None,
                          fields: list[str] | None = None, limit: int = 20,
                          since: str | None = None,
                          timeout_ms: int = DEFAULT_BUDGET_MS) -> dict[str, Any]:
    return await _dispatch("query_source", {
        "source": source, "endpoint": endpoint, "params": params or {},
        "fields": fields or [], "limit": limit, "since": since, "timeout_ms": timeout_ms})


async def _t_get_meta_schema() -> dict[str, Any]:
    return await _dispatch("get_meta_schema", {})


if MCP_V2:
    # mcp 2.x: 入参 schema 由函数签名推导, 名字/标题/描述沿用同一份定义
    for _fn, _spec in (
        (_t_list_sources, _tool_defs()[0]),
        (_t_get_source_meta, _tool_defs()[1]),
        (_t_describe_endpoint, _tool_defs()[2]),
        (_t_query_source, _tool_defs()[3]),
        (_t_get_meta_schema, _tool_defs()[4]),
    ):
        server.tool(name=_spec.name, title=_spec.title, description=_spec.description,
                    structured_output=False)(_fn)
else:
    # mcp 1.x: 低层 Server 的装饰器
    server.list_tools()(list_tools)
    server.call_tool()(call_tool)


async def main() -> None:
    if MCP_V2:
        await server.run_stdio_async()
        return
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
