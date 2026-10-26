# meta 规范

网关的"meta"分三层:**源级 meta**、**端点级 meta**、**调用级信封 meta**。
运行时 `GET /v1/schema` 会吐出本文的机器可读版本。

---

## 1. 调用级信封(ResponseEnvelope)

所有调用返回同一个形状。`meta` 描述**这次请求**,`data` 只放结果,两者严格分离。

```jsonc
{
  "meta": { ... },       // 见 §1.1
  "data": [ ... ],       // 列表或对象; 与端点 meta 里声明的结构一致
  "fields": ["DOI", "publisher"]   // 本次实际返回的字段名
}
```

### 1.1 `meta` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `request_id` | string | 本次请求 id(12 位十六进制) |
| `schema_version` | string | 信封版本,当前 `1.2` |
| `source` | string | **规范源 id**(即使用简称请求也会回报规范 id) |
| `endpoint` | string | 端点 id |
| `upstream` | object | `{url, method, status, latency_ms}` —— 实际打到的上游 |
| `latency_ms` | int | **客户端可见总耗时**(含序列化与框架开销) |
| `budget_ms` | int | 本次预算,恒 ≤500 |
| `cached` | bool | 是否命中缓存(含 stale 命中) |
| `cache_age_s` | int | 缓存年龄(秒) |
| `degraded` | bool | 是否降级返回 |
| `offline` | bool | 网关是否处于离线模式 |
| `count` | int | 本次返回条数 |
| `total` | int/null | 上游记录总数(null = 上游未提供) |
| `truncated` | bool | 是否被 `limit` 截断 / 上游页已取满 |
| `next_cursor` | string/null | 下页游标(目前恒 null,见优化报告 §3.8) |
| `rate_limit` | object | `{policy, remaining}`(目前为空) |
| `parser` | string | **实际使用的解析引擎**:`python`(解析面子进程)\|`go`(内置兜底解析器)。见 ADR-018 |
| `trace_id` | string | 本次请求的链路 id(32 位十六进制),用于把网关日志、解析面日志与调用方串起来 |
| `fallback` | object | **降级时的直连兜底**:`{upstream_url, method, hint}` |
| `errors` | array | `[{type, message, status?}]` |

> **v1.1 新增**:`parser` 与 `trace_id` 是**增量字段**,不改变信封的既有形状。
> `parser` 存在的意义是让"换了解析引擎"永远是可观测的 —— 不允许静默换引擎
> (ADR-018)。`trace_id` 在 Python 侧与 Go 侧都会产生,取值规则见下:
> **优先用上游传入的 `X-Request-Id` / `X-Gateway-Trace-Id` 头,没有则生成**。

### 1.2 降级类型(`errors[].type`)

| type | 含义 | 调用方该做什么 |
|---|---|---|
| `timeout` | 上游超过预算 | 看 `fallback.upstream_url` 直连;或稍后重试(后台在补数) |
| `connect_error` | **连不上**:域名解析失败 / 连接被拒 / 证书问题 | 换网络/检查端点 URL;或直连上游 |
| `read_error` | **连上了但中途断了**:连接重置 / 服务端提前关闭 / EOF | 稍后重试(后台在补数) |
| `upstream_error` | 上游返回 4xx/5xx,**或**其余传输层失败 | 看 `errors[].message` 里的上游原文/异常文本 |
| `upstream_not_json` | 上游返回的不是 JSON(常见于上游错误页/WAF 拦截页) | 看 `errors[].message` 里的正文片段;或直连 `fallback.upstream_url` 看真实情况 |
| `parse_error` | 正文不是声明的形状(CSV/JSONL 等) | 同上,直连上游看原文(目前只有 Go 网关会产生这一条;Python 参照面解析不了正文时报 `upstream_not_json`) |
| `response_too_large` | 响应超正文上限被截断 | 减小 `limit`,或直连上游 / 走批量下载 |
| `stale` | 缓存已过期,返回陈旧值 | 视场景接受,或稍后重试 |
| `offline` | 网关离线模式 | 只用元数据与已缓存数据 |
| `contact_required` | 该源要求 UA 带联系邮箱但未配置 | 设 `A_GATEWAY_CONTACT=you@example.com` |
| `budget_exceeded` | 实际耗时超预算 | 上报异常,通常伴随上游变慢 |
| `not_found` | 未知源 / 未知端点 | 用 `list_sources` 重新定位 |
| `missing_param` | 缺必填参数 | 看端点 meta 的 `params` |
| `endpoint_not_found` | 该源没有这个端点 | 用 `describe_endpoint` 列端点 |

