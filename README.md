# a-source-gateway

**A 类(法规强制公开)数据源的通用网关** —— 不存储业务数据,只保留源元数据索引;
数据按需实时取,**单次调用硬预算 ≤500ms**;同时提供 **HTTP API** 与 **MCP server**。
运行时服务 **117 个源**(海外清单 107 + 国内清单已接入 10);国内清单另有 24 个候选待接入。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Go](https://img.shields.io/badge/go-1.22%2B-00ADD8.svg)](https://go.dev/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](docs/meta-schema.md)
[![Data sources](https://img.shields.io/badge/sources-117%20%2F%2010%20families-orange.svg)](data/source_inventory.csv)

---

## 为什么需要它

"法规强制公开"的数据源(SEC EDGAR、Companies House、GLEIF、OFAC、ClinicalTrials.gov、
OpenAlex、Crossref……)有个共同特征:**数据本身免费且公开,但每个源的接口形态、限速口径、
增量策略、返回结构都不一样**。直接对接意味着为每个源写一遍客户端,还要自己踩限速的坑。

本项目把这些差异收敛成**一层统一信封**,并把"接口怎么调、返回什么、多大、多久更新"抽成
**声明式元数据**,让调用方(包括 LLM)可以先读元数据再决定怎么查。

```
                    ┌──────────────────────────────────────────┐
  调用方 ──HTTP/MCP─▶│ ① Go 网关面(传输与调度)                │
                    │   路由 · 元数据 · 缓存 · 限速 · 预算      │
                    │   117 源 / 164 端点 / ≤500ms 硬预算       │
                    └───────────────────┬──────────────────────┘
                                        │ 原始字节
                    ┌───────────────────▼──────────────────────┐
                    │ ② Python 解析面(内容与结构)             │
                    │   JSON · XML · CSV · TSV · JSONL         │
                    └───────────────────┬──────────────────────┘
                                        ▼
                       上游官方 API(声明 UA + 按站限速)
```

**核心契约:任何一次调用都在 500ms 内返回。** 做不到时不是超时挂死,而是返回带
`meta.degraded=true` 与 `meta.fallback.upstream_url` 的信封——调用方可以立刻直连上游。

**分工一句话:Go 做网关,Python 做解析。** 两边读**同一批** `data/` 元数据文件,
不存在两份真相。

---

## 快速开始

### 方式一:容器(推荐)

```bash
make up                       # docker compose up -d --build
curl -s http://127.0.0.1:8848/livez
```

### 方式二:本地跑 Go 网关

```bash
# Python 解析面(网关会把它作为子进程拉起)
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Go 网关
make gateway-run              # http://127.0.0.1:8848
```

### 方式三:只用 Python(不装 Go)

```bash
pip install -e ".[mcp]"
a-source-gateway serve
```

### 用起来

```bash
# 看看有哪些源 —— q 可以是中文关键词、源 id、别名("szse")或端点 id("stock_list")
curl -s "http://127.0.0.1:8848/v1/sources?q=制裁" | python3 -m json.tool | head -30

# 搜到的结果里带 aliases,直接告诉你该用哪个简称去调用
curl -s "http://127.0.0.1:8848/v1/sources?q=szse" | python3 -m json.tool | head -20

# 看某个端点要传什么参数、返回什么结构
curl -s http://127.0.0.1:8848/v1/sources/crossref/endpoints/works | python3 -m json.tool

# 执行查询
curl -s -X POST http://127.0.0.1:8848/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"source":"crossref","endpoint":"works",
       "params":{"filter":"from-index-date:2026-10-21"},"limit":3}' | python3 -m json.tool
```

### 接 MCP

```jsonc
{
  "mcpServers": {
    "a-source-gateway": {
      "command": "a-source-gateway-mcp",
      "args": []
    }
  }
}
```

| MCP 工具 | 参数 | 说明 |
|---|---|---|
| `list_sources` | `family?` `q?` `limit?` | 列源 |
| `get_source_meta` | `source` | 完整 meta(认证/限速/规模/增量/抓取方式/代理策略/风险) |
| `describe_endpoint` | `source` `endpoint` | 端点的请求参数与响应结构 |
| `query_source` | `source` `endpoint` `params?` `fields?` `limit?` `since?` `timeout_ms?` | 执行查询,返回信封 |
| `get_meta_schema` | — | meta 规范本身 |

**推荐调用序**:`list_sources` / `get_source_meta` 定位 → `describe_endpoint` 看参数 →
`query_source` 取数。MCP server 优先转发到 Go 网关,网关不可达时回落到进程内的 Python 实现。

### 不用服务也能用

```bash
a-source-gateway list --family 医药          # 列源
a-source-gateway show crossref               # 看一个源的完整 meta
a-source-gateway show ctgov --json           # JSON 输出
a-source-gateway validate                    # 校验数据文件
a-source-gateway build-registry -o r.json     # 导出注册表
a-source-gateway bench                       # 跑延迟基准
```

```python
from a_source_gateway import Resolver, QueryRequest, build_registry

r = Resolver(build_registry())
await r.startup()
env = await r.query(QueryRequest(source="openalex", endpoint="works",
                                  params={"filter": "from_publication_date:2026-09-22"}))
print(env.meta.total, env.meta.latency_ms, env.meta.cached)
```

---

## 🤖 让 AI 智能体帮你加数据源

这是本项目的核心用法之一:**你只需要对 MCode 或 Claude Code 说一句"把 xxx 网站加进来"。**

智能体读完 [`AGENTS.md`](AGENTS.md) 之后,会走这条流水线:

```
① 侦察   python3 scripts/recon.py <url> -o /tmp/recon.json     探测可达性/认证/限速/响应结构
② 补语义  (智能体判断)                                          族/机构/限速口径/规模口径/增量策略
③ 草稿   python3 scripts/scaffold_source.py --recon ...         生成 CSV 行 + 端点定义
④ 校验   make validate                                          数据文件结构与一致性
⑤ 验证   python3 scripts/verify_source.py "<源名>"              真实请求 + 断言(证据关卡)
⑥ 部署   make up                                                重建并重启
⑦ 交付   python3 scripts/demo.py "<源名>"                       调用示例 + 真实数据
```

草稿落到哪份清单由 `--country` 决定(`--country '中国'` → 国内清单,多一列 `优先级`);
国内源的 `--apply` 只进候选池,还要补端点定义与别名才接入网关 —— 见
[`AGENTS.md`](AGENTS.md) 规则 3.1。

第 ⑤ 步是**不可绕过的证据关卡**:降级的调用不算通过;"连不上上游"和"源有问题"是两个
不同的结论。第 ⑦ 步的输出就是智能体交给你的东西 —— **可复制的 curl 命令 + 真实响应**。

前提是:目标站点**允许程序化访问**。合规判断由人来做,不自动化(见
[`GOVERNANCE.md`](GOVERNANCE.md) §4)。

---

## 响应信封

所有调用返回同一个形状,`meta` 描述**这次请求**,`data` 只放结果。

```jsonc
{
  "meta": {
    "request_id": "6921f879dc47",
    "schema_version": "1.0",
    "source": "crossref",
    "endpoint": "works",
    "upstream": {
      "url": "https://api.crossref.org/works?filter=from-index-date%3A2026-10-21&rows=25",
      "method": "GET", "status": 200, "latency_ms": 812
    },
    "latency_ms": 3,            // 客户端可见总耗时
    "budget_ms": 500,           // 本次预算(≤500)
    "cached": true,             // 是否命中缓存
    "cache_age_s": 42,
    "degraded": false,          // 是否降级
    "offline": false,
    "count": 3,                 // 本次返回条数
    "total": 1734280,           // 上游记录总数
    "truncated": true,          // 是否被 limit 截断
    "next_cursor": null,
    "parser": "python",         // 实际解析引擎(python | go)
    "trace_id": "9f2c…",        // 链路追踪 id
    "rate_limit": {},
    "fallback": {},             // 降级时的直连兜底信息
    "errors": []                // [{type, message, status}]
  },
  "data": [ ... ],
  "fields": ["DOI", "publisher"]
}
```

完整字段定义见 [`docs/meta-schema.md`](docs/meta-schema.md),或运行时 `GET /v1/schema`。
机器可读的 API 描述见 `GET /openapi.json`(OpenAPI 3.1,Go/Python 两平面同一路径,文档从路由表生成,不会漂移)。

---

## 500ms 契约是怎么保证的

上游实测延迟参差:SEC 924ms、OpenAlex 934ms、Crossref 1576ms、Eurostat 5839ms。
单次实时调用压不进 500ms,所以:

| 层 | 延迟 | 机制 |
|---|---|---|
| 元数据 | **0.5–3ms** | 本地内存索引,运行期由 CSV + JSON 构建(约 6ms) |
| 热路径 | **0.5–10ms** | 缓存命中直接返回 |
| 冷路径 | **≤406ms** | 上游取数有 400ms 墙钟上限,预留 100ms 给解析/序列化 |

四条实现要点(踩过的坑都在文档里):

1. **总墙钟 deadline**:Python 用 `asyncio.timeout`、Go 用 `context.WithTimeout` ——
   而不是 httpx 的 per-request timeout。后者只约束单次 socket 操作,慢速大响应体
   (Eurostat 2MB 目录)会超时到天荒地老。
2. **客户端不设 timeout**:客户端级 read timeout 会独立于预算掐断慢响应,还会让后台补数静默失败。
3. **上游页大小归一化**:网关统一按固定页大小取,`limit` 在网关侧切 —— 缓存命中率大幅提升。
4. **后台补数**:用户请求超时后,后台用 15s 预算补一次数并入缓存,下次请求秒回。

实测(`make bench-gateway`,**对 Go 网关(主运行时)**跑,见 [`docs/performance.md`](docs/performance.md)):

| 指标 | 实测 |
|---|---|
| 元数据 p95 最差 | 3.0ms |
| 热路径 query p95 最差 | 2.4ms(中位 1.1ms) |
| 冷路径(每源首调)最差 | 410.2ms |
| 超预算用例 | **0** |
| 热路径有数据用例 | 8 / 10 |

热路径的 p95 在**毫秒级**,因为命中缓存后完全不碰网络;冷路径受上游真实延迟约束 ——
上游超过 400ms 预算时网关**降级并给出直连兜底**,而不是让调用方干等(这正是 500ms 契约的意义)。

> 基准机在中国,到欧美上游的实测往返是 0.4–7.5s,因此冷路径里 18/20 个用例是
> **降级态**(调用方拿到 `meta.degraded=true` + 直连 URL),不是网关故障。
> 隔离复测(单源、间隔访问)完全正常。详见 [`docs/performance.md`](docs/performance.md) §3.1。

---

## 数据源覆盖

117 个源 / 10 大族,官方入口逐条实测校验(86 个直连 200、5 个其他可达响应(202/302/307/405)、
15 个需 key 或浏览器 UA(401/403)、3 个本机网络层不可达,**0 个死链**):

| 族 | 源数 | 代表 |
|---|---|---|
| 证券监管与投资披露 | 19 | SEC EDGAR / FINRA / FCA / CSRC / MAS |
| 企业注册与主体标识 | 10 | Companies House / GLEIF LEI / 各州注册局 / SAM.gov |
| 制裁与合规黑名单 | 10 | OFAC SDN / EU FSFL / UN / 世行除名 / BIS |
| 土地房产 | 6 | HM Land Registry / ROS / Kadaster |
| 医药与生命科学 | 10 | ClinicalTrials.gov / openFDA / EMA / WHO GHO |
| 专利与知识产权 | 7 | PatentsView / USPTO Bulk / EPO OPS / WIPO |
| 学术与文献 | 11 | OpenAlex / Crossref / arXiv / PubMed / Europe PMC |
| 宏观贸易与统计 | 16 | Eurostat / WDI / FRED / Comtrade / ECB |
| 司法与政府公开 | 10 | CourtListener / Federal Register / EUR-Lex |
| 环境能源与自然系统 | 10 | EIA / EPA / USGS / NOAA / NASA / GBIF |

完整清单见 [`data/source_inventory.csv`](data/source_inventory.csv)(海外源)与
[`data/source_inventory_cn.csv`](data/source_inventory_cn.csv)(国内源),每行含:官方入口、
认证方式、官方限速口径、更新频率、实测规模、实测增量、代理策略、合规风险、链接状态。

两份清单**按 `国别` 分工**:写 `中国` 的源落国内清单,其余落海外清单 —— 同一个源只出现在
一份清单里。国内清单里**在 `data/endpoints.json` 有端点定义**的行才算已接入并被网关加载,
其余是候选(不进入运行时)。

---

## 合规立场

- 本项目**只访问官方公开接口**,不绕过任何认证、不伪造身份、不规避限速。
- 每个源都声明了官方公布的限速口径,网关按站限速;**不提供也不建议使用代理池提速**。
- 源标识宽松解析(简称、别名)只是**方便调用方定位**,不改变任何请求语义。
- 唯一"非直连"的行为是:上游超时后,**后台**用更长预算补一次数 —— 目的是让下次调用更快,
  不是提高对上游的请求频率。
- 数据版权属各自权利人;本项目不托管、不再分发任何一方的原始数据(见 [NOTICE](NOTICE))。
- 新增源要过合规闸:**是否允许程序化访问必须由人给出理由**并写进 PR。

---

## 配置

全部走环境变量,无需配置文件:

| 变量 | 默认 | 说明 |
|---|---|---|
| `A_GATEWAY_HOST` / `A_GATEWAY_PORT` | 127.0.0.1 / 8848 | 监听地址 |
| `A_GATEWAY_BUDGET_MS` | 500 | 对外总预算(毫秒) |
| `A_GATEWAY_UPSTREAM_BUDGET_MS` | 400 | 上游取数墙钟上限 |
| `A_GATEWAY_RESERVE_MS` | 100 | 留给解析/序列化/框架的余量 |
| `A_GATEWAY_CONTACT` | — | **建议设置**:会写进 User-Agent,上游据此识别你 |
| `A_GATEWAY_UA` | 自动生成 | 自定义 User-Agent |
| `A_GATEWAY_OFFLINE` | 0 | 设为 1 则完全不发网络请求(只供元数据与缓存) |
| `A_GATEWAY_CACHE_ENTRIES` / `_BYTES` | 512 / 32MB | 缓存双上限 |
| `A_GATEWAY_CACHE_TTL_S` | 900 | 默认缓存时长 |
| `A_GATEWAY_PREWARM` | 1 | 启动后后台预热 |
| `A_GATEWAY_DATA_DIR` | 自动探测 | 数据文件目录 |
| `A_GATEWAY_PARSER` | auto | 解析引擎:`auto` / `python` / `go` |
| `A_GATEWAY_PARSER_PYTHON` | python3 | 解析面使用的 Python 可执行文件 |
| `A_GATEWAY_URL` | http://127.0.0.1:8848 | MCP server 转发到的网关地址 |
| `A_GATEWAY_VERIFY` | true | 设为 `none` 关闭 TLS 校验(**生产环境不要**) |
| `A_GATEWAY_HTTP2` | 1 | 上游走 HTTP/2(同主机多次调用复用一条连接);个别上游 h2 有问题时设 `0` |

> **建议生产环境设置 `A_GATEWAY_CONTACT`**:SEC、Crossref、OpenAlex 等都会在 UA 里识别
> 请求方,带联系方式属于负责任的使用方式,也更容易拿到官方支持。

---

## 文档

**上手最快的方式是打开文档站**(快速部署 / API 用法 / 在线调试 / 加源流程):

```bash
make docs                 # 起在 http://127.0.0.1:8000/(带网关代理, 调试台可直接用)
```

也可以直接看静态文件:`site/index.html`。零外部依赖,不引 CDN,离线可读。

| 文档 | 内容 |
|---|---|
| [**文档站 `site/`**](site/index.html) | **快速部署 / API 参考 / 信封与降级 / 在线调试台 / MCP 集成 / 加源指南** |
| [`AGENTS.md`](AGENTS.md) | **智能体操作手册** —— "把 xxx 网站加进来"的完整流程 |
| [`docs/architecture.md`](docs/architecture.md) | 方案文档:双平面架构、模块、数据流、协议、部署、演进 |
| [`docs/tech-selection.md`](docs/tech-selection.md) | 技术选型:21 条 ADR(候选对比、决策与代价) |
| [`docs/optimization-report.md`](docs/optimization-report.md) | 现状指标、优化空间与可行性、优先级与投入产出 |
| [`docs/meta-schema.md`](docs/meta-schema.md) | meta 规范:源/端点/请求/响应信封的字段定义 |
| [`docs/performance.md`](docs/performance.md) | 性能基线、500ms 契约与压测方法 |
| [`docs/adding-a-source.md`](docs/adding-a-source.md) | 如何新增一个数据源(脚本 + 字段约束) |
| [`GOVERNANCE.md`](GOVERNANCE.md) | 治理:角色、决策规则、AI 辅助贡献规范 |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | 贡献指南 |
| [`SECURITY.md`](SECURITY.md) | 安全策略与漏洞报告流程 |
| [`deploy/README.md`](deploy/README.md) | 部署 runbook(容器 / systemd / K8s) |

---

## 目录结构

```
a-source-gateway/
├── pyproject.toml              Python 项目根(= 仓库根)
├── python/src/a_source_gateway/  Python 解析面 + MCP + CLI
├── python/tests/                 Python 测试
├── gateway/                      Go 网关(独立 go.mod,只用标准库)
├── data/                         四份事实源(两平面共享)
├── scripts/                      智能体接入工具链
├── site/                         文档站(静态,零依赖)
├── docs/  deploy/  examples/
└── AGENTS.md  CLAUDE.md  GOVERNANCE.md
```

---

## 开发

```bash
pip install -e ".[dev]"

make test              # 单元测试(默认不联网)
make network-test      # 需要访问真实上游的用例
make lint  type        # ruff / mypy
make gateway-test      # Go 单元测试
make parity            # 校验 Go 与 Python 构建出的注册表一致
make validate          # 校验 data/ 下的数据文件
make bench             # 延迟基准
make check             # 提交前全量检查
```

---

## 许可证

代码 MIT,见 [LICENSE](LICENSE)。数据文件的来源声明见 [NOTICE](NOTICE)。
