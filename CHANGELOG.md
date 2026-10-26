# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.4.1] - 2026-10-26

信息准确性:堵住非 JSON 形状上的"假成功";并给新接口补上延迟基线。

### 修复
- **非 JSON 形状的"假成功"**:csv / tsv 端点拿到上游的 HTML/XML 错误页时,以前
  把错误页当**数据行**返回(`degraded=false`、`count=2`,内容是 HTML 碎片);
  jsonl 端点则跳过所有坏行得到空列表(`degraded=false`、`count=0`)。两者都是
  "成功但没有数据"的假成功 —— 调用方分不清"确实没有数据"和"上游给了错误页",
  这正是 1.3.0 给 `json` 形状修过的那类问题。现在这类**形状不匹配**会显式降级:
  `degraded=true` + `errors[].type=parse_error` + 直连指引 + `data=[]`。
  - 判定规则: csv/tsv 的正文看起来是标记语言(以 `<!doctype` / `<html` / `<?xml`
    开头,或以 `<` 开头且前 512 字节内有 `</`);jsonl 有非空行却一行都解析不成功。
  - 波及 8 个已接入端点:BIS `consolidated_csv`、ECB `series_csv`、Eurostat `toc`、
    FRED `series_csv`、HMLR `monthly_csv`、OFAC `sdn_csv`、OpenSanctions `entities`、
    UK ONS `version_csv`。
  - 两个平面与三份 Python 解析实现(参照面 / worker / stdlib 镜像)给出**逐字相同**
    的结论与文案:`上游返回的不是 CSV(疑似 XML/HTML 错误页); 正文片段: …`;
    有跨平面一致性测试守着,另有反向回归(字段以 `<` 开头的合法 CSV 仍原样返回)。
- **空正文时 `upstream_not_json` 的文案两平面不一致**:Go 侧在正文片段为空时不追加
  `; 正文片段: ` 后缀,Python 侧一直追加。现在两边一致。
- **EIA API 的限速口径不再是"无"**:实测无 key 时 403 `API_KEY_MISSING`、
  复用同一 key 会 429 `OVER_RATE_LIMIT`,清单按实测事实改写(原来写"无"是误导)。

### 变更
- **`bench` 纳入 `POST /v1/search` 用例**:新接口第一次有延迟基线 —— 搜索的墙钟由
  **最慢的那一个源**决定,实测两个关键词用例 p95 198.3ms / 406.8ms,均在 500ms 内;
  选源本身(纯元数据)只占 p50 0.325–0.394ms,不是瓶颈。
- **CSV 行尾契约变成显式校验**:`validate` 现在检查 `data/*.csv` 是否纯 CRLF。
  行尾被工具悄悄改成 LF 时,整份清单会变成一份 108 行的**假 diff**,混在真实改动里
  几乎看不出来 —— 本轮它已经这样发生过两次,现在是一条明确的报错。
- **国内清单的行尾归一化为 CRLF**:它从 1.2.0 落地起就是 LF,与 `.gitattributes` 的
  约定不符(`scaffold_source.py --apply` 按目标文件现有风格写回,归一后不会再漂)。

## [1.4.0] - 2026-10-26

搜索广度:一次请求扇出多个数据源;并把"延迟契约"与"数据正确性"拆成两条判据。

### 新增
- **`POST /v1/search`**:一次请求扇出多个数据源。请求体两种形态 —— `queries`
  (显式列出每条查询打哪个源、哪个端点)或 `q`(按关键词选源,用与
  `GET /v1/sources?q=` 相同的搜索索引);`queries` 非空时优先用它。各源**并发**执行、
  各自一份 ≤500ms 预算,整体墙钟约等于最慢的那一条。响应是**信封数组 + 汇总 meta**
  (`{meta, queries, results}`):顶层 `meta` 给 `requested` / `ok` / `degraded` / `failed` /
  `with_data`(`requested = ok + degraded + failed`,三类互斥),`results[]` 里每一项
  各自是一个源的信封,`queries[i]` 与 `results[i]` 一一对应。
  **单个源失败不影响整次调用**:未知源 / 未知端点 / 上游超时都只写进那一条的 `errors`,
  整体仍返回 200 —— 这与 `POST /v1/query` 遇未知源返回 404 刻意不同(搜索要的是
  "一次看多个源",一个源坏了不该让整次调用失败)。
