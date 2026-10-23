PY ?= python3

.PHONY: sync-data test lint type fmt check build clean

sync-data:  ## 把仓库根 data/ 同步进包(构建自包含 wheel 前执行; 文件 gitignored)
	mkdir -p src/a_source_gateway/data
	cp ../data/source_inventory.csv src/a_source_gateway/data/
	cp ../data/source_inventory_cn.csv src/a_source_gateway/data/
	cp ../data/endpoints.json src/a_source_gateway/data/

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests

type:
	$(PY) -m mypy src

fmt:
	$(PY) -m ruff check --fix src tests

check: lint type test  ## 提交前检查

build: sync-data  ## 构建自包含 wheel/sdist
	$(PY) -m build

clean:
	rm -rf dist build *.egg-info src/a_source_gateway.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
