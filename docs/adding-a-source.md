# 新增一个数据源

**不需要写代码。** 只需要改数据文件(源清单按国别分两份)—— 但改之前,先按本文的约束把信息填对。

> 用 AI 智能体(MCode / Claude Code)的话,直接看 [`../AGENTS.md`](../AGENTS.md) §2,
> 那里有九步流程与可直接执行的命令。

---

## 1. 前置条件

1. 目标是**法规强制公开**或**官方免费接口**的数据源(能公开访问、不需要绕过任何限制);
2. 有可访问的官方入口(API 或批量下载);
3. 官方公布了限速口径(没有也行,但要在 `官方限速口径` 里写清楚"未公布"或实际观察到的)。

> **不接受的源**:需要登录才能拿到、明确禁止自动化、或需要付费订阅才能读的。
> 这条线不能模糊 —— 见 [`../NOTICE`](../NOTICE) 与仓库的合规立场。

---

## 2. 用脚本做前三步(推荐)

```bash
# ① 侦察目标站点
python3 scripts/recon.py https://api.myjournal.org/works -o /tmp/recon.json

# ② 补齐语义(限速口径/规模口径/增量策略/族归属)—— 这一步只能人做

# ③ 生成草稿(默认写到草稿目录,不动真实数据文件)
python3 scripts/scaffold_source.py --recon /tmp/recon.json \
  --name "My Journal API" --family "七、学术与文献" \
  --org "My Journal" --country "全球" --summary "期刊论文元数据与引用" \
  --out-dir /tmp/draft
```

`recon.py` 会探测可达性、状态码、内容类型、延迟、认证要求、限速响应头,并**启发式推断**
JSON 响应里的列表路径(`item_path`)与总数路径(`total_field`)。
每个推断字段都带 `inference`(推断依据);测不出来的字段是 `null` 并有 `warnings` —— **它不会替你猜**。

`scaffold_source.py` 产出的 `/tmp/draft/REPORT.md` 会列出**仍需你补的 TODO**
(限速口径、规模计数口径、增量策略),这些是语义判断,脚本不编。

补齐后落盘:

```bash
python3 scripts/scaffold_source.py --recon /tmp/recon.json --name "My Journal API" --apply
```

`--apply` 是保护式的:源名已存在就拒绝执行。

---

## 3. 两份清单的分工:海外源落哪份、国内源落哪份

本仓库有**两份**源清单,按 `国别` 列分工,**一个源只属于一份清单**:

| 清单文件 | 装什么 | 列 | 网关运行时 |
|---|---|---|---|
| `data/source_inventory.csv` | **海外**源(`国别` = 美国/欧盟/英国/全球/多国/…) | 17 列 | 全量加载 |
| `data/source_inventory_cn.csv` | **国内**源(`国别` ∈ {中国, 中国大陆, 中华人民共和国}) | 18 列 | 只加载"已接入"的行 |

**判定只看 `国别` 列**:写 `中国` → 落国内清单;其余 → 落海外清单。不要靠源名、域名猜,
也不要因为国内清单还没进运行时就把国内行抄进海外清单 —— 两份都有,字段必然漂移。

国内清单的**前 17 列与海外清单列名、列序完全一致**;第 18 列 `优先级`(`P0`/`P1`/`P2`)
是国内清单**专有**列。

**"已接入" vs "候选"是可机器判定的**:

- 在 `data/endpoints.json` 里有端点定义 → **已接入** → 网关加载它;
- 只在 CSV 里、没有端点定义 → **候选** → 网关**不加载**(候选不会污染网关的源列表)。

**已接入的国内源必须先有 ASCII 别名**。源 id 是对外契约:中文名的源 id 会退化成
`src_<行号>`,**海外清单每加一行,国内源的 id 就整体平移**。所以接入前先在
`data/aliases.json` 里备一个 ASCII 别名(如 `csrc` / `amac`);`make validate` 会检查这条。

### 脚本落盘:落哪份由 `--country` 决定

```bash
# 国内源:落 data/source_inventory_cn.csv,多一列 优先级(默认 P2)
python3 scripts/scaffold_source.py --recon /tmp/recon.json --name "..." \
  --country '中国' --priority P0 --apply
```

- `--country '中国'` → 写 `data/source_inventory_cn.csv`;其余 → 写 `data/source_inventory.csv`;
- `--priority P0|P1|P2` 只对国内清单有意义,默认 `P2`;
- **国内源的 `--apply` 只写国内清单(候选池),不写 `data/endpoints.json`** —— 国内源要先跑通
  `scripts/verify_source.py`,再单独补端点定义与 `data/aliases.json` 别名,才算接入网关。

> 权威表述见 [`../AGENTS.md`](../AGENTS.md) 规则 3.1。

---

## 4. 第一步:在源清单里加一行

按 `国别` 选清单(见 §3),按现有列顺序加一行 —— 海外清单 17 列,国内清单 18 列
(多一列 `优先级`):

