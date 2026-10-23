# a-source-gateway · Python 解析层

A 类(法规强制公开)数据源通用网关的 **Python 解析层**:包含与 Go gateway 等价的参考实现
(HTTP API + MCP server)、解析器适配器 SDK、以及解析器 sidecar 服务。

单体仓库说明见根目录 [README.md](../README.md)。

## 安装

```bash
pip install -e .            # 仅 HTTP 参考实现
pip install -e ".[mcp]"     # 带 MCP
pip install -e ".[parser]"  # 带解析器适配器(atom feed 等)
pip install -e ".[dev]"     # 开发
```

数据文件默认从仓库根 `../data` 读取;构建 wheel 前执行 `make sync-data` 使其自包含。

## 组件

| 入口 | 说明 |
|---|---|
| `a-source-gateway serve` | 参考实现 HTTP API(:8848) |
| `a-source-gateway-mcp` | MCP server(stdio) |
| `a-source-gateway parser-serve` | 解析器 sidecar(:8849,供 Go gateway 内部调用) |
| `a_source_gateway.adapters` | 解析器适配器 SDK 与内置适配器 |