- **MCP 工具 `search_sources`**:入参 `q`(必填)/ `limit`(默认 5)/ `max_sources`(默认 12),
  行为与 `POST /v1/search` 一致(优先走 Go 网关,连不上时回落本地 resolver),
  返回**紧凑版**结果:每个源给出 source/endpoint/count/total/degraded/errors,
  `data` 按 limit 截断。

### 变更
- **传输层失败改用两个平面共用的词表**:上游**连不上**(域名解析失败 / 连接被拒 / 证书问题)
  报 `connect_error`,**连上了但中途断了**(连接重置 / 服务端提前关闭 / EOF)报 `read_error`,
  其余传输层失败报 `upstream_error`(上游返回 HTTP ≥400 一直就是这个名字);`timeout` 仍是
  `timeout`。此前 Go 网关报 httpx 的异常类名(`ConnectError` / `ReadError` / `UpstreamError`),
  Python 参照面报自己的异常类名(如 `RemoteProtocolError`)—— 同一个失败在两个平面上给出
  **不同的 `errors[].type`**,调用方按 type 分支会漏掉一半。现在两平面逐条对齐,
  `python/tests/test_error_taxonomy.py` 守住这张词表不再外扩。原始异常文本照旧放在
  `errors[].message` 里(各运行时措辞不同,那是事实,不强行统一)。
- **bench 结论拆成两条判据**:`pass` / `latency_ok` 只表示**延迟契约**成立(冷/热路径 p95、
  max 都不超 500ms);数据正确性单独用 `data_ok` / `data_missing` 表达(热路径的每个用例
  都要 `count > 0`),并新增 `verdict` 一句话结论:
  `PASS — 延迟契约与数据正确性都成立` /
  `PASS(仅延迟契约) · 数据正确性未达标: N/M 个热路径用例没有数据` /
  `FAIL — 延迟契约不成立`。能不能拿到数据取决于上游限流与跨境网络,不是网关可控范围,
  所以**不并进 `pass`**;但它必须是一个独立可见的字段 —— 此前"10 个用例全部降级、
  count 全是 0"也会打印一句 PASS,把最该被看到的信号盖掉。详见
  [`docs/performance.md`](docs/performance.md) §2.2。

### 修复
- **搜索的每个源都有 panic 兜底**:各源跑在自己的 goroutine 里,而中间件的
  `RecoverMiddleware` 只兜得住处理请求那个 goroutine —— 一个源崩了会直接带走
  整个网关进程。现在每个源单独 recover,记成那条结果的 `internal_error` 降级,
  其余源照常返回。
- **Python 参照面的默认参数与 Go 侧对齐**:凭据参数(p["env"])在环境变量没配时,
  Python 会把这个参数**连同它声明的 `default` 一起丢掉**,Go 侧则照取 `default`
  —— 同一个端点在两个平面渲染出的 URL 因此不同(搜索的关键词扇出会用到它)。
  语义现在逐条对齐(`_default_params` ↔ `prewarmParams`)。

## [1.3.1] - 2026-10-26

数据源健康度审计(直连实测 112 个端点)后的修复批次。

### 修复
- **UN 安理会综合名单改指最终地址**:官方入口 `scsanctions.un.org` 现在 302 到
  **带签名**的 blob 地址(签名 1 小时过期,写不进端点定义),旧址 `main.un.org` 则是
  202 + 空正文 —— 两条路都取不到名单。实测该 blob 容器是**公开**的,去掉签名参数
  直接可取(200 + 2.5MB XML),端点因此改指公开容器的最终地址。
- **非 JSON 的错误文案统一**:三条路径(Go 引擎 / Python worker / Python 参照面)此前
  各说各话,Go 引擎那条还把解码器的原始报错(`invalid character '<' …`)泄进了对外信封,
  且整串在 300 字符处被截断时尾部片段会连同省略号一起被切掉。
  现在统一成 `上游返回的不是 JSON(疑似 HTML 错误页); 正文片段: …`,两平面逐字相同。
- **凭据参数不再回落到占位符 example**(Go 与 Python 两平面同步):
  环境变量没配时, `YOUR_CENSUS_API_KEY` 这类占位符会被真的发给上游,
  上游回一个 200 + "Invalid Key" 的 HTML 页 —— 状态码是 200, 调用方
  看到的是一次"成功的空结果"而不是"缺凭据"。现在报 `missing_param`。
  数据文件里 7 处占位符 example 一并移除。
