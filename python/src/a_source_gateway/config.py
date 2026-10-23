"""
config.py - 运行期配置

全部走环境变量, 不引入配置文件依赖; 任何一项都能用环境变量覆盖。
默认值面向"公开官方数据源 + 本机部署", 不含任何凭据。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 包内数据文件(随 wheel 一起安装)
DATA_SUBDIR = "data"
INVENTORY_CSV = "source_inventory.csv"
CN_INVENTORY_CSV = "source_inventory_cn.csv"   # 国内清单(规则 3.1)
ENDPOINTS_JSON = "endpoints.json"
ALIASES_JSON = "aliases.json"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


@dataclass
class Config:
    # ── 服务 ──
    host: str = field(default_factory=lambda: _env("A_GATEWAY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("A_GATEWAY_PORT", 8848))
    workers: int = field(default_factory=lambda: _env_int("A_GATEWAY_WORKERS", 1))
    # Go 网关的地址(两平面架构: Python 只做解析, 网关面是 Go)
    gateway_url: str = field(default_factory=lambda: _env("A_GATEWAY_URL", "http://127.0.0.1:8848"))
    gateway_timeout_s: float = field(default_factory=lambda: _env_float("A_GATEWAY_TIMEOUT_S", 3.0))

    # ── 预算 ──
    budget_ms: int = field(default_factory=lambda: _env_int("A_GATEWAY_BUDGET_MS", 500))
    upstream_budget_ms: int = field(default_factory=lambda: _env_int("A_GATEWAY_UPSTREAM_BUDGET_MS", 400))
    reserve_ms: int = field(default_factory=lambda: _env_int("A_GATEWAY_RESERVE_MS", 100))
    max_body_bytes: int = field(default_factory=lambda: _env_int("A_GATEWAY_MAX_BODY_BYTES", 1024 * 1024))
    max_parse_rows: int = field(default_factory=lambda: _env_int("A_GATEWAY_MAX_PARSE_ROWS", 15000))
    connect_pool: int = field(default_factory=lambda: _env_int("A_GATEWAY_POOL_SIZE", 32))

    # ── 缓存 ──
    cache_entries: int = field(default_factory=lambda: _env_int("A_GATEWAY_CACHE_ENTRIES", 512))
    cache_bytes: int = field(default_factory=lambda: _env_int("A_GATEWAY_CACHE_BYTES", 32 * 1024 * 1024))
    cache_ttl_s: int = field(default_factory=lambda: _env_int("A_GATEWAY_CACHE_TTL_S", 900))
    warm_budget_ms: int = field(default_factory=lambda: _env_int("A_GATEWAY_WARM_BUDGET_MS", 30000))
    prewarm: bool = field(default_factory=lambda: _env_bool("A_GATEWAY_PREWARM", True))

    # ── 身份与网络 ──
    contact: str = field(default_factory=lambda: _env("A_GATEWAY_CONTACT", ""))
    user_agent: str = field(default_factory=lambda: _env("A_GATEWAY_UA", ""))
    offline: bool = field(default_factory=lambda: _env_bool("A_GATEWAY_OFFLINE", False))
    verify_tls: bool = field(default_factory=lambda: _env("A_GATEWAY_VERIFY", "true").lower() != "none")

    # ── 数据文件(开发时可指向仓库里的 data/) ──
    data_dir: Path | None = None

    def __post_init__(self) -> None:
        d = _env("A_GATEWAY_DATA_DIR")
        if d:
            self.data_dir = Path(d).expanduser().resolve()
        if not self.user_agent:
            ua = f"a-source-gateway/1.0 (+{self.contact})" if self.contact \
                else "a-source-gateway/1.0 (+https://github.com/a-source-gateway)"
            self.user_agent = ua

    # ── 数据文件定位 ──
    def _candidate_dirs(self):
        if self.data_dir:
            yield self.data_dir
        # 仓库布局(v2 单体仓库): 事实源在 <root>/data, 包在 <root>/python/src/a_source_gateway
        here = Path(__file__).resolve().parent
        yield here.parent.parent.parent.parent / DATA_SUBDIR   # .../python/src/a_source_gateway -> <root>/data
        yield here.parent.parent.parent / DATA_SUBDIR          # 旧布局: src/a_source_gateway -> <root>/data
        yield here.parent.parent / DATA_SUBDIR                 # 更旧布局: src/a_source_gateway -> data
        yield here / DATA_SUBDIR                               # wheel 内包数据 / 扁平安装
        try:
            from importlib.resources import files
            yield Path(str(files("a_source_gateway") / DATA_SUBDIR))
        except Exception:
            pass

    def find_data(self, filename: str) -> Path:
        for d in self._candidate_dirs():
            p = d / filename
            if p.exists():
                return p
        raise FileNotFoundError(
            f"找不到数据文件 {filename}; 已尝试: "
            + ", ".join(str(d / filename) for d in self._candidate_dirs()))

    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept-Encoding": "identity"}


def load_config(**overrides) -> Config:
    cfg = Config()
    for k, v in overrides.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg
