# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