- **e-Stat 补上 `env` 声明**:它是唯一没有声明环境变量注入的凭据参数。
- **SEC EDGAR `companyfacts` 的 `item_path`/`total_field` 移除**:
  `facts` 是按 taxonomy 分组的对象, 不是列表, 也没有总量字段。
- **UN 安理会综合名单换址**:`main.un.org` 旧址已返回 202 + 空正文,
  制裁委员会站点的 `scsanctions.un.org` 则 302 到**带签名**的地址(签名 1 小时过期,
  写不进端点定义)—— 两条路都取不到名单,端点最终改指 blob 公开容器的最终地址
  (见上面第一条)。
- **OFAC Consolidated**:PublicationPreview 的导出接口已返回 200 空正文,
  综合名单换成 treasury.gov downloads 目录的 `cons_advanced.xml`;
  DTC 部分官方没有可直接下载的 XML, 按规则 2("宁可诚实")移除端点。
- **`link_checked_at` 不再是硬编码日期**:Go 与 Python 两侧原本都写死
  "2026-10-22", 于是不管多久没核过对外都宣称那天验证的。现在读清单。
- **验证脚本的 UA 拼接**:实测 SEC data.sec.gov 拒绝含 github.com 链接的
  UA(403)。`--contact` 提供邮箱时, UA 改用 `名字 (+邮箱)` 的朴素格式,
  SEC 系源在直连验证时不再永远不可达。

### 变更
- **清单新增 `最后验证` 列**(海外第 18 列, 国内插在 `优先级` 之前):
  这条元数据最近一次被真实请求核对过的日期(ISO);空 = 未核过,
  空着比填一个自己没跑过的日期诚实。本次实测通过的 42 个源已回填
  2026-10-26, 其余留空。
- **下线 Google Patents 端点**:`patents.google.com/xhr/query` 是站内私有
  接口而非官方开放 API, 违反规则 1(合法性优先于功能)。清单标注改为
  "已下线", 建议改接 PatentsView / EPO OPS。
- **移除中国基金业协会的 entry 端点**:它指向官网首页(HTML), 给不出
  结构化数据;该源退回候选池, 清单标注如实说明。
- **移除中国证监会的 entry 端点**(保留可用的 `news` JSON 端点)。

### 新增
- **`validate` 检查占位符 example**:参数 example 匹配 `YOUR_*`/`CHANGEME`
  之类占位符时报错;有 `env` 的必填参数豁免 example/default 要求。
- **`verify_source.py --all` 默认直连上游**(可用 `--via-gateway` 走网关):
  走网关时上游预算只有 400ms, 跨境往返实测 0.4-7.5s, 几乎每个源都
  "正常地"降级 —— 那是一份网络延迟报告, 不是元数据报告。
- **验证报告区分"网络慢"与"元数据写错"**:超时导致的降级单独计为
  `slow`, 不再混进 `failed`(实测 109 源里 103 个 degraded, 81 个只是超时)。
- **bench 报告冷/热路径分离**:新增 `query_warm_p95_max_ms` 与
  `query_cold_p95_ms`。热路径 p95 只有零点几毫秒(全是缓存), 拿它当
  "实时取数延迟"是自欺;终端输出也会明示多少用例全程命中缓存。

## [1.3.0] - 2026-10-26

### 新增
- **源发现覆盖别名与源 id**:`GET /v1/sources?q=` 现在匹配 canonical id、全部别名、
  名称、机构、国别、分类、数据内容摘要与**端点 id**(大小写不敏感)。
  此前只匹配名称/摘要/机构/国别/分类 —— 于是出现"**能按别名调用、却搜不到**"
  (实测 `q=sec_edgar` → 0 条、`q=szse` → 0 条;`q=制裁` → 10 条)。
  现在 `q=sec_edgar` → 1 条、`q=szse` → 1 条、`q=stock_list` → 3 条(三个源都有该端点)。
- **源列表投影新增 `aliases`**:搜到源之后立刻知道该用哪个简称去调用 ——
  "搜到 → 知道怎么调"闭环。键始终在场,没有别名时是 `[]`。
