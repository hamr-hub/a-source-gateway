# a-source-gateway —— 开发命令入口
#
# 布局(v2 单体仓库):
#   <root>/pyproject.toml                 Python 项目根(本文件所在目录,pip install -e . 在这里跑)
#   <root>/python/src/a_source_gateway/   Python 解析面(包名 a_source_gateway)
#   <root>/python/tests/                  Python 测试(pyproject 里 testpaths 指向这里)
#   <root>/data/                          事实源(Go 网关与 Python 共享)
#   <root>/gateway/                       Go 网关(独立 go.mod,只用标准库)

PY ?= python3
PORT ?= 8848
IMAGE ?= a-source-gateway:local
URL ?=
SOURCE ?=

.PHONY: help install dev test network-test lint type fmt validate bench bench-gateway run build clean \
        gateway-build gateway-test gateway-run \
        docker-build docker-run up down \
        recon verify-source demo parity check

help:  ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── Python ────────────────────────────────────────────────────────────
install:  ## 安装本项目(可编辑模式)
	$(PY) -m pip install -e .

dev:  ## 安装开发依赖
	$(PY) -m pip install -e ".[dev]"

test:  ## 单元测试(不联网)
	$(PY) -m pytest

network-test:  ## 需要访问真实上游的用例
	$(PY) -m pytest -m network -v

lint:  ## ruff 检查
	$(PY) -m ruff check python/src python/tests scripts

type:  ## mypy 类型检查
	$(PY) -m mypy python/src

fmt:  ## ruff 自动修复
	$(PY) -m ruff check --fix python/src python/tests scripts

validate:  ## 校验 data/ 下的数据文件
	$(PY) -m a_source_gateway.cli validate

bench:  ## 延迟基准(写 bench-report.json)
	$(PY) -m a_source_gateway.cli bench --rounds 3 --cold-rounds 2 --out bench-report.json

run:  ## 本地起 Python 版服务(PORT 可覆盖,默认 8848)
	A_GATEWAY_PORT=$(PORT) $(PY) -m a_source_gateway.cli serve

build:  ## 构建 wheel 与 sdist
	$(PY) -m build

clean:  ## 清理构建产物与缓存
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache .mypy_cache .parity bin
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -not -path './.venv/*' -prune -exec rm -rf {} +

# ── Go 网关 ───────────────────────────────────────────────────────────
gateway-build:  ## 编译 Go 网关到 bin/a-source-gateway
	cd gateway && go build -o ../bin/a-source-gateway ./cmd/gateway

gateway-test:  ## 跑 Go 网关的单元测试
	cd gateway && go test ./...

gateway-run: gateway-build  ## 编译并运行 Go 网关(端口 $(PORT))
	A_GATEWAY_PORT=$(PORT) ./bin/a-source-gateway

# 基准跑在**主运行时**上。README 上的延迟数字必须来自 Go 网关 ——
# `make bench` 起的是 Python 参照实现, 两者不是同一个运行时。
bench-gateway: gateway-build  ## 对 Go 网关跑延迟基准(主运行时)
	@echo ">>> 启动 Go 网关 $(PORT) ..."
	@A_GATEWAY_PORT=$(PORT) ./bin/a-source-gateway > /tmp/asg-bench-gw.log 2>&1 & \
	 echo $$! > /tmp/asg-bench-gw.pid; \
	 sleep 2; \
	 $(PY) -m a_source_gateway.bench --url http://127.0.0.1:$(PORT) \
	   --rounds 3 --cold-rounds 2 --out bench-report.json; \
	 rc=$$?; kill $$(cat /tmp/asg-bench-gw.pid) 2>/dev/null; \
	 rm -f /tmp/asg-bench-gw.pid; exit $$rc

# ── 容器 ──────────────────────────────────────────────────────────────
docker-build:  ## 构建容器镜像
	docker build -t $(IMAGE) .

docker-run:  ## 运行容器(端口 $(PORT))
	docker run --rm -p $(PORT):8848 -e A_GATEWAY_CONTACT="$(or $(A_GATEWAY_CONTACT),)" $(IMAGE)

up:  ## docker compose 起服务
	docker compose up -d --build

down:  ## docker compose 停服务
	docker compose down

# ── 智能体工具链(scripts/ 车道)────────────────────────────────────────
# 这三个目标直接透传给 scripts/ 下的脚本。
# 用法:make recon https://example.org/api
#      make verify-source crossref
#      make demo crossref
# (也可以写成 make recon URL=... / make demo SOURCE=...)
recon:  ## 侦察一个站点
	$(PY) scripts/recon.py $(or $(URL),$(filter-out $@,$(MAKECMDGOALS)))

