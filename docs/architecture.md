# 方案文档:a-source-gateway

> 版本 1.1 ｜ 面向开源发布 ｜ 2026-10
> v1.1 的核心变化:**双语言分层** —— Go 做网关,Python 做解析;并新增
> "智能体可自助接入新站点"的流水线。变更决策见 [`tech-selection.md`](tech-selection.md)
> 第二部分(ADR-015 ~ ADR-022)。

---

## 1. 问题陈述

"法规强制公开"(下文称 **A 类**)的数据源有 109 个,分布在 10 个族:证券监管、公司注册、
制裁名单、土地房产、临床试验、专利、学术文献、宏观统计、司法公开、环境能源。

它们的共同点是**数据免费且公开**,但接口形态各异:

| 差异维度 | 举例 |
|---|---|
| 认证 | 无 key / 免费 key / OAuth token / 需邮箱 / 付费 |
| 限速维度 | 按 IP(3 次/秒) / 按 key(600 次/5 分钟) / 按流量池(4000 次/12 小时) / 按每日额度(10 万 credits/日) |
| 响应形态 | JSON / Atom XML / CSV / TSV / 29MB XML |
| 结构位置 | `message.items` / `hits.hits` / `data` / `value` / `results` / 顶层字典值域 |
| 增量口径 | 按日索引 / 按日期字段 / 按游标 / 只有全量快照 / 只有月度 diff |

对每个源写一遍客户端,意味着:100+ 份请求代码、100+ 份解析代码、100+ 份限速配置,
还要自己踩"字段藏在三层嵌套里"和"服务端其实按 key 计数"的坑。

## 2. 目标与非目标

### 目标

1. **统一信封**:所有源返回同一个 `{meta, data, fields}` 结构,`meta` 完整描述这次请求。
2. **元数据先行**:调用方(或 LLM)能先读到"这个源怎么调、返回什么、多大、多久更新、
   要不要 key、限速多少",再决定怎么查。
3. **500ms 契约**:任何一次调用都在 500ms 内返回。做不到时降级并给出直连兜底,而不是挂死。
4. **不落业务数据**:只保留元数据索引(约 120 KiB),不镜像任何一方的原始数据。
5. **零绕**:不绕过认证、不伪造身份、不规避限速。
6. **可自助扩展**:新增一个源应当是**数据操作 + 脚本验证**,而不是写代码 ——
   任何人(或任何 AI 智能体)都能完成。详见 [`AGENTS.md`](../AGENTS.md)。

### 非目标

- 不做爬虫(页面型抓取是 `class_a_fetcher` 这类工具的活)。
- 不做数据仓库/BI(不存业务数据)。
- 不做代理池(本项目**明确反对**用代理池绕过官方限速,见 §8)。

---

## 3. 架构:双平面

```
┌────────────────────────────────────────────────────────────────────────┐
│  调用方        HTTP / MCP            MCP 由 Python 适配,转发到 Go 网关  │
└───────────────────────┬────────────────────────────────────────────────┘
                        │  统一 QueryRequest → ResponseEnvelope
┌───────────────────────▼────────────────────────────────────────────────┐
│  ① Go 网关面  gateway/                                                │
│     路由 · 注册表 · 缓存(SWR + 自适应 TTL) · 按站限速 ·                │
│     总墙钟预算 · 上游取数(正文上限) · Prometheus 指标 · OpenAPI        │
│                                                                        │
│     ① 渲染 URL(参数模板 + 页大小归一 + 缓存键归一)                     │
│     ② 缓存查找(新鲜 / 陈旧→SWR / 未命中)                               │
│     ③ 上游取数(context deadline 总墙钟, 正文上限)                      │
│     ④ 交给 Python 解析面 → ⑤ 投影与信封 → ⑥ 降级 → ⑦ 后台补数 → ⑧ 观测 │
└───────────────────────┬────────────────────────────────────────────────┘
                        │ 原始字节(raw bytes)
┌───────────────────────▼────────────────────────────────────────────────┐
│  ② Python 解析面  python/src/a_source_gateway/parser_worker.py         │
│     JSON · XML · CSV · TSV · JSONL · text → 规范化条目                  │
│     item_path / total_field / item_shape / limit / fields 投影         │
│     (Go 侧保留一份等价的 JSON 兜底实现,见 ADR-018)                     │
└────────────────────────────────────────────────────────────────────────┘
                        │
┌───────────────────────▼────────────────────────────────────────────────┐
│  ③ 元数据层(两份事实源,两平面共享)                                      │
│     data/source_inventory.csv · data/endpoints.json · data/aliases.json│
└────────────────────────────────────────────────────────────────────────┘
```