- **新错误类型 `upstream_not_json`**:上游返回的不是 JSON(错误页 / WAF 拦截页)时如实标降级,
  并在 `message` 里带上**正文片段**(单行、限长 240 字符),排障时一眼看出是 HTML 错误页。
- **`A_GATEWAY_HTTP2`(默认 1)**:上游走 HTTP/2;个别上游 h2 实现有问题时设 `0` 强制 HTTP/1.1。

### 变更
- 信封 `schema_version` 1.1 → **1.2**(源投影新增 `aliases` 字段)。Go 与 Python 两平面同步。
- **搜索索引在注册表构建期算好**:请求期不再逐源 ToLower/归一化分配,也不再多趟 O(n) 扫描 ——
  `resolve_source` 与 `list_sources` 走同一份索引。索引是运行时结构,**不进导出注册表**
  (`make parity` 的对齐契约不变)。
- **上游走 HTTP/2**:Go 的 `http.Transport` 设了自定义 `DialContext`,标准库会**保守禁用** HTTP/2,
  于是每次 HTTPS 调用都退回 HTTP/1.1、各自付一次 TCP + TLS 握手。
  现在显式 `ForceAttemptHTTP2`(ALPN 协商;上游不支持 h2 自动回落 HTTP/1.1,不会失败)。
- **上游状态 ≥400 且正文是 HTML 时补报 `upstream_error`(带 `status`)** —— 状态码不再在降级分支里丢失。

### 修复
- **"成功 + 0 条"的假成功**:上游返回非 JSON 正文时,解析层曾静默兜底成 `{"_text": …}`,
  投影得到空列表而 `meta.degraded` 仍是 `false` —— 调用方分不清"确实没有数据"和"上游报错"。
  现在降级 + `upstream_not_json` + 正文片段 + 直连兜底。
- **一次 HTML 错误页不再让解析面永久降级**:解析管理器曾把"上游返回非 JSON"当成
  "Python worker 故障",于是打 `worker failed` 警告并**永久**切到 Go 引擎。
  现在这种错误与 deadline/cancel 同等对待 —— worker 保持可用,下一次正常请求仍走 Python。
- **证据关卡诊断口径**:`verify_source.py` 现在区分"上游给了错误页"与"端点元数据写错了" ——
  前者报 `upstream_body_is_json` 失败并**跳过** item_path / total_field / 字段名断言
  (拿错误页比对路径只会误导), 后者的判定逻辑不变。

### 已知限制
- `ListSources` 的关键词匹配仍是子串语义(要遍历全部源),只是每源零分配;
  源规模到万级时需要换倒排索引。
- Python 参照面**仍走 HTTP/1.1**:httpx 要用 h2 得装 `h2` 包,本项目不把它列为依赖。
  传输协议不属于信封 / 注册表的语义契约,Go 网关才是主运行时。
- **"上游返回的不是 JSON"只对 `json` 形状生效**:`csv`/`tsv` 会把 HTML 当数据行解析,
  `jsonl` 会跳过坏行得到空列表 —— 这两种形状下,上游的错误页**不会**被标成
  `upstream_not_json`(两平面行为一致,不是两平面不一致)。

## [1.2.0] - 2026-10-24

### 新增
- **国内 A 类源清单** `data/source_inventory_cn.csv`:按 `国别` 与海外清单分工,
  34 个国内源(10 个已接入 + 24 个候选),前 17 列与海外清单同名同序,多一列 `优先级`。
- **`response.item_flatten`**(默认 false):列表套列表时展平一层(上交所公告的形状)。
- **`item_path` 支持数字下标**:与 `total_field` 同一套路径语义
  (深交所 A 股列表:顶层是数组 `[{metadata:…, data:[…]}]`)。
- **缓存命中返回真实记录**:此前缓存里存的是解析结果包装(字段未导出, 序列化成 `{}`),
  于是**任何第二次调用都拿到空壳**;现在存的是投影后的载荷。
- **空结果只短暂缓存**(30s):上游在高并发下偶发 200 + 空载荷时, 不会把空壳钉住整个 TTL。