> **传输层失败用三个共用名字**:`connect_error`(根本没连上)/ `read_error`(连上了但中途断了)/
> `upstream_error`(其余传输层失败)。调用方是按 `errors[].type` 分支的,所以同一个失败类别
> 在两个平面必须给同一个名字 —— Go 网关的 `upstreamErrorType()`
> (`gateway/internal/server/resolver.go`)与 Python 参照面的 `_transport_error_type()`
> (`python/src/a_source_gateway/resolver.py`)逐条对齐,`python/tests/test_error_taxonomy.py`
> 守住这张词表不再外扩。
> **原始异常文本照旧放在 `errors[].message` 里** —— 各运行时的措辞不同,那是事实,不强行统一。
> `timeout` 仍是 `timeout`。

### 1.3 搜索响应(`POST /v1/search`)

`POST /v1/search` 是**唯一不返回单个信封**的路由:它一次扇出多个源,返回
**信封数组 + 汇总 meta**。

```jsonc
{
  "meta": { ... },                    // 见 §1.3.1 —— 整次扇出的汇总
  "queries": ["crossref/works", "gleif_lei/lei_records"],
  "results": [                        // 每项各自是一个源的信封
    {
      "source": "crossref",
      "endpoint": "works",
      "degraded": false,
      "cached": false,
      "latency_ms": 1,
      "count": 2,
      "total": null,
      "data": [ ... ],                 // 这一条的结果
      "fields": [ ... ],               // 这一条返回的字段名
      "errors": []                     // 这一条的错误;无错时是 []
      // "fallback": { ... }           // 降级且有直连指引时才出现
    }
  ]
}
```

#### 1.3.1 汇总 `meta`

| 字段 | 类型 | 说明 |
|---|---|---|
| `requested` | int | 这次实际发起的查询条数,恒等于 `ok + degraded + failed`(三类互斥) |
| `ok` | int | 既没降级、也没有 error 的条数 |
| `degraded` | int | 降级条数(超时、上游报错、解析失败等) |
| `failed` | int | 没降级但有 error 的条数 —— 未知源 / 未知端点这类硬失败 |
| `with_data` | int | `count > 0` 的条数 |
| `budget_ms` | int | 本次总预算(默认 500),**每个源各自一份** |
| `latency_ms` | int | 整次扇出的墙钟耗时(约等于最慢的那一条) |
| `trace_id` | string | 本次请求的链路 id |
| `schema_version` | string | 信封结构版本,当前 `1.2` |