### 3.1 为什么分成两个平面

| 关注点 | 归属 | 理由 |
|---|---|---|
| 并发、连接池、硬 deadline | Go | 网关侧是 I/O 密集;Go 的 goroutine 与 `context` deadline 让"总墙钟"预算表达得最直接 |
| 响应结构识别(XML/CSV/TSV/JSONL) | Python | 解析是内容密集型工作,Python 的标准库(`csv`/`json`/`xml.etree`)表达力与迭代速度更合适 |
| 协议适配(MCP) | Python | MCP 官方 SDK 是 Python 生态最完整的 |
| 贡献者工作流(CLI/校验/接入工具链) | Python | 脚本化工具链用 Python 写最快 |
| 事实源 | 两边共享 | **不存在两份元数据** —— 这是整套设计的地基 |

**关键约束**:两个平面读**同一份** `data/` 文件。Go 侧从 CSV/JSON 直接构建注册表,
不经过任何生成物(ADR-017),因此不存在"生成物与源文件不同步"的老问题。

### 3.2 目录结构

```
a-source-gateway/
├── pyproject.toml                 Python 项目根(= 仓库根)
├── python/
│   ├── src/a_source_gateway/      Python 解析面 + MCP + CLI
│   └── tests/                     Python 测试
├── gateway/                       Go 网关(独立 go.mod,只用标准库)
│   ├── cmd/gateway/               入口
│   └── internal/{config,registry,cache,parser,upstream,
│                 ratelimit,envelope,metrics,server}/
├── data/                          事实源(四个文件,两平面共享)
│   ├── source_inventory.csv       海外清单:107 个源 × 17 列
│   ├── source_inventory_cn.csv    国内清单:34 个源 × 19 列(+优先级),候选池
│   ├── endpoints.json             端点定义(56 个源 / 101 条)
│   └── aliases.json               源名 → 常用简称
├── scripts/                       智能体接入工具链(recon/scaffold/verify/demo)
├── docs/                          方案 / ADR / 优化报告 / meta 规范 / 性能
├── deploy/                        k8s / systemd / runbook
├── Dockerfile, docker-compose.yml, Makefile
└── AGENTS.md                      智能体操作手册
```

> `data/` 放在仓库根、而不是包内,是因为 **Go 网关也要读它**。
> 构建 wheel 时通过 hatchling `force-include` 直接把根目录的 `data/` 打进包里,
> 不做"复制一份进包"的同步步骤 —— 避免出现"仓库里的 data 和 wheel 里的 data 不一致"。

### 3.3 解析面通信协议

Go 网关把 Python 解析面作为**子进程**拉起,用**换行分隔 JSON(NDJSON)**通信:

```
Go 网关 ──stdin──▶ {"id":"r1","op":"parse","shape":"object","media_type":"application/json",
                    "body_b64":"<base64>","spec":{"item_path":"message.items","total_field":"message.total",
                    "item_shape":"list","limit":20,"fields":[],"max_rows":15000}}
          ◀─stdout── {"id":"r1","ok":true,"items":[...],"total":1734280,"truncated":false,
                      "fields":[...],"elapsed_ms":3.2}
```

- **stdout 只走协议,日志一律 stderr** —— 否则会串协议。
- 每个请求产生且只产生**一行**响应;未知 op 返回错误行但**不退出**。
- 子进程崩了 Go 能拉起新的;Go 退出它自然结束。
- `op:"ping"` 用于启动时探活;`op:"shutdown"` 用于优雅退出。

