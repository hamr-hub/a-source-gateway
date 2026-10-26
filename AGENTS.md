# AGENTS.md — a-source-gateway 智能体操作手册

> **这份文件是给 AI 智能体看的。** MCode、Claude Code、Codex 或任何能读写本仓库的 Agent,
> 开始工作前请完整读一遍。
> 人类贡献者请看 [`CONTRIBUTING.md`](CONTRIBUTING.md);Claude Code 用户看 [`CLAUDE.md`](CLAUDE.md)。

---

## 0. 30 秒了解本项目

**a-source-gateway** 是一个**元数据网关**:它不存储业务数据,只保存"法规强制公开(A 类)"
数据源的**元数据索引**(海外清单 + 国内清单,见[规则 3.1](#规则-31--两份清单的分工海外用哪份国内用哪份)),
并按需实时向上游官方 API 取数,封装成统一信封对外提供 HTTP API 与 MCP 工具。

国内 A 类源另有一份**国内清单** `data/source_inventory_cn.csv`(34 个源 = 10 个已接入
+ 24 个候选;按国别分工)。

对调用方的硬承诺:**任何一次调用都在 500ms 内返回**;做不到时不超时挂死,而是返回
`meta.degraded=true` + `meta.fallback.upstream_url`,让调用方可以立刻直连上游。

```
调用方 ──HTTP/MCP──▶ Go 网关(gateway/)         路由 / 注册表 / 缓存 / 限速 / 总墙钟预算 / 指标
                            │
                            ├──▶ 上游官方 API(按站限速,声明 UA)
                            │
                            └──▶ Python 解析面(parser_worker)  解析原始响应 → 规范化条目
```

**一句话分工:Go 做网关(传输与调度),Python 做解析(内容与结构)。**

---

## 1. 三条不可违反的硬规则

违反任何一条,这个项目就失去了它存在的意义。请在任何情况下都遵守。

### 规则 1 —— 合法性优先于功能

只接入**官方公开、允许程序化访问**的数据源。

- ✅ 接受:法规强制公开的数据、官方免费 API、官方批量下载文件。
- ❌ 拒绝:需要登录/付费才能读的数据、明确禁止自动化抓取的站点、需要绕过任何限制才能拿到的数据。
- ❌ 拒绝:内置代理池或 IP 轮换来绕过官方限速。**限速是授权条件,不是技术障碍。**
  项目立场详见 [`NOTICE`](NOTICE) 与 [`docs/architecture.md`](docs/architecture.md) §8。

遇到"技术上能爬但条款不允许"的站点,**停下来并向用户说明**,不要自行接入。

### 规则 2 —— 不许编造

任何写进 `data/` 的字段,必须来自下面三者之一,并且能追溯:

1. **上游文档**里明确写的(限速口径、认证方式、计数字段);
2. **真实 HTTP 请求**的实测结果(状态码、延迟、响应结构、规模数字);
3. **用户/维护者明确提供**的信息。

拿不准就留空或写"未实测",并把计数口径写清楚。**宁可诚实,不要完整。**
`实测规模` 允许没有数字,但要写明"用哪个端点的哪个字段计"。

### 规则 3 —— 加源不改代码

新增一个数据源**只允许改数据文件**:

| 要加什么 | 改哪个文件 |
|---|---|
| 源基本信息(**海外/全球/多国**) | `data/source_inventory.csv` 加一行 |
| 源基本信息(**中国国内**) | `data/source_inventory_cn.csv` 加一行 |
| 端点定义 | `data/endpoints.json` 的 `sources` 下加键 |
| 常用简称/别名 | `data/aliases.json` 加一行 |

**不要**为了加源去改 `src/` 里的任何 Python 文件,也不要往 Go 代码里硬编码源、端点或别名。
(历史遗留:`src/registry.py` 里曾有硬编码的 `ALIASES` 字典,已迁到 `data/aliases.json`。)

### 规则 3.1 —— 两份清单的分工:海外用哪份、国内用哪份

本仓库有**两份**源清单,按 `国别` 列分工,**一行只属于一份清单**:

| 清单文件 | 装什么 | 列 | 网关运行时是否加载 |
|---|---|---|---|
| `data/source_inventory.csv` | **海外**源(`国别` = 美国/欧盟/英国/全球/多国/…) | 18 列 | ✅ 全量加载 |
| `data/source_inventory_cn.csv` | **国内**源(`国别` ∈ {中国, 中国大陆, 中华人民共和国}) | 19 列 | ⚠️ 只加载"已接入"的行 |

**判定方法:只看 `国别` 列。**

- `国别` ∈ {`中国`, `中国大陆`, `中华人民共和国`} → 落 `data/source_inventory_cn.csv`
- 其余一律(全球/多国/欧盟/美国/**中国香港**…) → 落 `data/source_inventory.csv`

判定用的是 `a_source_gateway.registry.CN_COUNTRIES` / `is_cn_country()` —— 脚本与校验器
都从这一处取,不在别处另抄一份。「中国香港」是单独法域,留在海外清单,所以这里是白名单而不是前缀匹配。

不要靠源名、域名或"这是个新源"来猜归属;也不要因为国内清单还没进运行时,就把国内行
顺手抄进海外清单。

**国内清单的列契约**:前 18 列与 `data/source_inventory.csv` **列名与列序完全一致**;
第 19 列 `优先级`(`P0`/`P1`/`P2`)是国内清单**专有**列,P0 = 最高优先接入/维护。

**端点定义里两个与国内源相关的字段**(1.1 起):

- `response.item_path` 支持**数字下标**(与 `total_field` 同一套语义):深交所的响应是
  顶层数组 `[{metadata:…, data:[…]}]`,所以写 `item_path: "0.data"`、
  `total_field: "0.metadata.recordcount"`。
- `response.item_flatten: true`(默认 false):列表**套列表**时展平一层 ——
  上交所公告的 `pageHelp.data` 是 `[[{…}], [{…}]]`,不展平的话调用方拿到的是数组套数组。

**"已接入" vs "候选" 是机器可判定的,不要靠感觉**:

- 一个国内源**在 `data/endpoints.json` 里有端点定义** → **已接入** → 运行时加载它;
- **只在 CSV 里、没有端点定义** → **候选** → 运行时**不加载**(候选源不会污染网关的源列表)。

所以国内清单里可以放很多"还没接入"的高优先级候选,不用急着给它们编端点定义 ——
没跑通 `verify_source.py` 的源不许进 `endpoints.json`。

**硬约束(防止两份清单长出副本)**:

1. **不复制**。同一个源只能出现在一份清单里。国内源不要把行再抄进 `source_inventory.csv` ——
   两份都有,改字段时必然漂移(哪份是真的说不清)。
2. **国内源接网关时,海外清单不动**。正确做法是:国内行在 `source_inventory_cn.csv` **原地保留**,
   在 `data/endpoints.json` 加端点定义,在 `data/aliases.json` 加别名 —— 运行时**自动**加载它,
   不需要改代码。
3. **改字段只改一份**。改国内源的机构名/限速口径,只改 `source_inventory_cn.csv` 那一行。
4. **已接入的国内源必须有 ASCII 别名**。源 id 是对外契约:名字是中文时 id 会退化成
   `src_<行号>`,**清单一增删就平移**。所以接入前先在 `data/aliases.json` 里备一个
   ASCII 别名(如 `csrc` / `amac`),`make validate` 会检查这条。
5. **`data/` 下的新文件必须进所有分发路径**,否则线上会静默少源。四处都要加:
   `pyproject.toml` 的 `force-include`、`.github/workflows/release.yml` 的 tarball 拷贝、
   `deploy/README.md` 的 systemd 安装命令、`python/Makefile` 的 `sync-data`。
   漏装的症状:线上注册表比仓库少源, 且随包数据连自己的 `make validate` 都过不了。
   (运行时发现国内清单缺失会**告警**, 但那已经是在生产上了。)

---

## 2. 核心任务:「把 xxx 网站加进来」

这是本项目最主要的使用场景。**用户会对 MCode 或 Claude Code 说:"把 xxx 网站加进来"。**
你必须能独立完成下面整套流程,并且**最后给出可复制的调用示例和真实返回的数据**。

### 步骤 0 —— 先确认合规(30 秒)

在动手之前先回答这三个问题,任何一个答不上来就**先问用户**:

1. 这个数据是不是**法规强制公开**或**官方免费开放**的?
2. 它的官方条款/robots 是否允许程序化访问?
3. 它是否公布了限速口径?(没有也行,但要写清"未公布"或"实测观察到")。

### 步骤 1 —— 侦察目标站点

```bash
python3 scripts/recon.py https://api.example.org/works -o /tmp/recon.json
```

`recon.py` 会探测:可达性、状态码、内容类型、延迟、认证要求(401/403/`WWW-Authenticate`)、
限速响应头(`X-RateLimit-*`/`Retry-After`)、常见的 API 文档端点(`/openapi.json`、`/swagger.json`、
`/api-docs` 等,最多约 20 次探测)、以及 JSON 响应里**列表所在路径**和**总数所在路径**的启发式推断。

常用参数:`--contact you@example.com`(很多官方 API 要求 UA 带邮箱)、`--timeout 8`、`--no-probe`(跳过路径发现)。

输出 `recon.json` 里每个推断字段都带 `inference`(推断依据);无法确定的字段是 `null` 并且有 `warnings`。

> **注意**:多数官方 API 要求 UA 里带联系邮箱。若 `recon.py` 报 403 且站点文档要求邮箱,
> 设置 `A_GATEWAY_CONTACT=you@example.com` 后重试。

### 步骤 2 —— 补齐语义(这一步需要你的判断,不能自动化)

侦察能测出"接口长什么样",但测不出"这数据是干什么的、更新多快、限速怎么算"。这些要靠你读
上游文档 + 你的领域判断补齐:

| 字段 | 从哪来 | 典型写法 |
|---|---|---|
| 分类 | 你的判断,取自现有十族 | `七、学术与文献` |
| 机构 / 国别 | 上游官方站点 | `美国SEC` / `美国` |
| 数据内容 | 上游文档 | `申报文件全文/XBRL财报/Form4内部人` |
| 认证方式 | **必须用分类器认识的写法**(见下表) | `无key` |
| 官方限速口径 | 上游文档原文 | `官方约10次/秒` |
| 实测规模 | 真实请求拿到的数字 + 计数口径 | `实测 1234567 条(接口 total 字段)` |
| 实测增量 | 真实观测 | `约 3300 份/日(按 daily-index 增量)` |
| 代理需求 / 合规风险 | 你的判断 | `不走代理` / `低` |
| 建议采集方式 | 你的判断 | `按 published_date 增量` |

**认证方式取值(分类器是规则表匹配,写了不认识的值会被 `validate` 拦下):**

`无key` `需邮箱` `免费key` `可注册key` `可选key` `需token` `部分付费` `账号+付费`
`免费/付费` `部分免费部分付费` `多数免费` `部分注册`

> ⚠️ 不要写"无key"以外的任意含 `key` 的词。历史 bug:用 `"key" in text` 判定会把 65 个"无key"的源
> 误判成需要 key。现在是规则表,且 `可选`+`key` 必须优先于 `无key` 判定。

### 步骤 3 —— 生成草稿

```bash
python3 scripts/scaffold_source.py \
  --recon /tmp/recon.json \
  --name "Example Journal API" \
  --family "七、学术与文献" \
  --org "Example Journal" \
  --country "全球" \
  --summary "期刊论文元数据与引用" \
  --out-dir /tmp/draft
```

默认写到草稿目录,**不动真实数据文件**。产物:

- `/tmp/draft/source_inventory.row.csv` —— 可直接粘进对应清单的一行
- `/tmp/draft/endpoints.entry.json` —— 可直接粘进 `data/endpoints.json` 的端点定义
- `/tmp/draft/REPORT.md` —— 每个字段的来源,以及**仍需要你补的 TODO 项**(限速口径、规模口径、增量策略)

**落哪份清单由 `--country` 决定**:`--country '中国'` → `data/source_inventory_cn.csv`
(且国内行多一列 `优先级`,用 `--priority P0|P1|P2` 指定);其余 → `data/source_inventory.csv`。

看完 `REPORT.md` 的 TODO,补齐后再落盘:

```bash
python3 scripts/scaffold_source.py --recon /tmp/recon.json --name "..." --apply
```

`--apply` 是幂等保护式:源名已存在就拒绝执行。
**国内源的 `--apply` 只写国内清单(候选池),不写 `endpoints.json`** —— 国内源要先跑通
`verify_source.py`,再单独补端点定义与别名才算接入网关(见规则 3.1)。

### 步骤 4 —— 加别名(让 LLM/简称也能找到它)

编辑 `data/aliases.json`,在 `aliases` 对象里加一项:

```json
{
  "version": "1.0",
  "aliases": {
    "My Journal API": ["mja", "myjournal"]
  }
}
```

⚠️ **必须嵌在 `aliases` 对象里**。两个加载器(Go 与 Python)只读 `aliases` 这个键;
写成顶层键会被**静默忽略** —— `validate` 照样通过,但别名永远不生效。
键必须**精确等于** CSV 里 `数据源` 那一列的值。

### 步骤 5 —— 校验

```bash
make validate          # 等价于 python3 -m a_source_gateway.cli validate
```

它会检查**两份清单**(`source_inventory.csv` + `source_inventory_cn.csv`):列齐全、源名不重复、
官方入口是 URL、`实测增量` 非空且不等于 `实测规模`、`认证方式` 取值能被分类器识别、
没有残留的 `TODO(...)` 占位符、`endpoints.json` 的每个键在两份清单合计里能找到对应行、
每个端点有 `url_template` 与 `response`、必填参数都有 `example` 或 `default`、
URL 里用了 `{size}` 就必须声明 `size` 参数;另外还查**国别归属**、**跨文件重名**、
**优先级取值**(只能是 P0/P1/P2)与**已接入国内源的 ASCII 别名**(保证源 id 不随清单顺序漂移)。

### 步骤 6 —— 用真实数据验证(这一步不能跳过)

```bash
python3 scripts/verify_source.py "My Journal API" --direct-only
```

这是**证据关卡**。它会真的去请求上游,然后断言:

- 声明的 `response.item_path` 在真实响应里**确实**指向一个列表;
- 声明的 `response.total_field` **确实**是一个数字;
- `response.fields` 里声明的字段名在返回条目里**确实**出现;
- 本次调用**没有降级**(降级的调用不算"验证通过")。

退出码:`0` 已用真实数据验证通过 / `1` 断言失败或调用降级 / `2` **根本连不上上游**
(这是环境问题,不是源的问题 —— 如实报告,不要伪造) / `3` **端点定义是对的,只是本机没配凭据**
(必填参数全挂在环境变量上而本机没设 —— 配好凭据再跑,不要去改元数据)。

同时它会打印一段**真实的**响应样本和一条可复制的 `curl` 命令。

> **为什么默认加 `--direct-only`?** 走网关时,网关的上游预算只有 400ms,
> 而从中国到欧美上游的实测往返常常是 0.4–7.5s —— 于是调用会**正常地降级**,
> `not_degraded` 断言随之失败、退出码变成 `1`。**那不是元数据写错了,是网络慢。**
> `--direct-only` 让验证脚本直连上游(带自己的超时),不受网关 400ms 预算影响,
> 才能真正回答"我声明的字段对不对"。
>
> 代价:直连验证**不覆盖网关这一段**。所以两条都要跑:
> ```bash
> python3 scripts/verify_source.py "My Journal API" --direct-only   # 元数据对不对
> python3 scripts/verify_source.py "My Journal API"                  # 走网关能不能通
> ```
> 第二条在网络慢时返回 `DEGRADED` + 退出码 1,这是**预期行为**,不是源有问题;
> 它证明的是"网关在预算内优雅降级并给出直连兜底"。
>
> 如果退出码是 `1`,**先看断言名**:
> - `not_degraded` 失败 → 网络/预算问题,元数据可能完全正确,别去改元数据;
> - `upstream_not_error` 失败且详情提到"配额/限流" → 上游配额耗尽(如 BLS 的
>   `REQUEST_NOT_PROCESSED`),结构断言会被跳过 —— 元数据没错,换时段或配好 key 再试;
> - `item_path_resolves_to_list` / `total_field_resolves_to_number` 失败 → 元数据写错了,去改。

#### 批量复验与 `最后验证` 列

`最后验证` 是这条元数据最近一次被**真实请求**核对过的日期。留空比填一个自己没跑过的日期
诚实 —— 空着运行时会读到 `unverified` 哨兵,而不是某个历史日期。

```bash
# 复验全部源, 汇总写成 JSON(默认直连上游, 逐源子进程, 单源失败不拖垮整批)
python3 scripts/verify_source.py --all --json > /tmp/recap.json

# 把这次跑通过的日期写回两份清单的『最后验证』列
python3 scripts/verify_source.py --write-verified-date --recap-json /tmp/recap.json
```

写回的规则也就是"哪些才敢改":一个源名下**每一个**被复验过的端点都 `verified` 才落笔;
`failed` / `degraded` 一律不写。`unreachable`(环境问题)与 `credentials_missing`
(本机没配凭据)默认同样不写 —— 它们不是核对通过,只是这一次没能核对。确实想把这类
"至少源是活的"也记下来的场景,显式加 `--allow-partial-date`。

> **注意**:`--all` 对每个源只复验**第一个非 `entry` 端点**。所以多端点源写回的那条日期,
> 证据覆盖的是那一个端点,不是全部端点 —— 这条允许它是有意的取舍,但别把它读成"整源已核对"。

### 步骤 7 —— 更新部署

```bash
make up                 # docker compose up -d --build
# 或本地直接跑:
make gateway-run
```

改的只是数据文件时,重启即可生效(注册表在进程启动时从 `data/` 构建)。

### 步骤 8 —— 给出调用示例和真实数据(交付物)

```bash
python3 scripts/demo.py "My Journal API" --limit 2
```

输出三样东西,**原样贴给你的用户**:

1. 端点说明(干什么的、必填参数、限速口径);
2. 一条可复制粘贴的 `curl` 命令;
3. **真实的**响应信封(`data` 截断到前 1–2 条)。

### 步骤 9 —— 收尾

```bash
make check              # ruff + mypy + pytest + 数据校验 + Go 构建/测试 + 注册表 parity + 文档站自检
```

然后提 PR。PR 描述必须包含:官方入口、限速口径、规模与增量的**取法**、合规风险判断依据,
以及步骤 6/8 的真实输出片段。

---

## 3. 目录结构:东西都在哪

```
a-source-gateway/
├── gateway/                    Go 网关(主运行时)。cmd/gateway + internal/{config,registry,
│                               cache,parser,upstream,ratelimit,envelope,metrics,server}
├── python/src/a_source_gateway/  Python 解析面 + 贡献者工作流
│   ├── parser_worker.py        解析服务(NDJSON over stdio),由 Go 网关拉起
│   ├── resolver.py             参照实现:URL 渲染 / 缓存键 / SWR / 降级 / 解析投影
│   ├── registry.py             注册表构建规则(slugify / classify_auth / parse_total)
│   ├── schema.py               信封与 spec 的权威定义
│   ├── api.py                  Python 版 HTTP 服务(兼容路径,参照实现)
│   ├── mcp_server.py           MCP 适配(优先走 Go 网关,不可达时回落到本地 resolver)
│   └── cli.py                  命令行入口
├── data/                       唯一事实源(改这里,不改代码)
│   ├── source_inventory.csv    海外源清单(18 列)
│   ├── source_inventory_cn.csv 国内源清单(19 列 = 18 列契约 + 优先级)
│   ├── endpoints.json          端点定义
│   └── aliases.json            源名 → 常用简称
├── scripts/                    智能体接入工具链
│   ├── recon.py                侦察目标站点
│   ├── scaffold_source.py      生成草稿(CSV 行 + endpoints 条目)
│   ├── verify_source.py        证据关卡:真实请求 + 断言
│   ├── demo.py                 产出调用示例与真实数据
│   ├── serve_docs.py           起本地文档站 + /__gateway 代理
│   └── check_docs.py           文档站自检(结构 / 链接 / 导航)
├── site/                       文档站(静态,零依赖;make docs 起本地服务)
├── python/tests/                 pytest(默认不联网)
├── docs/                       方案文档 / 技术选型(ADR)/ 优化报告 / meta 规范 / 性能
├── deploy/                     k8s / systemd / 部署 runbook
├── Dockerfile, docker-compose.yml, Makefile
└── AGENTS.md                   ← 你正在读的文件
```

---

## 4. 命令速查

| 命令 | 作用 |
|---|---|
| `make install` | 安装 Python 包(可编辑模式) |
| `make test` | pytest(默认**不联网**) |
| `make network-test` | 需要访问真实上游的用例 |
| `make lint` / `make type` | ruff / mypy |
| `make validate` | 校验 `data/` 下的数据文件 |
| `make bench` | 延迟基准(写 `bench-report.json`) |
| `make gateway-build` / `make gateway-test` / `make gateway-run` | Go 网关 |
| `make up` / `make down` | docker compose 起停 |
| `make recon URL` | 侦察一个站点 |
| `make verify-source ID` | 证据关卡 |
| `make demo ID` | 调用示例 + 真实数据 |
| `make parity` | 校验 Go 与 Python 构建出的注册表一致 |
| `make docs` | 起本地文档站(带网关代理, 调试台可用) |
| `make docs-check` | 校验文档站(结构 / 内部链接 / 导航齐全) |
| `make check` | 提交前全量检查 |

服务起来之后:

```bash
curl -s "http://127.0.0.1:8848/v1/sources?q=制裁" | python3 -m json.tool
curl -s http://127.0.0.1:8848/v1/sources/crossref/endpoints/works | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8848/v1/query -H 'Content-Type: application/json' \
  -d '{"source":"crossref","endpoint":"works","params":{},"limit":3}' | python3 -m json.tool
```

**怎么找源**:`q=` 可以是中文关键词、源 id、别名(`szse`)、或端点 id(`stock_list`)——
命中任意一处即返回。结果里的 `aliases` 告诉你该用哪个简称去调用:

```bash
curl -s "http://127.0.0.1:8848/v1/sources?q=szse"   # → id=szse, aliases=["szse","szse_stock"]
```

---

## 5. 智能体动手时的边界

- **不要**为了让测试变绿而放宽断言或删测试。
- **不要**在没有真实请求证据的情况下声称"这个源能用"。
- **不要**把凭据写进任何文件(它们只走环境变量)。`A_GATEWAY_CONTACT` 是联系邮箱,不是密钥;
  API key 走 `A_GATEWAY_*` 约定的环境变量,`config.py` 里有完整清单。
- **不要**为了让页面能通就加代理/绕限速。
- 改 `data/` 之后**必须**跑 `make validate`;改了行为**必须**跑 `make check`。
- 涉及具体源的行为(限速口径、规模、增量)**以数据文件为准**,以 `docs/` 为解释;
  如果代码和文档不一致,先查清哪个是新的,再改**两者**。

---

## 6. 人类与智能体的分工

本项目由**多人 + AI 共同维护**:

- **AI 智能体**做重复劳动:侦察、生成草稿、跑验证、产出调用示例。
- **人类维护者**做判断:合规判定、限速口径确认、代码评审、合并。

流程约定:

1. 智能体开的 PR 必须带**真实请求的输出**(步骤 6 / 8 的产物),不能只有"应该可以"。
2. 涉及新增源类型的 PR 需要一位维护者评审数据字段的真实性。
3. 定时任务会复验所有源的可达性与响应契约;**失败会开 issue,不会自动改主分支**
   (上游抖动很常见,自动改会把清单改脏)。

---

## 7. 出问题先看哪里

| 症状 | 先查 |
|---|---|
| `validate` 报错 | `docs/adding-a-source.md` §2–§4 的字段约束 |
| 查询返回 `degraded` | 信封的 `meta.errors[]` 和 `meta.fallback.hint` —— 它们会直接告诉你原因和下一步 |
| 返回空列表 | 先看 `meta.errors`:有 `upstream_not_json` 就是上游给了 HTML 错误页(改上游可访问性,不是改 `item_path`);`errors` 为空且 `degraded=false` 才是"确实没有数据" |
| 按简称能调用、却搜不到源 | `GET /v1/sources?q=` 从 1.2 起匹配源 id / 别名 / 端点 id;搜到的结果里带 `aliases` |
| 403 | 上游要求 UA 带邮箱 → 设 `A_GATEWAY_CONTACT` |
| 超时 / `budget_exceeded` | 该端点 `budget_ms` 与上游实际延迟;必要时调小 `page_size` |
| 响应太大 / `response_too_large` | 调小端点的 `page_size`;文件型源应直连上游 |
| Go 与 Python 注册表不一致 | `make parity`,会打印具体差异行 |
| MCP 工具报错 | MCP 优先走 Go 网关(`A_GATEWAY_URL`),连不上会回落本地 resolver 并打日志 |

---

## 8. 交付判定标准(Definition of Done)

一个"新增数据源"的改动,只有同时满足下面全部条件才算完成:

- [ ] `make validate` 通过
- [ ] `make check` 通过(ruff / mypy / pytest / Go 构建与测试 / parity / 文档站自检)
- [ ] `python3 scripts/verify_source.py <源名> --direct-only` 退出码为 `0`,且输出里有**真实响应样本**
- [ ] `python3 scripts/verify_source.py <源名>`(走网关)要么退出码 `0`,要么是 `DEGRADED`
      (网络慢时的预期结果 —— 关键是它必须给出 `fallback.upstream_url`)
- [ ] `data/aliases.json` 已加别名(除非该源名称本身就是唯一标识)
- [ ] `python3 scripts/demo.py <源名>` 的输出已附在 PR 描述里(**调用示例 + 真实数据**)
- [ ] PR 描述写清了:官方入口、限速口径、规模与增量的取法、合规风险判断依据
- [ ] 没有改动 `src/` 下的 Python 业务逻辑(加源只改数据文件)
