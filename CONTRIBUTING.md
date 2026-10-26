# 贡献指南

感谢你愿意为 a-source-gateway 出力。这份指南覆盖三件事:**怎么跑起来**、**怎么改**、**怎么提 PR**。

---

## 心智模型(先看这个)

这个项目只有两类东西:

1. **数据文件** —— `data/source_inventory.csv`(海外源的基本信息)、
   `data/source_inventory_cn.csv`(国内源,比海外多一列 `优先级`)、
   `data/endpoints.json`(结构化端点定义)、`data/aliases.json`(源名 → 常用简称)。
   (各文件的具体行数以当前文件为准 —— 源数会随加源增长,别在文档里写死。)
   **加源只改这里。** 两份清单按 `国别` 分工,规则见 [`AGENTS.md`](AGENTS.md) 规则 3.1。
2. **代码** —— `gateway/`(Go 网关)与 `src/a_source_gateway/`(Python 解析面)。负责把数据文件
   变成可调用的接口。

```
data/*.csv + *.json  ──▶  Go 网关 (gateway/)      ──▶  上游官方 API
                            路由/缓存/限速/预算              │
                            │                              ▼ 原始响应
                            ▼                              │
                     Python 解析面 (parser_worker) ─────────┘
                     解析 + 投影 → 统一信封
```

**分工一句话:Go 做网关(传输与调度),Python 做解析(内容与结构)。**

**核心契约:任何一次调用都在 500ms 内返回。** 做不到时降级并给出直连兜底,
而不是挂死。改代码时不要破坏这条。

> ⚠️ 两种语言各实现了一份注册表构建规则。改 `src/a_source_gateway/registry.py` 里的派生规则
> (认证分类、规模解析、slug)时,**必须同步改 Go 侧**,否则 `make parity` 会挂。

---

## 开发环境

```bash
git clone <repo-url> && cd a-source-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

需要 Python ≥ 3.10。改 Go 网关还需要 Go ≥ 1.22(Go 侧**只用标准库**,不需要 `go get` 任何东西)。

### 常用命令

```bash
make help          # 看所有目标
make test          # 单元测试(不联网)
make network-test  # 需要访问真实上游的用例
make lint          # ruff
make type          # mypy
make validate      # 校验数据文件
make bench         # 延迟基准
make run           # 本地起服务(127.0.0.1:8848)