| 列 | 填什么 | 例子 |
|---|---|---|
| 分类 | 十族之一(照抄现有写法) | `七、学术与文献` |
| 数据源 | 源名称(**会作为 `endpoints.json` 的键**) | `My Journal API` |
| 机构 | 发布机构 | `My Journal` |
| 国别 | 覆盖范围 | `全球` |
| 数据内容 | 一句话摘要 | `期刊论文元数据与引用` |
| 官方入口 | 可直达的官方 URL | `https://api.myjournal.org/works` |
| 认证方式 | 原文口径,取值见下 | `无key` |
| 官方限速口径 | 官方公布的限速原文 | `10次/秒` |
| 全量下载 | 批量文件/导出方式 | `JSON 导出` |
| 更新频率 | `daily`/`weekly`/`monthly`/`quarterly`/`realtime` | `daily` |
| 实测规模 | 总量 **+ 计数口径** | `实测 1234567 条(接口 total 字段)` |
| 实测增量 | 增量速率 **+ 怎么取** | `日增约 3000 条(按 from-created-date)` |
| 代理需求 | `不走代理` / `兜底` / `直连` | `兜底` |
| 合规风险 | `低`/`中`/`高` | `低` |
| 建议采集方式 | 增量策略 | `按 published_date 增量` |
| 链接状态 | 最近一次校验的状态码 | `200` |
| 探测耗时ms | 最近一次校验耗时 | `812` |

> 国内清单还要多填一列 `优先级`(`P0`/`P1`/`P2`,默认 `P2`),位置在最后一列。

### 认证方式的取值

分类器按**固定规则**解析这一列,取值请照抄现有写法:

| 写法 | 解析结果 |
|---|---|
| `无key` / `无` / `无key(有限)` | `none` |
| `需邮箱` | `email_param` |
| `免费key` / `可注册key` | `api_key` |
| `可选key` / `无key(可选)` | `api_key` + `optional=true` |
| `需token` | `token` |
| `部分付费` / `账号+付费` | `paid` |
| `免费/付费` / `部分免费部分付费` / `多数免费` / `部分注册` | `mixed` |

> ⚠️ **不要写「无key」以外的任意含 "key" 的词** —— 分类器是规则表匹配,
> 写不认识的取值会被 `validate` 拦下。
> 另:`可选`+`key` 必须优先于 `无key` 判定(历史 bug:用 `"key" in text` 判定会把 65 个
> 「无key」的源误判成需要 key)。

### 实测规模与实测增量的硬约束

- `实测增量` **不能**是 `实测规模` 的复制(会被 `validate` 拦下);
- `实测增量` 不能为空;
- `实测规模` 允许写"未实测",但更好的做法是写清**计数口径**,例如
  `以 /api/works 的 total 字段计`。
- **诚实比完整重要** —— 数字可以没有,口径不能含糊。

---

## 5. 第二步:加端点定义(可选但强烈建议)

编辑 `data/endpoints.json`,在 `sources` 下按**源名称**加键:

```jsonc
{
  "sources": {
    "My Journal API": [
      {
        "id": "works",
        "name": "论文检索",
        "method": "GET",
        "url_template": "https://api.myjournal.org/works?filter={filter}&rows={size}",
        "headers": { "Accept": "application/json" },
        "page_size": 25,
        "params": [
          {
            "name": "filter", "in": "query", "type": "string", "required": true,
            "desc": "支持的过滤表达式",
            "example": "from-pub-date:2026-01-01"
          },
          {
            "name": "size", "in": "query", "type": "int", "required": false,
            "default": 25, "example": 10, "desc": "每页条数"
          }
        ],
        "response": {
          "media_type": "application/json",
          "shape": "object",
          "item_path": "data",
          "total_field": "meta.total",
          "fields": [
            { "name": "data", "type": "array", "desc": "论文条目" },
            { "name": "data[].title", "type": "string", "desc": "标题" },
            { "name": "data[].doi", "type": "string", "desc": "DOI" },
            { "name": "meta.total", "type": "int", "desc": "命中总数" }
          ],
          "notes": "实测 1,234,567 条"
        },
        "cache_ttl_s": 900,
        "budget_ms": 400
      }
    ]
  }
}
```

### 关键字段说明

| 字段 | 说明 |
|---|---|
| `url_template` | 带 `{param}` 占位。**必须是完整可调用的 URL** |
| `params[].in` | `path` / `query` / `header` / `body` |
| `params[].required` | 必填参数**必须**给 `example` 或 `default`(否则 `validate` 报错) |
| `headers` | 端点专有请求头(如 `Accept: application/json`) |
| `page_size` | 上游页大小。**单条记录很大时要调小**(否则响应超 1MB 被截断) |
| `response.item_path` | 列表数据在响应里的位置(点号路径) |
| `response.item_shape` | `list`(默认)或 `map`(顶层是 `{id: 记录}` 字典) |
| `response.total_field` | 总数字段路径,支持数组下标(`0.total`) |
| `cache_ttl_s` | 缓存时长。热数据调小 |
| `budget_ms` | 该端点允许的上游墙钟,默认 400 |

