"""版本号三处一致的守卫(纯离线)。

网关、Python 包与打包元数据各报一个版本号, 少一处同步就会出现
"网关自述 1.5.0、pip 装的包是 1.4.1"这种最难查的不一致 —— 调用方拿到的
信封里写着哪个版本, 取决于他连的是哪一端。

三处:
  1. pyproject.toml                 —— 打包元数据(Makefile 也从这里取 VERSION)
  2. python/src/a_source_gateway/__init__.py —— Python 包的 __version__
  3. gateway/internal/config/config.go         —— Go 网关的 Version 变量
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml 里找不到 version 字段"
    return match.group(1)


def _package_version() -> str:
    text = (REPO_ROOT / "python" / "src" / "a_source_gateway" / "__init__.py").read_text(
        encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE)
    assert match, "__init__.py 里找不到 __version__"
    return match.group(1)


def _gateway_version() -> str:
    text = (REPO_ROOT / "gateway" / "internal" / "config" / "config.go").read_text(
        encoding="utf-8")
    match = re.search(r'^var Version = "([^"]+)"', text, re.MULTILINE)
    assert match, "config.go 里找不到 Version 变量"
    return match.group(1)


def test_three_places_report_the_same_version():
    versions = {
        "pyproject.toml": _pyproject_version(),
        "a_source_gateway.__version__": _package_version(),
        "gateway config.Version": _gateway_version(),
    }
    distinct = set(versions.values())
    assert len(distinct) == 1, f"版本号三处不一致: {versions}"


def test_the_installed_package_reports_the_same_version():
    """真正被 import 的包, 报出来的版本也要一致(防止 __init__.py 改了没生效)。"""
    import a_source_gateway

    assert a_source_gateway.__version__ == _pyproject_version()
