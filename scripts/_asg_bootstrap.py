"""
_asg_bootstrap.py - scripts/ 下的公共底座。

三件事:
1) 按文件路径加载 scripts/ 里的兄弟模块 —— scripts/ 刻意不做成包(没有 __init__.py),
   这样 pytest 收集时不会把 scripts/ 当成测试包; 测试里用 importlib 按路径加载。
2) 让 `import a_source_gateway` 在所有已知布局下都成立:
   - python/src/a_source_gateway/...  (当前单体仓库布局)
   - src/a_source_gateway/...          (旧 src 布局)
   - src/...                           (更旧的扁平布局)
   优先用已安装的包(import a_source_gateway); 否则把发现的路径塞进 sys.path;
   扁平布局(包目录叫 src)只能现场注册一个包对象。
3) 定位仓库根与 data/ 目录。

这个模块只依赖标准库, 顶层不做任何重活(导入即插 .pth 也不会拖慢解释器启动)。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS_DIR = Path(__file__).resolve().parent


class UpstreamNotJSONError(Exception):
    """上游正文不是 JSON(疑似 HTML 错误页 / WAF 拦截页)。

    scripts/ 下两个后端(网关 + 离线镜像)各自的异常类型不同, 这里统一成
    一个, 让调用方(verify_source.py)不用关心当前挂在哪个后端上。
    """


# ── 仓库定位 ─────────────────────────────────────────────────────────────
def repo_root() -> Path:
    """仓库根: 向上找带 pyproject.toml 且带 data/ 的目录。"""
    here = Path(__file__).resolve()
    for cand in [here.parent.parent, *here.parents]:
        if (cand / "pyproject.toml").is_file() and (cand / "data").is_dir():
            return cand
    # 兜底: scripts/ 的上一级
    return here.parent.parent


def default_data_dir() -> Path:
    return repo_root() / "data"


# ── 兄弟模块加载 ─────────────────────────────────────────────────────────
def load_sibling(name: str) -> ModuleType:
    """按路径加载 scripts/<name>.py, 缓存在 sys.modules 里避免重复执行。"""
    path = SCRIPTS_DIR / f"{name}.py"
    key = f"_asg_{name}"
    cached = sys.modules.get(key)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:      # pragma: no cover - 路径一定存在
        raise ImportError(f"加载 {path} 失败")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 网关包引导 ───────────────────────────────────────────────────────────
def package_candidates(root: Path) -> list[tuple[Path, str]]:
    """已知布局: (包目录, 模式)。模式 insert=把父目录塞进 sys.path; alias=现场注册包对象。"""
    return [
        (root / "python" / "src" / "a_source_gateway", "insert"),   # 当前单体仓库布局
        (root / "src" / "a_source_gateway", "insert"),               # 旧 src 布局
        (root / "src", "alias"),                                     # 更旧的扁平布局
    ]


def ensure_gateway_package() -> ModuleType | None:
    """确保 `import a_source_gateway` 可用; 成功返回模块, 失败返回 None。

    优先用已安装/已注册的包; 否则按已知布局现场把包路径挂上。
    __init__.py 执行失败(例如缺 httpx)不阻断 —— 子模块仍可单独导入。
    """
    mod = sys.modules.get("a_source_gateway")
    if mod is not None:
        return mod
    try:
        import a_source_gateway as mod  # noqa: F811
        return mod
    except Exception:
        pass

    root = repo_root()
    for pkg_dir, mode in package_candidates(root):
        init = pkg_dir / "__init__.py"
        if not init.is_file():
            continue
        if mode == "insert":
            parent = str(pkg_dir.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            try:
                import a_source_gateway as mod  # noqa: F811
                return mod
            except Exception:
                continue
        # alias: 扁平布局里包目录叫 "src", 只能现场注册一个包对象
        spec = importlib.util.spec_from_file_location(
            "a_source_gateway", init, submodule_search_locations=[str(pkg_dir)])
        if spec is None or spec.loader is None:  # pragma: no cover
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules["a_source_gateway"] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            if not hasattr(mod, "__version__"):
                mod.__version__ = "0.0.0+local"
        return mod
    return None


def install() -> None:
    """给 venv 的 .pth 用: 引导网关包 + 指向仓库 data/。失败静默(不能拖垮解释器)。"""
    try:
        root = repo_root()
        data_dir = root / "data"
        if data_dir.is_dir():
            os.environ.setdefault("A_GATEWAY_DATA_DIR", str(data_dir))
        ensure_gateway_package()
    except Exception:
        pass


# ── 网关运行时(注册表 + 解析器) ─────────────────────────────────────────
class GatewayRuntime:
    """把网关的注册表与解析器装起来, 供 verify/demo 复用。

    优先用网关自己的实现(resolve_source / _render / _project 的语义完全一致);
    网关包装不上时退回 mirror.py 里的镜像实现, 并在报告里标明用的是哪一套。
    """

    def __init__(self, backend: str = "auto", data_dir: str | os.PathLike[str] | None = None):
        self.backend = "none"
        self.registry: dict = {}
        self.resolver = None
        self.reason = ""
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else None

        if backend in ("auto", "gateway"):
            try:
                self._load_gateway()
                self.backend = "gateway"
                return
            except Exception as exc:  # noqa: BLE001
                self.reason = f"{type(exc).__name__}: {exc}"
                if backend == "gateway":
                    raise

        mirror = load_sibling("_asg_mirror")
        self.mirror = mirror
        self.registry = mirror.load_registry(self.data_dir)
        self.backend = "mirror"

    def _load_gateway(self) -> None:
        ensure_gateway_package()
        from a_source_gateway.config import load_config  # noqa: PLC0415
        from a_source_gateway.registry import build_registry  # noqa: PLC0415
        from a_source_gateway.resolver import Resolver  # noqa: PLC0415

        cfg = load_config()
        if self.data_dir is not None:
            # 只改这一个实例的配置, 不动进程的环境变量
            # (改环境变量会污染同进程里的其它代码/测试)
            cfg.data_dir = self.data_dir
        self.cfg = cfg
        self.registry = build_registry(cfg)
        self.resolver = Resolver(self.registry, offline=False, cfg=cfg)

    # ── 源解析 ────────────────────────────────────────────────────────
    def resolve_source(self, key: str) -> str | None:
        if self.backend == "gateway":
            return self.resolver.resolve_source(key)
        return self.mirror.resolve_source(self.sources_by_id(), key)

    def sources_by_id(self) -> dict[str, dict]:
        if self.backend == "gateway":
            return self.resolver.sources
        return {s["id"]: s for s in self.registry.get("sources", [])}

    def get_source(self, key: str) -> dict | None:
        rid = self.resolve_source(key)
        return self.sources_by_id().get(rid) if rid else None

    # ── URL 渲染 + 投影 ───────────────────────────────────────────────
    def render_url(self, ep: dict, params: dict, page_size: int | None = None) -> str:
        if self.backend == "gateway":
            from a_source_gateway.schema import QueryRequest  # noqa: PLC0415
            req = QueryRequest(source="", endpoint=ep.get("id", ""),
                               params=dict(params), limit=page_size or 20)
            return self.resolver._render(ep, req, page_size)
        return self.mirror.render_url(ep, params, page_size)

    def project(self, payload, ep: dict, limit: int = 20, fields: list[str] | None = None):
        if self.backend == "gateway":
            from a_source_gateway.schema import QueryRequest  # noqa: PLC0415
            req = QueryRequest(source="", endpoint=ep.get("id", ""),
                               params={}, fields=list(fields or []), limit=limit)
            return self.resolver._project(payload, ep, req)
        return self.mirror.project(payload, ep, limit, fields or [])

    def parse_body(self, body: str, shape: str, strict: bool = False):
        """把上游正文解析成中间结构。

        `strict=True` 时,上游正文不是 JSON 会抛 `UpstreamNotJSONError`
        (而不是静默兜底成 {"_text": ...})。证据关卡据此把"上游给了错误页"
        与"端点元数据写错了"区分开 —— 前者不该让人去改 item_path。
        """
        if self.backend == "gateway":
            # 别名导入: 否则函数内的 import 会把模块级的 UpstreamNotJSONError
            # 变成局部名, mirror 分支的 raise 就会变成 UnboundLocalError。
            from a_source_gateway.resolver import UpstreamNotJSONError as _RuntimeNotJSON  # noqa: PLC0415
            try:
                return self.resolver._parse(body, shape, strict=strict)
            except _RuntimeNotJSON as exc:
                raise UpstreamNotJSONError(str(exc)) from None
        mirror = self.mirror
        try:
            return mirror.parse_body(body, shape, strict=strict)
        except mirror.NotJSONError as exc:
            raise UpstreamNotJSONError(str(exc)) from None