**不加端点定义会怎样?** 该源会得到一个通用透传端点 `entry`(直接打官方入口),
能取到数据,但调用方拿不到参数说明与响应结构。`validate` 会提示。

> **国内源例外**:国内清单里没有端点定义的行是**候选**,网关**不加载**它。要让国内源真正
> 接进网关,必须补上端点定义(以及别名)—— 见 §3。

---

## 6. 第三步:加别名

编辑 `data/aliases.json`(不是 Python 代码 —— 别名是元数据):

```json
{
  "version": "1.0",
  "aliases": {
    "My Journal API": ["mja", "myjournal"]
  }
}
```

键必须**精确等于** CSV 里 `数据源` 那一列的值。加了之后,MCP/LLM 调用方用简称也能定位到源。

---

## 7. 第四步:校验

```bash
make validate          # 等价于 python3 -m a_source_gateway.cli validate
```

它会检查:

- **两份清单**的 CSV 列齐全、表头列序符合契约、源名不重复、官方入口是 URL;
- `实测增量` 非空且不等于 `实测规模`;
- `认证方式` 取值能被分类器识别;
- `endpoints.json` 的每个键在**两份清单合计**里能找到对应行;
- 每个端点有 `url_template` 与 `response`;
- 必填参数都有 `example` 或 `default`;
- URL 里用了 `{size}` 就必须声明 `size` 参数;
- **国别归属**:国内清单里不许出现非「中国」国别,海外清单里不许出现「中国」国别;
- **跨文件重名**:同一个源只能出现在一份清单里;
- **优先级取值**:国内清单第 18 列只能是 `P0`/`P1`/`P2`;
- **已接入国内源有 ASCII 别名**:中文名的源 id 会退化成 `src_<行号>`,清单一增删就平移 ——
  接入前先在 `data/aliases.json` 备好 ASCII 别名(如 `csrc` / `amac`)。

---

## 8. 第五步:用真实数据验证(不可跳过)

```bash
python3 scripts/verify_source.py "My Journal API" --direct-only
```

这是**证据关卡**。它真的去请求上游,然后断言:

- 声明的 `response.item_path` 在真实响应里**确实**指向一个列表;
- 声明的 `response.total_field` **确实**是一个数字;
- `response.fields` 里声明的字段名在返回条目里**确实**出现;
- 本次调用**没有降级**(降级的调用不算验证通过)。

退出码:

| 码 | 含义 | 你该做什么 |
|---|---|---|
| `0` | 已用真实数据验证通过 | 进下一步 |
| `1` | 断言失败,或调用降级 | **先看是哪条断言**:`not_degraded` 失败 = 网络慢(网关 400ms 预算),元数据可能完全正确;`item_path`/`total_field` 断言失败 = 元数据写错了,去改 |
| `2` | **连不上上游** | 这是环境问题,不是源的问题 —— 换个网络环境再试,并如实说明 |

> **为什么带 `--direct-only`?** 网关的上游预算只有 400ms,而从中国到欧美上游的实测往返
> 常有 0.4–7.5s。走网关时调用会**正常降级**,`not_degraded` 断言随之失败 ——
> **那是网络慢,不是元数据写错了**。`--direct-only` 让脚本直连上游(自带超时),
> 才能真正验证"我声明的字段对不对"。
>
> 但直连不覆盖网关这一段,所以**两条都跑**:
> ```bash
> python3 scripts/verify_source.py "My Journal API" --direct-only   # 元数据对不对
> python3 scripts/verify_source.py "My Journal API"                  # 走网关能不能通
> ```
> 第二条在网络慢时返回 `DEGRADED` + 退出码 1,这是**预期行为**:
> 它证明的是"网关在预算内优雅降级并给出直连兜底"。

---

## 9. 第六步:部署并交付

```bash
make up                                             # 重建并重启
python3 scripts/demo.py "My Journal API" --limit 2  # 调用示例 + 真实数据
```

`demo.py` 的输出就是你要交给用户的交付物:**端点说明 + 可复制的 curl 命令 + 真实响应**。

---

## 10. 常见问题

**Q:官方入口是网页不是 API,能用吗?**
能,但只能走通用透传端点。请确认该站的 robots.txt 与使用条款允许程序化访问;
不允许就别加。

**Q:源需要 API key 怎么办?**
在 `认证方式` 写 `免费key`,并在 `官方入口` 旁边留个申请链接(可以放在 `全量下载` 或
`建议采集方式` 里)。网关不会替你保管凭据 —— 凭据走环境变量。

**Q:实测规模我数不出来怎么办?**
写 `未实测` 或写计数口径,例如 `以 /api/works 的 total 字段计`。
`validate` 不会因为没数字而报错 —— **诚实比完整重要**。

**Q:我的源响应超过 1MB 怎么办?**
要么调小 `page_size`,要么把它当**文件型源**处理:网关会识别为 `response_too_large`
并返回"请直连上游"的指引,不会假装有数据。

**Q:上游要 UA 带邮箱怎么办?**
设 `A_GATEWAY_CONTACT=you@example.com`。没配时网关会直接返回 `contact_required`
(0ms),不会去打上游吃 403。