### 变更
- 信封 `schema_version` 1.0 → 1.1(新增 `item_flatten` 端点字段)。
- **`item_path` 的数字下标会真正生效(行为变更)**:此前顶层是数组时 `item_path` 被**忽略**,
  网关返回整个数组 —— 与端点元数据自相矛盾, 证据关卡也判不出来(假绿)。
  现在 `item_path: "1"`(World Bank)与 `"0.object.vectorDataPoint"`(Statistics Canada)
  这类既有端点返回的是**定位后的记录**,而不是整个数组。调用方若依赖旧的"整个数组"
  形状, 需要相应调整。
- `make validate` 同时校验两份清单, 并查国别归属、跨文件重名、优先级取值,
  以及"已接入国内源必须有 ASCII 别名"(源 id 不能随清单顺序漂移)。

### 已知限制
- 上游**偶发返回非预期正文**(如 HTML 错误页)时, 解析层会兜底成 `{"_text": …}`,
  投影得到空列表但 `meta.degraded` 仍为 false —— 调用方看不出"这次没拿到数据"。
  影响面小(本来就取不到记录), 但排障时需留意 `meta.errors` 为空而 `data` 为空的情况。

## [1.1.0] - 2026-10-23

### 新增
- **Go 网关面**(`gateway/`):路由、注册表、缓存(SWR + 自适应 TTL)、按站限速、
  总墙钟预算、上游取数与正文上限、Prometheus 指标、OpenAPI。**只用 Go 标准库**,
  无第三方依赖。
- **Python 解析面**(`parser_worker`):以子进程方式运行,用换行分隔 JSON(NDJSON)
  over stdio 通信;负责 JSON / XML / CSV / TSV / JSONL / text 的解析与投影。
- Go 内置 JSON 兜底解析器(`A_GATEWAY_PARSER=go`),没有 Python 也能跑;
  实际使用的引擎始终暴露在 `meta.parser`。
- **智能体自助接入工具链**(`scripts/`):
  - `recon.py` —— 侦察目标站点(可达性/认证/限速头/推断 `item_path`+`total_field`);
  - `scaffold_source.py` —— 生成 CSV 行 + 端点定义草稿(默认不动真实数据文件);
  - `verify_source.py` —— **证据关卡**:真实请求 + 断言 + 真实样本,三态结论
    (通过 / 失败 / 不可达);
  - `demo.py` —— 产出调用示例与真实数据。
- `AGENTS.md` / `CLAUDE.md`:智能体操作手册 —— "把 xxx 网站加进来"的九步流程。
- `GOVERNANCE.md`:治理章程(角色、决策规则、AI 辅助贡献规范)。
- `.github/CODEOWNERS`:代码归属映射。
- `meta.parser` 与 `meta.trace_id` 两个增量信封字段。
- 容器化:`Dockerfile`(多阶段)、`docker-compose.yml`、`deploy/`(K8s + systemd + runbook)。

### 变更
- **目录结构改为单体仓库双平面**:`python/src/a_source_gateway/`(解析面)与
  `gateway/`(Go 网关)并列,`data/` 留在仓库根由两个平面共享。
- `pyproject.toml` 移回仓库根;Python 包路径改为 `python/src/a_source_gateway`,
  **修掉了 `packages = ["src/a_source_gateway"]` 指向不存在目录导致包从未被正确安装的缺陷**。
- 别名表从 `src/registry.py` 的硬编码字典迁到 `data/aliases.json` —— "加源不改代码"
  成为硬约束,这也是智能体自助接入的前提。
- `data/` 通过 hatchling `force-include` 直接打进 wheel,不做"复制一份进包"的同步步骤。
- MCP server 优先转发到 Go 网关(`A_GATEWAY_URL`),网关不可达时回落到进程内 Python 实现,
  保证查询逻辑只有一份实现而 MCP 仍可独立使用。

### 新增配置项
- `A_GATEWAY_PARSER`(`auto`/`python`/`go`)、`A_GATEWAY_PARSER_PYTHON`、
  `A_GATEWAY_URL`、`A_GATEWAY_TIMEOUT_S`。

### 新增(发布前打磨)
- **`GET /openapi.json`**:Go 网关此前只能用 `gateway openapi -o` 离线导出,
  FastAPI 参照实现却自带该路径。现在两平面在同一路径提供 OpenAPI 3.1 文档,
  paths 集合实测完全一致(11 条路径),文档从路由表生成不会漂移。