### 3.4 模块职责

**Go 网关面**

| 包 | 职责 | 不负责 |
|---|---|---|
| `internal/registry` | 从三份数据文件构建注册表;别名与认证分类 | 网络访问 |
| `internal/cache` | 有界 TTL/LRU 缓存 + 访问计数 | 知道什么是"数据" |
| `internal/ratelimit` | 按站限速 | 知道上游是谁 |
| `internal/upstream` | 取数:总墙钟 deadline + 正文上限 + UA | 解析响应 |
| `internal/parser` | 解析面客户端(NDJSON 子进程)+ Go JSON 兜底 | HTTP 语义 |
| `internal/envelope` | 信封结构 | — |
| `internal/metrics` | Prometheus 指标 | — |
| `internal/server` | 路由 / 探针 / OpenAPI | 上游知识 |

**Python 解析面**

| 模块 | 职责 |
|---|---|
| `parser_worker.py` | NDJSON 解析服务:原始字节 → 规范化条目 |
| `registry.py` | 注册表构建规则(slugify / classify_auth / parse_total) |
| `schema.py` | 信封与 spec 的权威定义 |
| `resolver.py` | 参照实现:URL 渲染 / 缓存键 / SWR / 降级 / 投影 |
| `mcp_server.py` | MCP 适配(优先走 Go 网关,不可达时回落本地 resolver) |
| `cli.py` | 命令行入口(validate / bench / serve / build-registry) |

> `resolver.py` 是**参照实现**:Go 网关的行为以它为准,两边的行为差异由
> `make parity` 与 CI 守住。

### 3.5 两平面有意分歧

两个平面读**同一份** `data/`,所以**语义**必须一致;但**传输层与安全姿态**上的差异是
有意保留的 —— 主运行时是 Go 网关,Python 参照面/回落路径只在网关不可达时顶班。

#### 有意的分歧

| 事项 | Go 网关(主运行时) | Python 参照面 / 回落路径 | 为什么这样分 |
|---|---|---|---|
| **重定向** | 只跟随**同主机**重定向(`gateway/internal/upstream/upstream.go` 的 `sameHostRedirect`)。跨主机直接拒绝,错误文案是 `redirect to another host is not followed: … (the endpoint template must point at the final URL)`;同主机换端口也拒绝(端口只允许**随 scheme 一起**变,如 http→https) | `httpx.AsyncClient(..., follow_redirects=True)`,**无条件跟随** | **安全姿态**:不能让调用方借一个源端点去够它本不该够的地址。官方源把文件放在自己的 CDN 上时(OFAC 导出接口 302 到带签名的 S3 地址),端点模板要**指向最终地址**;签名地址写不进模板,那种源按"文件型源直连下载"处理(见 ADR-022) |
| **HTTP/2** | 显式 `Transport.ForceAttemptHTTP2 = true`(ALPN 协商),个别上游 h2 有问题时用 `A_GATEWAY_HTTP2=0` 关掉 | HTTP/1.1 —— httpx 要用 h2 得额外装 `h2` 包,本项目不把它列为依赖 | 传输协议**不属于**信封/注册表的语义契约,不值得为它引入一个额外依赖 |

#### 必须一致的语义

| 契约 | 靠什么保证一致 |
|---|---|
| **信封形状与字段语义** | 同一份 `data/` + 同一套 `schema_version`(两平面当前都是 `1.2`);字段定义见 [`meta-schema.md`](meta-schema.md) |
| **注册表**(源 / 端点 / 别名) | `make parity` 把两个平面从 `data/` 构建出的注册表逐项比对(忽略生成时间等易变字段),CI 守住 |
| **错误类型口径** | 同名同义:`timeout` / `connect_error` / `read_error` / `upstream_error` / `upstream_not_json` / `response_too_large` / `stale` / `offline` / `budget_exceeded` / `not_found` / `contact_required` / `missing_param` —— **传输层失败也用这三个共用名字**(见 §4.3)。`parse_error` 两平面都会产生:正文不是端点声明的形状(csv/tsv 拿到 XML/HTML 错误页、jsonl 一行都解析不成功) |
| **校验错误形状** | 两平面都返回 FastAPI 同形的 `{"detail":[{"type":…,"loc":[…],"msg":…,"input":…}]}` |

