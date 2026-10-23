# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

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

### 修复
- 包安装路径错误:`packages = ["src/a_source_gateway"]` 指向不存在的目录,
  `pip install -e .` 装不出可导入的 `a_source_gateway`。

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