- **`asg_query_latency_ms` 直方图**:此前只有 avg/max 两个 gauge,
  无法回答"多少比例的调用超过 500ms 契约"。现在按
  `[10…500…5000]ms` 桶输出标准 `_bucket/_sum/_count`,Go/Python 桶边界严格一致,
  500ms 两侧更密。
- `verify_source.py --all --json` 全量复验模式:逐源子进程隔离,
  汇总 verified/failed/unreachable,退出码=未通过源数(上限 100),
  单源 90s 超时按 unreachable 计;`make verify-all` 一键跑。

### 变更(发布前打磨)
- 每周链接检查(`link-check.yml`)从只扫海外清单扩展为海外 + 国内两份清单。
- `verify-sources.yml` 删除 80 行 shell 逐源退路,直接调用新落地的 `--all`。
- GitHub Actions 全部升级到当前大版本(checkout v7、setup-go v7、
  setup-python v6、cache v6、download-artifact v8)。

### 评审修复(独立 code review 后修的)

- **B1 · 500ms 契约在 Go 网关上不成立(阻塞级)**:
  解析阶段原本新起一个完整 500ms 预算(而不是用**剩余**预算),且 Python 解析 worker 的
  互斥锁不是 context-aware —— 等锁的请求会把上一个请求的解析时长整个加在自己身上。
  实测单次 579ms、并发 701ms。现在解析阶段跑在 `deadline − ReserveMS` 之下,
  worker 槽位改成可被 deadline 取消的信号量并按请求 id 解复用;修后 419ms / 400ms,
  **0 超预算**。新增 `budget_test.go` 回归(跑真实 Python 解析面)。
- **`{url}` 占位符不再原样注入**:此前 `{url}` 会被原样替换, 调用方可借此追加
  查询参数(`&admin=true`)或用 `#` 截断。现在所有占位符一律百分号编码(路径参数保留 `/`),
  Python / Go / 镜像实现三处同步。
- **裸查询参数不再让 Python 侧崩溃**:模板里出现没有 `=` 的键时, Python 渲染抛
  `IndexError`(Go 侧正常)—— 同一份数据文件在 Go 网关上能用、在 MCP 回落路径上会炸。
- **NDJSON 协议补上端到端测试**:此前两套实现的协议零自动化覆盖。新增
  `test_parser_worker_e2e.py`(真子进程 + 真管道, 覆盖"每请求一行""未知 op 不死"
  "不失步""stdout 只走协议")与 `test_mirror_parity.py`(镜像实现与 resolver 逐例对齐)。
- **Python 3.10 上超时类型不对(预存缺陷)**:3.10 的 `asyncio.TimeoutError` 与内建
  `TimeoutError` 是两个类, 兼容层抛错了类, 导致信封里的 `errors[].type` 变成
  `"TimeoutError"` 而不是 `"timeout"`。3.10 在 CI 矩阵里, 必须修。
- **上游跳转限制为同主机**:此前跟随最多 10 跳, 一个 302 到内网的官方站会被跟进。
  同时把 OFAC / HMLR 三个端点模板钉到跳转后的最终主机, 从源头免掉跳转。
- **`GET /v1/query?params=[1,2]`** 在两个平面上状态码不一致, 统一为 400。
- `.gitignore` 的行尾注释导致 5 条忽略规则全部失效(只有行首的 `#` 才是注释),
  陈旧的 `registry.json`(97 源快照)会被误提交。
- 新增 `.gitattributes`:把 `data/*.csv` 标成 `-text`, 保证 CRLF 逐字节入库
  (否则 `scaffold_source.py --apply` 的"老行一个字节没动"承诺在克隆后不成立)。
- 文档与实现漂移更正:正文上限 1MB(文档写 2MB)、后台补数预算 30s(文档写 15s)。

### 修复
- **Python `/metrics` 返回的不是 Prometheus 文本(真实 bug)**:FastAPI 对裸
  `str` 返回值会做 JSON 编码,实际响应是一个被引号包住、换行全被转义的
  JSON 字符串(`content-type: application/json`),Prometheus 无法解析。
  改为 `PlainTextResponse`,并加 content-type 与无转义换行的断言。
- **CI secret-scan 在新仓库必挂**:`gitleaks-action` 基于
  `<首提交>^..HEAD` 的 git log 区间扫描,首提交没有父引用导致区间非法。
  改为 gitleaks CLI 直接扫工作树 + `.gitleaks.toml`(依赖目录白名单),
  不再依赖 git 历史深度。