make gateway-build # 构建 Go 网关
make gateway-test  # Go 单元测试
make gateway-run   # 起 Go 网关
make parity        # 校验 Go 与 Python 构建出的注册表一致
make up / make down  # docker compose 起停
```

---

## 加一个数据源(最常见的贡献)

**不需要写代码,只改数据文件。** 完整步骤见 [`docs/adding-a-source.md`](docs/adding-a-source.md)。

如果你用 AI 智能体(MCode / Claude Code),直接看 [`AGENTS.md`](AGENTS.md) §2 —— 那里有
九步流程(侦察 → 补语义 → 生成草稿 → 别名 → 校验 → **真实数据验证** → 部署 → **调用示例** → 收尾),
以及可以直接跑的脚本:

```bash
python3 scripts/recon.py https://api.example.org/works -o /tmp/recon.json   # 侦察
python3 scripts/scaffold_source.py --recon /tmp/recon.json --name "..."      # 生成草稿
python3 scripts/verify_source.py "My Journal API"                           # 证据关卡(真实请求)
python3 scripts/demo.py "My Journal API"                                    # 调用示例 + 真实数据
```

国内源加 `--country '中国'`(落国内清单,可用 `--priority P0|P1|P2` 指定优先级);
`--apply` 对国内源**只写国内清单**,不写 `data/endpoints.json` —— 详见
[`docs/adding-a-source.md`](docs/adding-a-source.md)。

手动加的话,摘要:

1. 先定归属 —— **只看 `国别` 列**:写 `中国` → 加到 `data/source_inventory_cn.csv`
   (19 列 = 海外的 18 列 + `优先级`,取值 `P0`/`P1`/`P2`);其余(全球/多国/欧盟/美国…)→
   加到 `data/source_inventory.csv`(17 列,列含义见该文档)。**同一个源只能出现在一份清单里**;
2. `data/endpoints.json` 加端点定义(可选但强烈建议)—— 国内源**必须**有端点定义才算接入网关,
   没有端点定义的国内行只是候选,网关不会加载它;
3. `data/aliases.json` 加常用简称(键 = CSV 里的 `数据源` 原文);
4. `make validate` 必须通过(它同时校验两份清单,并查国别归属、跨文件重名、优先级取值、
   以及已接入国内源的 ASCII 别名);
5. `python3 scripts/verify_source.py "<源名>"` 必须退出码 0 —— **必须用真实请求验证**,
   降级的调用不算数;
6. PR 里附上 `scripts/demo.py` 的输出(调用示例 + 真实数据)。

> **加了 `data/` 下的新文件?** 四处分发路径都要跟着加,否则线上会静默少源:
> `pyproject.toml` 的 `force-include`、`.github/workflows/release.yml` 的 tarball 拷贝、
> `deploy/README.md` 的 systemd 安装命令、`python/Makefile` 的 `sync-data`。
> 自查方式:按 release 的方式暂存一份 data/,跑 `go run ./cmd/gateway registry-dump`,
> 源数应与仓库一致;并跑一次该目录的 `validate`。

---

## 改代码时的约定

| 约定 | 原因 |
|---|---|
| **预算用 `asyncio.timeout` 总墙钟**,不要用 httpx 的 per-request timeout | 后者只约束单次 socket 操作,慢响应会超时到天荒地老 |
| **客户端不要设 `timeout=`** | 它独立于预算,会静默掐断慢响应 |
| **不要用 `except: pass` 吞异常** | 至少要记进 `self.metrics`,否则问题无从排查 |
| **响应被截断要显式降级**,不要退化成 `_text` 后假装成功 | "查到了但没数据"是最难排查的失败形态 |
| **缓存键只由上游请求决定** | `limit`/`fields` 是网关侧投影,进键会让预热全部落空 |
| **不要给上游加代理池** | 官方限速是授权条件,不是技术障碍 |
| **新增依赖前先问一句** | 这个项目的定位是轻量;Python 运行时依赖 4 个,**Go 侧只用标准库** |
| **改注册表派生规则要同步改 Go 侧** | 两份实现,靠 `make parity` 守住一致性 |
| **解析面 stdout 只走协议,日志一律 stderr** | NDJSON 一行一请求,混进日志就会串协议 |

### Go 侧约定

- **只用标准库**。不要 `go get` 任何第三方模块 —— 保持 `go build` 离线可用,减少供应链面。
- **不要给上游设客户端级 timeout**,预算由每个请求的 context deadline 单独约束(与 Python 侧同源教训)。
- 任何 handler 都要能 `recover`,返回一个降级信封而不是让进程崩。
- 改了 Go 侧的注册表构建规则 → 必须同步改 Python 侧 → `make parity` 必须绿。

### 代码风格

- 注释与 docstring 用中文(与现有代码一致),面向"为什么"而不是"做了什么";
- 公共函数带类型标注;`mypy` 宽松模式但不允许 `no_implicit_optional`;
- 行宽 110;`ruff` 规则集见 `pyproject.toml`;
- 提交前跑 `make lint test validate`。

---

## 测试

```bash
make test           # 默认不联网, 必须秒级通过
make network-test   # 会打真实上游, 只在 CI 或本地有意验证时跑
```

测试用 `httpx.MockTransport` 完全离线(见 `tests/test_resolver.py`),
所以**不需要 mock 框架**,也不依赖网络。

写测试时优先覆盖这几类:

- **信封形状** —— `meta` 必须包含文档里列的全部键;
- **降级路径** —— 上游超时/报错/超限时,`meta.degraded`、`meta.errors`、`meta.fallback`
  必须齐全;
- **缓存语义** —— 第二次调用必须命中;错误响应**不能**被缓存;
- **投影** —— `item_path` / `item_shape` / `fields` / `limit` 的组合行为。

---

## 提交信息

用祈使句、说明动机:

```
fix: 截断响应不再退化成 _text 后静默返回空

响应超过正文上限时, JSON 解析失败会退化成 {"_text": ...},
调用方看到的是"查到了但没数据"—— 最难排查的失败形态。
改为显式报 response_too_large 并给出直连指引。
```

类型:`feat` / `fix` / `docs` / `perf` / `refactor` / `test` / `chore` / `data`。

---

## 提 PR 前

- [ ] `make validate` 通过
- [ ] `make test` 通过
- [ ] `make gateway-test` 通过(改了 Go 侧)
- [ ] `make lint` 无新增告警
- [ ] `make parity` 通过(改了注册表派生规则)
- [ ] 新增源的话,附上官方入口与限速口径的出处
- [ ] **新增源的话,附上 `scripts/verify_source.py` 的真实输出(退出码 0)与
      `scripts/demo.py` 的输出(调用示例 + 真实数据)**
- [ ] 改了 meta 结构的,同步更新 `docs/meta-schema.md`
- [ ] 改了配置项的,同步更新 `README.md` 的配置表
- [ ] 用 AI 生成的贡献,在描述里注明关键的人工介入点(见 [`GOVERNANCE.md`](GOVERNANCE.md) §4)

---

## 行为准则与安全

参与即同意 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
安全问题请按 [`SECURITY.md`](SECURITY.md) 的流程私下报告,**不要**开公开 issue。

治理与决策规则见 [`GOVERNANCE.md`](GOVERNANCE.md)。

---

## 许可

贡献即同意以 MIT 许可授权你的贡献。详见 [`LICENSE`](LICENSE)。

数据文件的来源声明见 [`NOTICE`](NOTICE)。