verify-source:  ## 证据关卡(真实请求 + 断言)
	$(PY) scripts/verify_source.py $(or $(SOURCE),$(filter-out $@,$(MAKECMDGOALS)))

demo:  ## 调用示例 + 真实数据
	$(PY) scripts/demo.py $(or $(SOURCE),$(filter-out $@,$(MAKECMDGOALS)))

# ── 注册表 parity ─────────────────────────────────────────────────────
# Go 网关与 Python 必须从同一份 data/ 构建出同一份注册表。
# 比对时忽略易变字段:生成时间、链接探测时间、探测到的状态码。
define PARITY_PY
import json, sys
V = {'generated_at', 'link_checked_at', 'link_status'}
def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in V}
    if isinstance(o, list):
        return [strip(i) for i in o]
    return o
def firstdiff(x, y, path=''):
    if isinstance(x, dict) and isinstance(y, dict):
        for k in sorted(set(x) | set(y)):
            if k not in x:
                return path + '/' + k + ' (只在 Go 里)'
            if k not in y:
                return path + '/' + k + ' (只在 Python 里)'
            d = firstdiff(x[k], y[k], path + '/' + k)
            if d:
                return d
        return ''
    if isinstance(x, list) and isinstance(y, list):
        if len(x) != len(y):
            return path + ' (长度不同 py=' + str(len(x)) + ' go=' + str(len(y)) + ')'
        for i, (p, q) in enumerate(zip(x, y)):
            d = firstdiff(p, q, path + '[' + str(i) + ']')
            if d:
                return d
        return ''
    if x != y:
        return path + ': py=' + repr(x)[:100] + ' go=' + repr(y)[:100]
    return ''
a = strip(json.load(open(sys.argv[1], encoding='utf-8')))
b = strip(json.load(open(sys.argv[2], encoding='utf-8')))
if a == b:
    print('注册表一致(Python 与 Go 一致)')
    sys.exit(0)
print('注册表不一致(Python 与 Go 有差异)', file=sys.stderr)
da = {s['id']: s for s in a.get('sources', [])}
db = {s['id']: s for s in b.get('sources', [])}
diff = [i for i in sorted(set(da) & set(db)) if da[i] != db[i]]
print('  差异源(' + str(len(diff)) + '): ' + ', '.join(diff[:10]), file=sys.stderr)
for i in diff[:3]:
    print('  [' + i + '] ' + firstdiff(da[i], db[i]), file=sys.stderr)
sys.exit(1)
endef
export PARITY_PY

parity:  ## 校验 Go 与 Python 构建出的注册表一致
	@mkdir -p .parity
	$(PY) -m a_source_gateway.cli build-registry --out .parity/registry.python.json
	cd gateway && go run ./cmd/gateway registry-dump -o ../.parity/registry.go.json
	@$(PY) -c "$$PARITY_PY" .parity/registry.python.json .parity/registry.go.json

# ── 全量检查 ──────────────────────────────────────────────────────────
check: lint type test validate gateway-test parity  ## 提交前全量检查

# ── 跨车道依赖 ────────────────────────────────────────────────────────
# demo / verify-source / recon → scripts/ 下的工具链(scripts 车道)
# gateway-* / parity            → gateway/ 下的 Go 代码(Go 车道)
# 哪条车道还没就位,对应目标就会直接报错 —— 这是有意的,
# 不要用假目标或者注释掉来糊过去。

# ── 兜底规则 ──────────────────────────────────────────────────────────
# `make demo crossref` 里的 crossref 会被 make 当成"另一个要构建的目标",
# 没有这条规则就会报 "No rule to make target 'crossref'"。
# 这里把它吃掉(当作脚本目标的参数),但只允许它出现在
# recon / verify-source / demo 后面 —— 其它位置的未知目标一律报错,
# 免得把拼错的目标名静默吞掉。
%:
	@if [ -e '$@' ]; then \
		exit 0; \
	elif [ "$(words $(MAKECMDGOALS))" = "1" ]; then \
		echo "未知目标 '$@'。可用目标见: make help" >&2; \
		exit 2; \
	elif [ -z "$(filter recon verify-source demo,$(MAKECMDGOALS))" ] || [ -n "$(filter $@,recon verify-source demo)" ]; then \
		echo "未知目标 '$@'(它不是脚本目标的参数)。可用目标见: make help" >&2; \
		exit 2; \
	fi