#### 1.3.2 `results[]` 每一项

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` / `endpoint` | string | 这一条实际命中的规范源 id 与端点 id |
| `degraded` | bool | 这一条是否降级 |
| `cached` | bool | 这一条是否命中缓存 |
| `latency_ms` | int | 这一条自己的耗时 |
| `count` | int | 这一条返回的条数 |
| `total` | int/null | 这一条的上游记录总数 |
| `data` | array/object | 这一条的结果 |
| `fields` | string[] | 这一条返回的字段名 |
| `errors` | array | 这一条的错误列表,**始终存在**;无错时是 `[]`,每项形如 `{type, message}` |
| `fallback` | object | 降级时的直连兜底;**只在降级且有直连指引时才出现** |

`queries[i]` 与 `results[i]` **一一对应且顺序一致**,`queries[i]` 的格式是
`"<source>/<endpoint>"`(例如 `"crossref/works"`)。

#### 1.3.3 与 §1 三段式的对应关系

| 三段式 | 搜索响应里的对应 |
|---|---|
| `meta` | 顶层 `meta`(汇总 `requested` / `ok` / `degraded` / `failed` / `with_data`) |
| `data` | `results[i].data` |
| `fields` | `results[i].fields` |

**单个源失败不影响整次调用**:未知源 / 未知端点 / 上游超时都只写进那一条的 `errors`,
整体仍返回 200。这与 `POST /v1/query` 遇未知源返回 404 **刻意不同** —— 搜索要的是
"一次看多个源",一个源坏了不该让整次调用失败。各源**并发**执行、各自一份 ≤500ms 预算,
整体墙钟约等于最慢的那一条。

---

## 2. 源级 meta(SourceSpec)

```jsonc
{
  "id": "crossref",                 // 规范 id
  "name": "Crossref",
  "aliases": ["crossref", "doi"],   // 常用简称(1.2 起);调用方可以直接用它代替 id
  "family": "七、学术与文献",
  "org": "Crossref",
  "country": "全球",
  "summary": "DOI 元数据/引用/资助信息/许可协议",
  "homepage": "https://api.crossref.org/works",
  "link_status": "200",             // 最近一次链接校验的状态码
  "link_checked_at": "2026-10-22",
  "auth": {
    "type": "none",                 // none|api_key|oauth2|token|email_param|paid|mixed
    "optional": false,              // 凭据是否可选
    "note": "无key"                  // 原文口径
  },
  "limits": {
    "rate_per_sec": null,           // 部分源未在清单里量化,故为 null
    "burst": null,
    "daily_credits": null,
    "note": "50次/秒(礼貌池)"
  },
  "scale": {
    "total": 187910966,
    "unit": "records",
    "measured_at": "2026-10-22",
    "basis": "实测187910966条DOI"     // 计数口径(怎么数出来的)
  },
  "increment": {
    "frequency": "daily",
    "daily": "30天新增 1231180 条(≈4.1万/日);单日新增 73625 但索引变更 753681(约 10 倍)",
    "window": "2026-10-01 → 2026-10-22",
    "strategy": "新增用from-created-date;变更用from-index-date"
  },
  "crawl": {
    "mode": "query",
    "batch_files": [],
    "notes": "全量 JSON 导出"
  },
  "proxy_policy": "fallback",       // never|fallback —— 是否允许走代理兜底
  "risk": "low",                    // low|medium|high 合规风险
  "tags": ["七、", "全球"],
  "aliases": ["crossref", "doi"],   // 常见简称
  "ua_requires_contact": false,     // 该源是否要求 UA 带联系邮箱
  "endpoints": [ ... ]               // 见 §3
}
```

### 2.1 关键词检索(`GET /v1/sources?q=`)

`q` 对每个源的**可搜索文本**做**子串**匹配(大小写不敏感),可搜索文本 =
规范 id + 全部别名 + 名称 + 机构 + 国别 + 分类 + 数据内容摘要 + 该源全部端点 id。

命中任意一处即返回该源 —— 所以 `q=sec_edgar`(规范 id)、`q=szse`(别名)、
`q=stock_list`(端点 id)、`q=制裁`(数据内容)都能找到源。

**返回项里的 `aliases` 告诉你该用哪个简称去调用** —— "搜到 → 知道怎么调"闭环:

```bash
curl -s "http://127.0.0.1:8848/v1/sources?q=szse"
# → {"meta":{"count":1,…},"data":[{"id":"szse","name":"深圳证券交易所 A股列表",
#                                  "aliases":["szse","szse_stock"],…}]}
```

> 1.2 起的行为变更:此前 `q` **只**匹配名称/摘要/机构/国别/分类 ——
> 于是出现"能按别名调用、却搜不到"(别名能解析到源,但搜不出来)。

---

## 3. 端点级 meta(EndpointSpec)

这是"接口怎么调、返回什么"的核心。**调用方不需要猜任何字段。**

```jsonc
{
  "id": "works",
  "name": "DOI 检索",
  "method": "GET",
  "url_template": "https://api.crossref.org/works?filter={filter}&rows={size}",
  "headers": { "Accept": "application/json" },   // 端点专有请求头
  "page_size": 10,                   // 上游页大小(字段肥的源调小)
  "params": [
    {
      "name": "filter", "in": "query", "type": "string",
      "required": true, "desc": "created=新增, index=变更(量约 10 倍)",
      "pattern": null, "default": null,
      "example": "from-index-date:2026-10-21",
      "env": null                      // 凭据从哪个环境变量读(见下)
    }
  ],
  "response": {
    "media_type": "application/json",
    "shape": "object",                // object|array|csv|xml|tsv|text
    "fields": [
      {"name": "message.items", "type": "array", "desc": "文献条目"},
      {"name": "message.total-results", "type": "int", "desc": "命中总数"}
    ],
    "item_path": "message.items",     // 列表数据在响应里的位置
    "item_shape": "list",             // list|map —— map 表示顶层是 {id: 记录} 字典
    "total_field": "message.total-results",
    "notes": "实测 187,910,966 条"
  },
  "cache_ttl_s": 900,
  "budget_ms": 400,                   // 该端点允许的上游墙钟
  "notes": ""
}
```

### 3.1 `params[].in`

`path` / `query` / `header` / `body`。`path` 表示占位在 URL 路径里。

### 3.1.1 参数取值顺序

调用方没传的参数,网关按这个顺序补:

1. **`params[].env` 指向的环境变量** —— 凭据不进数据文件,数据里只写变量名;
2. **`params[].default`** —— 端点声明的默认值;
3. **`params[].example`** —— 只对 `required=true` 的参数兜底。

调用方显式传的值永远优先。必填参数如果三者都没有,才报 `missing_param`。

`example` 对必填参数的作用是**"这个端点怎么调"的答案**:所以
`{"source":"sec_edgar","endpoint":"submissions","params":{}}` 这样不带参数的调用
也能取到数据(用端点声明的示例值),而不是报缺参数。

### 3.1.2 请求头里的 `${VAR}`

`headers` 的值里可以写 `${VAR}`,网关在发请求前用环境变量替换:

```jsonc
"headers": { "Authorization": "Token ${A_GATEWAY_COURTLISTENER_TOKEN}" }
```

引用了**未设置**的变量时,整个请求头都不发 —— 发一个残缺的凭据
(只剩 `Bearer `)比不发更糟。

### 3.2 `response.item_path` 与 `item_shape`

- `item_shape=list`(默认):列表在 `item_path` 指向的位置;
- `item_shape=map`:顶层就是 `{id: 记录}` 字典,网关取**值域**作为列表
  (例:SEC `company_tickers` 是 `{"0": {...}, "1": {...}}`,实测 10,425 条)。

### 3.2.1 `response.item_flatten`(1.1 新增,默认 false)

上游的列表**套着列表**时,打开它会把内层列表**展平一层**。
默认关闭 —— 隐式展平会悄悄改变既有端点的输出形状。

上交所公告就是这种形状:`pageHelp.data` 是 `[[{...}], [{...}]]`(每条公告各装一个数组),
声明 `item_flatten: true` 后,网关返回的是扁平的记录列表。

### 3.2.2 `item_path` 支持数字下标(1.1 起)

`item_path` 与 `total_field` 用**同一套**点号路径语义,段与段之间可以夹数字下标:

- `total_field` 一直支持(`0.total`);
- `item_path` 从 1.1 起也支持(`0.data` —— 深交所 A 股列表的形状:
  顶层是数组 `[{metadata:…, data:[…]}]`)。

**顶层是数组时**:`item_path` 首段是数字才按下标定位;首段不是数字则**保持整个数组**
(与 1.0 行为一致,不会变成空列表)。

### 3.3 `response.total_field`

点号路径,指向**总数**所在字段(通常是整数)。网关把它放进 `meta.total`。
数组下标也支持(如 `0.total`)。

键名本身带点的情况(OData 的 `@odata.count`)也支持:点号路径取不到时,
网关会再按**整串字面键**找一次(嵌套路径优先,不会改变已有语义)。

### 3.4 凭据相关的环境变量

数据文件里只写变量名,值由部署时提供。已登记的变量:

| 环境变量 | 用途 |
|---|---|
| `A_GATEWAY_CONTACT` | 联系邮箱(拼进 UA;OpenAlex/SEC/Unpaywall 等要求) |
| `A_GATEWAY_CONGRESS_API_KEY` | Congress.gov 的 api_key |
| `A_GATEWAY_CENSUS_API_KEY` | Census 的 key |
| `A_GATEWAY_EIA_API_KEY` | EIA 的 api_key |
| `A_GATEWAY_REGULATIONS_API_KEY` | Regulations.gov 的 api_key |
| `A_GATEWAY_COLLEGE_SCORECARD_KEY` | College Scorecard 的 api_key |
| `A_GATEWAY_EDINET_SUBSCRIPTION_KEY` | EDINET 的 Subscription-Key |
| `A_GATEWAY_DART_API_KEY` | DART 的 crtfc_key |
| `A_GATEWAY_WORLDBANK_KEY` | World Bank 的 APIM 订阅密钥(请求头) |
| `A_GATEWAY_COMPANIES_HOUSE_AUTH` | Companies House 的 Basic 认证头(整串 `Basic ...`) |
| `A_GATEWAY_COURTLISTENER_TOKEN` | CourtListener 的 Token(请求头用 `Token ${...}`) |

未设置的变量不会让请求失败,端点会退回到声明的默认值(或在缺失时报缺参数)。

---

## 4. 请求

```jsonc
{
  "source": "crossref",              // 也接受简称: crossref / Crossref / doi
  "endpoint": "works",
  "params": { "filter": "from-index-date:2026-10-21" },
  "fields": ["DOI", "publisher"],    // 只回这些字段(投影)
  "limit": 20,                       // 1..500
  "since": "2026-10-01",             // 增量窗口(由端点语义决定)
  "timeout_ms": 500                  // 50..500,上限 500
}
```

**保留参数 `_cb`**:只进缓存键、不发给上游,用于强制冷路径测试(基准用)。

### 4.1 搜索请求(`POST /v1/search`)

请求体有**两种形态**;`queries` 非空时**优先**用它,两个都给也以 `queries` 为准。

**形态 A —— 显式扇出**:

```jsonc
{
  "queries": [
    {
      "source": "crossref",                 // 必填;也接受简称
      "endpoint": "works",                  // 缺省 "entry"(通用透传端点)
      "params": { "filter": "from-index-date:2026-10-21" },  // 键顺序原样保留
      "limit": 2,                           // 缺省取顶层 limit
      "fields": [ "DOI" ]                   // 只回这些字段
    }
  ],
  "limit": 5
}
```

**形态 B —— 关键词扇出**:

```jsonc
{
  "q": "制裁",          // 用与 GET /v1/sources?q= 相同的搜索索引选源
  "limit": 5,           // 默认 5,取值 1..100
  "max_sources": 12     // 默认 12,取值 1..50;只对 q 形态生效
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `queries` | array | 否 | — | 显式扇出的查询列表;非空时逐项各跑一次查询 |
| `q` | string | 否 | — | 关键词扇出(仅在 `queries` 缺省或为空时生效) |
| `limit` | int | 否 | `5` | 每条查询最多返回多少条,取值 1..100;**越界被钳到范围内**(不报错) |
| `max_sources` | int | 否 | `12` | 关键词扇出时最多选多少个源,取值 1..50;越界同样被钳位 |

`queries[]` 里每一项的字段:

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `source` | string | 是 | — | 源 id(也接受别名) |
| `endpoint` | string | 否 | `entry` | 端点 id;缺省用通用透传端点 `entry` |
| `params` | object | 否 | `{}` | 端点请求参数;**键顺序原样保留**(渲染出的 URL 与缓存键由它决定) |
| `limit` | int | 否 | 顶层 `limit` | 这一条最多返回多少条 |
| `fields` | string[] | 否 | `[]` | 只回这些字段 |

> `q` 的选源规则与 §2.1 的关键词检索**同一套索引**:大小写不敏感地匹配源的规范 id、
> 全部别名、名称、机构、国别、分类、摘要与该源全部端点 id。命中后每个源各跑它自己的
> 第一个**非通用**端点(跳过自动派生的 `entry`)。

**校验失败(422)**:响应体与 FastAPI 同形
`{"detail":[{"type":…,"loc":[…],"msg":…,"input":…}]}`;嵌套字段的 `loc` 把整条路径放在
**一个段**里(如 `["body","queries.0.source"]`)。主要几类:

| type | 什么时候 | msg |
|---|---|---|
| `json_invalid` | 请求体不是合法 JSON | `Invalid JSON body` |
| `model_attributes_type` | 请求体不是对象 | `Input should be a valid dictionary or object to extract fields from` |
| `list_type` | `queries` 不是数组 / `fields` 不是数组 | `Input should be a valid list` |
| `dict_type` | `queries[i]` 或 `params` 不是对象 | `Input should be a valid dictionary` |
| `missing` | 缺 `source`;或 `queries` 为空且 `q` 也缺(`q` 是空串同样算缺,`loc` 指向 `q` 自己) | `Field required` / `Field required: provide "queries" or "q"` |
| `string_type` | `source` / `endpoint` / `q` / `fields[i]` 不是字符串 | `Input should be a valid string` |
| `int_type` | `limit` / `max_sources` / `queries[i].limit` 不是整数 | `Input should be a valid integer` |

> 注意与 `POST /v1/query` 的差别:查询接口的 `limit` 越界报 422,搜索接口的 `limit` /
> `max_sources` 是**钳位**(不报错)。

---

## 5. 源清单 CSV 的列契约

源级 meta 的事实源是**两份清单**,按 `国别` 列分工 —— 一个源只属于一份清单:

| 清单 | 装什么 | 列 | 网关运行时 |
|---|---|---|---|
| `data/source_inventory.csv` | 海外源(`国别` 不是国内白名单里的值) | 18 列 | 全量加载 |
| `data/source_inventory_cn.csv` | 国内源(`国别` ∈ {中国, 中国大陆, 中华人民共和国}) | 19 列 | 只加载"已接入"的行 |

两份清单的**前 18 列列名与列序完全一致**;第 19 列 `优先级`(`P0`/`P1`/`P2`)是**国内清单专有**,
海外清单没有这一列。

**"已接入"是可机器判定的**:一个国内源在 `data/endpoints.json` 里有端点定义 → **已接入** →
网关加载它;只在 CSV 里、没有端点定义 → **候选** → 网关**不加载**(候选不污染网关的源列表)。
因此 `meta` 里出现的国内源,一定同时能在 `data/endpoints.json` 找到对应的端点定义。

海外清单的 18 列契约如下 —— 列名即契约:

| 列 | 说明 |
|---|---|
| `分类` | 十族之一 |
| `数据源` | 源名称(与 `endpoints.json` 的键对应) |
| `机构` | 发布机构 |
| `国别` | 覆盖范围 |
| `数据内容` | 内容摘要 |
| `官方入口` | 官方 URL(必须可直达) |
| `认证方式` | 原文口径(`无key` / `免费key` / `需token` / `部分付费` …) |
| `官方限速口径` | 官方公布的限速原文 |
| `全量下载` | 批量文件/导出方式 |
| `更新频率` | daily / weekly / monthly / quarterly / realtime |
| `实测规模` | 总量与计数口径 |
| `实测增量` | 增量速率与取法 |
| `代理需求` | `不走代理` / `兜底` / `直连` |
| `合规风险` | low / medium / high |
| `建议采集方式` | 增量策略 |
| `链接状态` | 最近一次校验的状态码 |
| `探测耗时ms` | 最近一次校验耗时 |
| `最后验证` | 这条元数据最近一次被真实请求核对过的日期(ISO);空 = 未核过 |

> `认证方式` 与 `实测增量` 有两条硬约束(由 `a-source-gateway validate` 检查):
> ① `实测增量` **不能**是 `实测规模` 的复制;② `实测增量` 不能为空。

---

## 6. 版本策略

- `schema_version` 语义化。**加字段 = 小版本**;**改字段语义或删字段 = 大版本**。
- 信封里未知字段应当被忽略(向前兼容)。
- v1.x 期间 `data` 的结构不做破坏性变更;端点 meta 的 `fields` 只增不删。
