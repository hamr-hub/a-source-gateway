# syntax=docker/dockerfile:1
#
# a-source-gateway —— 单容器双进程镜像
#
# 容器里有两个进程:
#   1. Go 网关     /usr/local/bin/a-source-gateway —— 公共 HTTP 服务,也是容器主进程
#   2. Python 解析 worker  python3 -m a_source_gateway.parser_worker
#                   —— 由 Go 网关拉起并监管,stdio 上跑 NDJSON。
#   Go 侧还有内置的纯 Go JSON 解析器(A_GATEWAY_PARSER=go),
#   所以即使 Python 不可用,容器仍能正确提供服务(降级为 Go 解析)。
#
# ── 必需环境变量(全部有默认值,不设也能跑)────────────────────────────
#   A_GATEWAY_HOST=0.0.0.0         监听地址(容器内必须 0.0.0.0,否则外部访问不到)
#   A_GATEWAY_PORT=8848            监听端口
#   A_GATEWAY_DATA_DIR=/app/data   事实源目录(source_inventory.csv / endpoints.json / aliases.json)
#
# ── 强烈建议设置 ─────────────────────────────────────────────────────
#   A_GATEWAY_CONTACT=you@example.com
#       会拼进 User-Agent。SEC、OpenAlex 等上游据此识别调用方;
#       不带邮箱 SEC 会直接返回 403。这是联系邮箱,不是密钥。
#
# ── 常用可选变量 ─────────────────────────────────────────────────────
#   A_GATEWAY_BUDGET_MS=500        单次调用的总墙钟预算(硬承诺 500ms)
#   A_GATEWAY_UPSTREAM_BUDGET_MS=400 上游请求预算
#   A_GATEWAY_RESERVE_MS=100       留给解析与封装的余量
#   A_GATEWAY_CACHE_ENTRIES=512    进程内缓存条目上限
#   A_GATEWAY_CACHE_BYTES=33554432 进程内缓存字节上限
#   A_GATEWAY_CACHE_TTL_S=900      缓存 TTL(秒)
#   A_GATEWAY_PREWARM=1            启动时预热缓存
#   A_GATEWAY_OFFLINE=0            离线模式:完全不发网络请求
#   A_GATEWAY_PARSER=auto          auto(默认)|python|go
#                                  go = 纯 Go 运行,不拉起 Python worker,
#                                  用于"镜像里没有 Python"的极限场景
#
#   ⚠️ 凭据(上游 API key)永远不要写进镜像;一律走运行时环境变量,
#      Kubernetes 用 Secret 注入,本机用 .env / EnvironmentFile。
#
# ── 镜像内两个同名的 CLI ─────────────────────────────────────────────
#   /usr/local/bin/a-source-gateway       → Go 网关(ENTRYPOINT)
#   /usr/local/bin/a-source-gateway-cli   → Python CLI(a-source-gateway list / validate / bench)
#   Python 包原本也装出 a-source-gateway 这个名字,会和 Go 二进制撞名,
#   因此安装后显式改名,把 /usr/local/bin/a-source-gateway 让给 Go 网关。

# ── 阶段 1:编译 Go 网关 ───────────────────────────────────────────────
FROM golang:1.22-alpine AS go-builder

# 纯静态二进制,运行时不依赖 libc
ENV CGO_ENABLED=0

WORKDIR /src/gateway

# gateway/ 只用 Go 标准库,没有第三方依赖 —— 因此不需要 go mod download,
# 构建期除了基础镜像之外没有额外网络访问。
COPY gateway/ ./

RUN go build -trimpath -ldflags "-s -w" -o /out/a-source-gateway ./cmd/gateway

# ── 阶段 2:运行时 ─────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    A_GATEWAY_HOST=0.0.0.0 \
    A_GATEWAY_PORT=8848 \
    A_GATEWAY_DATA_DIR=/app/data

WORKDIR /app

# README.md 是 pyproject.toml 的 readme 字段,构建元数据里要读它,必须一起 COPY。
# data/ 既是 wheel 的 force-include 输入(装完包内自带一份),
# 也是运行时的事实源目录(A_GATEWAY_DATA_DIR 指向这里)—— 双保险。
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY python/src/ ./python/src/
COPY data/ /app/data/

# python/src/a_source_gateway/data/ 是构建期从仓库根 data/ 同步出来的副本,
# 在 .gitignore 里被忽略(hatchling 默认尊重 VCS ignore,本地构建因此不会把它打进 wheel)。
# 但镜像构建上下文里没有 .gitignore,这个副本会被当成正常包文件,
# 和 pyproject 的 force-include 撞成同一个 wheel 路径 → hatchling 直接报
# "A second file is being added to the wheel archive at the same path"。
# 所以这里显式删掉:wheel 里的数据文件一律来自 force-include(仓库根 data/)。
RUN rm -rf python/src/a_source_gateway/data \
    && pip install --no-cache-dir . \
    && mv -f /usr/local/bin/a-source-gateway /usr/local/bin/a-source-gateway-cli

COPY --from=go-builder /out/a-source-gateway /usr/local/bin/a-source-gateway
COPY deploy/docker/healthcheck.py /usr/local/bin/healthcheck.py

# 非 root 运行:uid/gid 固定 10001,便于 Kubernetes 的 runAsUser 对齐
RUN groupadd --system --gid 10001 gateway \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin gateway \
    && chown -R gateway:gateway /app

USER gateway

EXPOSE 8848

# /livez 只回答"进程还活着吗";就绪状态请看 /readyz
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python3", "/usr/local/bin/healthcheck.py"]

ENTRYPOINT ["/usr/local/bin/a-source-gateway"]