> **给读者的话**:如果你看到一个"只在某个平面上出现"的行为 —— 先来这里对一下,
> 而不是当成 bug。本节列的就是**登记过的**分歧;没列在这里的行为差异,才是真 bug。

---

## 4. 关键机制

### 4.1 500ms 预算怎么花

```
总预算 500ms
├─ 元数据查询      ~0ms    (内存索引)
└─ 数据查询
   ├─ 缓存命中        ~1ms
   ├─ 缓存过期       ~1ms    (SWR: 返回陈旧值, 后台刷新)
   └─ 缓存未命中
      ├─ 上游取数   ≤400ms   (总墙钟 deadline)
      ├─ 解析      ≤ 60ms   (含跨进程 NDJSON 往返; 正文上限 1MB / CSV 最多 15000 行)
      └─ 序列化     ≤ 40ms   (预留)
```

**关键约束:必须用总墙钟 deadline。** httpx 的 per-request timeout 只约束单次 socket 操作,
慢速持续吐数据的大响应(EUROSTAT 目录 2MB、OFAC XML 29MB)会让总耗时远超预期。
客户端级 timeout 同样危险 —— 它独立于预算把慢响应掐断,导致后台补数**静默失败**。
Go 侧同理:用 `context.WithTimeout` 约束**整次请求**,不设客户端级 timeout。

### 4.2 缓存键归一

缓存键只由**上游请求内容**决定:

- `limit` / `fields` 是网关侧的投影,不进键;
- 分页尺寸参数统一归一(默认 25),所以"要 3 条"和"要 50 条"命中同一条缓存;
- 保留参数 `_cb` 只进键、不发给上游,用于强制冷路径测试。

### 4.3 降级契约

任何一次拿不到完整结果的调用,都必须给出**可执行的下一步**:

```jsonc
"meta": {
  "degraded": true,
  "errors": [{"type": "timeout", "message": "上游超过 400ms 预算, 已降级"}],
  "fallback": {
    "upstream_url": "https://api.crossref.org/works?filter=...",
    "method": "GET",
    "hint": "网关预算内取不到;可直连上游, 或稍后重试(后台正在补数)"
  }
}
```

降级类型:`timeout` / `connect_error` / `read_error` / `upstream_error` /
`upstream_not_json` / `parse_error` / `response_too_large` / `stale` / `offline` /
`budget_exceeded` / `not_found` / `contact_required` / `missing_param`。

> `upstream_not_json` 是 1.3.0 起新增的:上游给了 HTML 错误页(状态码可能是 200),
> 这**不是**空结果, 必须显式标降级。状态码 ≥400 且正文非 JSON 时,
> `upstream_error`(根因)与 `upstream_not_json`(症状)两条都会报。

**传输层失败有三个共用名字**:调用方是按 `errors[].type` 分支的("这是连不上,
还是连上了但中途断了"),所以同一个失败类别在两个平面必须给同一个名字:

| type | 含义 |
|---|---|
| `connect_error` | 域名解析失败 / 连接被拒 / 证书问题 —— 根本没连上 |
| `read_error` | 连上了但中途断了(连接重置 / 服务端提前关闭 / EOF) |
| `upstream_error` | 其余传输层失败;以及上游返回 HTTP ≥400(这一直就是这个名字) |

Go 网关(`gateway/internal/server/resolver.go` 的 `upstreamErrorType()`)与 Python
参照面(`python/src/a_source_gateway/resolver.py` 的 `_transport_error_type()`)逐条对齐,
`python/tests/test_error_taxonomy.py` 守住这张词表不再外扩。
**原始异常文本照旧放在 `errors[].message` 里** —— 各运行时的措辞不同,那是事实,不强行统一。
`timeout` 仍是 `timeout`(两个平面早就是统一的)。

**降级也是有效信息**:调用方知道该直连上游。这比抛 5xx 好 —— 信封形状恒定,
Agent 侧只需写一套解析逻辑(ADR-013)。

### 4.4 后台补数

用户请求超时后,后台以 **30s** 长预算(`A_GATEWAY_WARM_BUDGET_MS`)补一次数并入缓存。关键性质:

- 用户侧的 500ms 承诺**不受影响**(补数在后台);
- 后台补数可以**重试并缩小页大小**(响应超上限时),因此能处理 OpenAlex / Crossref
  这类单条记录很大的源;
- 对 29MB 这种文件型响应,识别为 `response_too_large` 后**不再缓存残片**(残片没有价值
  还占缓存),后续查询 0ms 返回"请直连上游"。

### 4.5 观测

`/metrics` 暴露 Prometheus 文本格式指标(零依赖手写):总查询数、缓存命中、降级、
超时、错误、后台补数成败、平均/最大延迟、命中率,以及**按源**的查询数与降级数。
指标名在 Go 与 Python 两侧保持一致(`asg_*`),便于平滑切换运行时。

---

## 5. 接口

### HTTP

| 路由 | 说明 |
|---|---|
| `GET /` | 服务信息 + 路由表 + MCP 工具清单 |
| `GET /healthz` | 综合健康 |
| `GET /livez` | 存活探针(上游不可达也应 200) |
| `GET /readyz` | 就绪探针(注册表可用才 200) |
| `GET /v1/schema` | meta 规范本身 |
| `GET /v1/stats` | 缓存、预热与**当前解析引擎**状态 |
| `GET /metrics` | Prometheus 文本格式指标(零依赖) |
| `GET /v1/sources?family=&q=&limit=` | 源清单(轻量投影) |
| `GET /v1/sources/{id}` | 单源完整 meta |
| `GET /v1/sources/{id}/endpoints/{eid}` | 端点 meta |
| `POST /v1/query` | 执行查询 → 信封 |
| `GET /v1/query?...` | 同上(查询参数形式) |
| `POST /v1/search` | 搜索:一次请求扇出多个源 → **信封数组 + 汇总 meta** |

> **`POST /v1/search` 是信封形状的唯一例外**:它一次返回多个源的结果,不是单个
> `{meta, data, fields}`。顶层 `meta` 汇总这次扇出(`requested` / `ok` / `degraded` /
> `failed` / `with_data`,其中 `requested = ok + degraded + failed`,三类互斥),
> `results[]` 里每一项**各自是一个源的信封**(`source` / `endpoint` / `degraded` /
> `cached` / `latency_ms` / `count` / `total` / `data` / `fields` / `errors`,
> 降级时还有 `fallback`);`queries[i]` 与 `results[i]` 一一对应、顺序一致。
>
> **单个源失败不影响整次调用**:未知源 / 未知端点 / 上游超时都只写进那一条的 `errors`,
> 整体仍返回 200。这与 `POST /v1/query` 遇未知源返回 404 **刻意不同** —— 搜索要的是
> "一次看多个源",一个源坏了不该让整次调用失败。
> 各源**并发**执行、各自一份 ≤500ms 预算,整体墙钟约等于最慢的那一条。

### MCP

`list_sources` / `get_source_meta` / `describe_endpoint` / `query_source` /
`search_sources` / `get_meta_schema`。

其中 `search_sources` 对应 `POST /v1/search`:给一个关键词在多个源上并行查一遍,
返回**紧凑版**结果(每个源给出 source/endpoint/count/total/degraded/errors,
`data` 按 limit 截断)。

推荐调用序:**先读元数据再查数**。`describe_endpoint` 会给出参数名、位置、类型、必填、
正则、示例值,以及响应里列表与总数所在路径 —— 这些信息足以让调用方不猜字段。

MCP server **优先转发到 Go 网关**(`A_GATEWAY_URL`),网关不可达时回落到进程内的 Python
resolver —— 因此查询逻辑只有一份实现,而 MCP 在没有网关时也仍可用。

---

## 6. 部署

```bash
# 容器(推荐)
make up                 # docker compose up -d --build
curl -s http://127.0.0.1:8848/livez

# 本地
make gateway-run        # Go 网关
make run                # Python 参照实现
```

生产建议:

- 设 `A_GATEWAY_CONTACT=you@example.com`(上游据此识别你,属于负责任的使用方式);
- 用 systemd / Docker / K8s 托管,探针指向 `/livez` 与 `/readyz`;
- 抓 `/metrics` 进 Prometheus,盯 `asg_degraded_total` 与 `asg_source_degraded{source=...}`;
- 镜像内 Go 网关是主进程,Python 解析面是它拉起的子进程;两者版本天然一致;
- 多实例部署时注意缓存是**进程内**的,跨实例不共享(见优化报告 §3.7)。

详见 [`deploy/README.md`](../deploy/README.md)。

---

## 7. 智能体自助接入

本项目要支撑的场景:**"未来有新的网站,只 MCode 或 Claude 打开本项目,对它说'把 xxx 网站加进来',
就能自动分析目标网站数据、把代码改好、更新部署,最后给出调用示例和真实数据。"**

为此,接入流程被固化成**脚本化的流水线 + 仓库级操作手册 + 不可绕过的证据关卡**:

```
recon.py  ──▶  [智能体补语义]  ──▶  scaffold_source.py  ──▶  make validate
                                                                   │
      ┌────────────────────────────────────────────────────────────┘
      ▼
 verify_source.py (证据关卡:真实请求 + 断言)  ──▶  make up (部署)  ──▶  demo.py (调用示例 + 真实数据)
```

设计要点:

1. **证据关卡不可绕过** —— 降级的调用不算验证通过;"连不上上游"与"源有问题"是两个不同的结论。
2. **不许编造** —— 侦察推断的每个字段都带推断依据;无法确定的字段是 `null` + 警告。
3. **人工判断不自动化** —— 限速口径、规模计数口径、增量策略、族归属是**合规与语义判断**。
4. **只改数据文件** —— 加源不碰 `src/` 下的 Python 代码。

完整步骤见 [`AGENTS.md`](../AGENTS.md) §2;脚本用法见 [`adding-a-source.md`](adding-a-source.md)。

---

## 8. 合规立场

1. **只访问官方公开接口**,不绕过认证、不伪造身份、不规避限速。
2. **按站限速**,限速口径来自各源官方文档,写在元数据里。
3. **明确反对代理池提速**:本项目不提供、不建议用代理绕过官方限速。限速是授权条件,
   不是技术障碍。
4. **不托管原始数据**:只存元数据;上游响应按需取,取完即弃(仅保留小份缓存)。
5. **数据版权属权利人**,见 [`NOTICE`](../NOTICE)。
6. 唯一"非直连"行为是后台补数 —— 目的是让**下次**调用更快,不是提高对上游的请求频率。
7. **新增源要过合规闸**:是否允许程序化访问必须由人给出理由并写进 PR(见
   [`GOVERNANCE.md`](../GOVERNANCE.md) §4)。

---

## 9. 演进路线

| 阶段 | 目标 | 主要内容 |
|---|---|---|
| v1.0 | 可用的元数据网关 | 97 源 / 109 端点 / 500ms 契约 / HTTP + MCP(Python 单体) |
| **v1.1(当前)** | **双平面 + 自助接入** | **Go 网关 / Python 解析面 / NDJSON 契约 / 数据文件化别名 / 智能体接入流水线 / 容器与 CI / 109 源(海外 107 + 国内已接入 2)+ 34 个国内候选** |
| v1.2 | 覆盖面与弹性 | 端点定义从 56 源扩到全量;国内候选池逐批接入;链接状态定期复验;批量查询;游标透传;按端点自适应预算 |
| v1.3 | 规模化 | 跨进程共享缓存;精确分位数;OpenTelemetry;多源实体核查 |
| v2.0 | 生态 | 源包市场(第三方贡献源定义);多语言 SDK;边缘部署评估 |

详见 [`optimization-report.md`](optimization-report.md)。