- **CI 容器镜像任务在 PR 上必挂**:PR 事件的 `GITHUB_REF_NAME` 形如 `2/merge`,
  直接拼镜像 tag 是非法引用。改为 main 推 `:latest`、`v*` tag 推同名 tag、
  PR 只构建不打 tag 不推送。
- 包安装路径错误:`packages = ["src/a_source_gateway"]` 指向不存在的目录,
  `pip install -e .` 装不出可导入的 `a_source_gateway`。
- 别名键 `HM Land Registry Price Paid` 与清单里的 `HMLR Price Paid Data` 对不上,
  导致 `hmlr` / `landregistry` / `ppd` 别名从未生效。
- 端点参数类型 `boolean` 不在 schema 契约内(应为 `bool`)。
- **全量复验后修正 10 个源的登记漂移**(逐个打真实上游实测出来的,不是推测):
  `congress_gov_govinfo` 端点补 `api_key`(上游 v3 全部要 key,实测 403 API_KEY_MISSING);
  `who_gho_ghed_ictrp` 端点补 `&$count=true`(OData 在 `$top` 时默认不回 `@odata.count`);
  `usgs` 的 `format=rss` 已被上游下线(实测 400),改 XML + 必填 `stateCd`,并新增
  `earthquakes` GeoJSON 端点; `imf_ifs_bis_oecd_sdmx` 的 `format=sdmx-json` 不是合法值
  (实测 406),改 `json-structure-2.0.0`,`datamapper` 改用 `item_shape=map`;
  `pubmed_e_utilities` 的 `esearchresult.count` 上游返回字符串,去掉抽不出总数的
  `total_field` 声明; `unpaywall` 的 `email` 占位示例被上游拒收(实测 422);
  `courtlistener_recap` / `world_bank` 的认证方式改回真实值(实测 401);
  `patentsview` / `registers_of_scotland_ppd` 的官方入口域名已死,改到现役地址。
  详见 [`docs/verification-2026-10-23.md`](docs/verification-2026-10-23.md)。

---

## [1.0.1] - 2026-10-23

> 已被 [1.1.0](#110---2026-10-23) 取代:该 tag 与本仓库当前的 tag 指向同一份代码
> (包含双平面架构), 版本号按语义化版本应为次版本号, 故以 1.1.0 为准。
> 保留此节以记录发布过程。

---


## [1.0.0] - 2026-10

首个公开版本:97 个 A 类数据源、109 个端点、500ms 契约、HTTP API + MCP server。
以下为 v1.0 的变更记录。

### 新增
- `a-source-gateway` CLI: `serve` / `mcp` / `list` / `show` / `build-registry` / `validate` / `bench`
- `/metrics`(Prometheus 文本格式,零依赖)、`/livez`、`/readyz`
- stale-while-revalidate:缓存过期时立即返回陈旧值并后台刷新
- 自适应 TTL:热条目(被访问 ≥3 次)TTL 缩到 1/4
- 源要求 UA 带联系方式但未配置时,返回可执行的 `contact_required` 提示(0 ms)而不是让用户撞 403
- 自动把 `A_GATEWAY_CONTACT` 填进端点里需要 `email`/`mailto` 的参数

### 变更
- 端点定义从 Python 字面量迁到 `data/endpoints.json`,加源不再需要改代码
- 注册表改为运行时从数据文件构建(约 6 ms),消除"生成物与源文件不同步"
- 上游预算收紧为 400 ms 总墙钟,预留 100 ms 给解析与序列化
- 正文上限 1 MB;CSV/TSV 最多解析 15000 行
- `User-Agent` 改为可配置,默认带项目标识
- 认证分类改用规则表(原先把 65 个「无key」的源误判成需要 key)
- 清单 CSV 的「实测增量」列恢复为真实增量,「代理需求」列恢复原值

### 修复
- 端点键名与清单源名不一致,导致 HMLR 的专用端点从未生效
- 客户端级 httpx timeout 独立于预算掐断慢响应,使后台补数静默失败
- 响应超上限截断后 JSON 解析失败,退化成 `_text` 并静默返回空
- 上游错误响应被缓存
- 后台补数失败被静默吞掉,现记入 `asg_warm_errors_total`
