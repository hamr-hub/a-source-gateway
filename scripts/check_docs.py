"""
check_docs.py - 文档站自检: 结构完整、链接可达、页面互链齐全。

为什么要有它:
    文档站是"上手第一站", 一个坏链接或一个缺了 `<main id="main">` 的页面,
    会让读者在第一步就掉出去 —— 而这种错误在浏览器里表现为"页面看着正常, 只是搜索不好用"。
    把它变成**可执行的门禁**, 比在 PR 里靠人眼扫一遍可靠。

检查项:
    1. 每页的外壳完整: #main / .doc / #sidebar / #pager / #toc / #search-input /
       #theme-toggle / assets/site.js
    2. 内部链接可达: 页面链接、锚点(#id)、静态资源(assets/*)
    3. 侧栏导航在每页都列全了其它页面(不会漏链)
    4. site.js 的 PAGES 清单覆盖了 site/ 下的每个 .html(搜索索引不会漏页)
    5. 代码块结构完整(.code-block > .code-head + pre > code)
    6. 没有残留的占位符(TODO(...)、FIXME、lorem ipsum)

退出码: 0 全部通过 / 1 有问题
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SITE_DIR = REPO_ROOT / "site"

# 每页必须存在的元素(按出现顺序检查 id 或 class)
REQUIRED_MARKERS: list[tuple[str, str]] = [
    ("id", "main"),
    ("id", "sidebar"),
    ("id", "toc"),
    ("id", "pager"),
    ("id", "search-input"),
    ("id", "search-results"),
    ("id", "theme-toggle"),
    ("class", "doc"),
    ("class", "layout"),
    ("class", "sidebar"),
]

# 必须出现的资源引用
REQUIRED_SCRIPTS = ("assets/site.js",)
REQUIRED_STYLES = ("assets/base.css",)

PLACEHOLDER_PATTERNS = [
    # 只报"真的没填"的占位符: scaffold 生成的形态是 TODO(待填:xxx) / TODO(待补:xxx)。
    # 文档里引用约定本身时写的是 TODO(...), 那不是残留。
    (re.compile(r"TODO\((?!\.\.\.\))"), "残留 TODO 占位符(应换成真实值或明确的\"未实测\")"),
    (re.compile(r"\bFIXME\b"), "残留 FIXME"),
    (re.compile(r"lorem ipsum", re.IGNORECASE), "残留 lorem ipsum"),
]

# 这些前缀的链接不是站点内的静态资源(调试台的同源代理路径等), 不做文件存在性检查
NON_STATIC_PREFIXES = ("/__gateway",)


class PageParser(HTMLParser):
    """收集一页里的 id、class、链接、资源引用与代码块结构。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.classes: set[str] = set()
        self.links: list[str] = []       # <a href>
        self.assets: list[str] = []      # <script src> / <link href> / <img src>
        self.code_blocks = 0
        self.code_blocks_ok = 0
        self.headings: list[str] = []    # h2/h3 的文本(按出现顺序)
        self.heading_ids: list[str] = [] # 与 headings 一一对应的 id(可能为空)
        self.sidebar_links: list[str] = []  # 侧栏 <aside> 段内的链接
        self._in_sidebar = False
        self._heading: list[str] | None = None
        # div 栈: 每个元素是 (是否 code-block, 是否有 code-head, 是否有 pre, 是否有 code)
        self._div_stack: list[tuple[bool, bool, bool, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if "id" in attr:
            self.ids.add(attr["id"])
        for cls in attr.get("class", "").split():
            self.classes.add(cls)

        if tag == "a" and "href" in attr:
            self.links.append(attr["href"])
            if self._in_sidebar:
                self.sidebar_links.append(attr["href"])
        if tag in ("script", "img") and "src" in attr:
            self.assets.append(attr["src"])
        if tag == "link" and "href" in attr:
            self.assets.append(attr["href"])

        if tag == "aside" and "sidebar" in attr.get("class", "").split():
            self._in_sidebar = True

        if tag in ("h2", "h3"):
            self._heading = []
            self.heading_ids.append(attr.get("id", ""))

        if tag == "div":
            is_block = "code-block" in attr.get("class", "").split()
            if is_block:
                self.code_blocks += 1
            if self._div_stack:
                # 在当前 div 内部, 更新最顶层 code-block 的标记
                top = list(self._div_stack[-1])
                if "code-head" in attr.get("class", "").split():
                    top[1] = True
                self._div_stack[-1] = (top[0], top[1], top[2], top[3])
            self._div_stack.append((is_block, False, False, False))
            return

        if self._div_stack:
            top = list(self._div_stack[-1])
            if tag == "pre":
                top[2] = True
            elif tag == "code":
                top[3] = True
            self._div_stack[-1] = (top[0], top[1], top[2], top[3])

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h2", "h3") and self._heading is not None:
            self.headings.append("".join(self._heading))
            self._heading = None
        if tag == "aside":
            self._in_sidebar = False
        if tag != "div" or not self._div_stack:
            return
        frame = self._div_stack.pop()
        is_block, has_head, has_pre, has_code = frame
        if is_block:
            if has_head and has_pre and has_code:
                self.code_blocks_ok += 1
            return
        # 关闭的是内层 div: 把它的标记并回外层
        if self._div_stack:
            top = list(self._div_stack[-1])
            self._div_stack[-1] = (top[0], top[1] or has_head, top[2] or has_pre, top[3] or has_code)

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._heading.append(data)


def parse_page(path: Path) -> PageParser:
    parser = PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def is_external(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.scheme) or url.startswith("//")


def is_non_static(url: str) -> bool:
    """不是站点内的静态资源(同源代理路径等)。"""
    return url.startswith(NON_STATIC_PREFIXES)


def slugify(text: str) -> str:
    """复刻 site.js 的 slugify 规则(两处必须一致, 否则锚点对不上)。"""
    s = re.sub(r"[`\"'*_\[\]()]", "", re.sub(r"\s+", " ", text).strip().lower())
    s = re.sub(r"[^\w一-龥]+", "-", s, flags=re.ASCII)
    s = s.strip("-")
    return s or "section"


def runtime_anchors(parser: PageParser) -> set[str]:
    """页面在浏览器里实际会有的 id 集合。

    标题的锚点是 site.js 在运行时按标题文本生成的, 静态 HTML 里并没有 id。
    所以校验跨页锚点链接时, 必须**复刻同一套生成规则**, 否则会误报。

    复刻要点(site.js initHeadings): `used` 里只装**标题**的 id ——
    页面里其它元素的 id 不参与去重。搞成"页面全部 id"会让这里多出
    "#xxx-2" 这种浏览器里根本不存在的锚点, 坏链接就漏过去了。
    """
    ids = set(parser.ids)
    used: set[str] = set()
    for heading_id, text in zip(parser.heading_ids, parser.headings, strict=False):
        if heading_id:
            used.add(heading_id)
            continue
        base = slugify(text)
        anchor = base
        counter = 2
        while anchor in used:
            anchor = f"{base}-{counter}"
            counter += 1
        used.add(anchor)
        ids.add(anchor)
    return ids


def check_structure(path: Path, parser: PageParser, problems: list[str]) -> None:
    rel = path.name
    for kind, marker in REQUIRED_MARKERS:
        found = marker in parser.ids if kind == "id" else marker in parser.classes
        if not found:
            problems.append(f"{rel}: 缺少外壳元素 <{kind}={marker}>")

    text = path.read_text(encoding="utf-8")
    for script in REQUIRED_SCRIPTS:
        if script not in text:
            problems.append(f"{rel}: 未引用 {script}")
    for style in REQUIRED_STYLES:
        if style not in text:
            problems.append(f"{rel}: 未引用 {style}")

    if parser.code_blocks != parser.code_blocks_ok:
        problems.append(
            f"{rel}: 有 {parser.code_blocks - parser.code_blocks_ok} 个代码块结构不完整"
            "(缺 code-head 或 pre/code)")

    for pattern, label in PLACEHOLDER_PATTERNS:
        if pattern.search(text):
            problems.append(f"{rel}: {label}")


def check_links(path: Path, parser: PageParser, pages: dict[str, PageParser],
                anchors: dict[str, set[str]], site_dir: Path, problems: list[str]) -> None:
    rel = path.name
    for href in parser.links:
        if not href or is_external(href) or is_non_static(href):
            continue
        if href.startswith("#"):
            anchor = href[1:]
            if anchor and anchor not in anchors.get(rel, set()):
                problems.append(f"{rel}: 锚点链接指向不存在的 id → {href}")
            continue
        parts = urlsplit(href)
        target = parts.path
        if not target:
            continue
        if target.endswith(".html"):
            if target not in pages:
                problems.append(f"{rel}: 链接到不存在的页面 → {href}")
                continue
            anchor = parts.fragment
            if anchor and anchor not in anchors.get(target, set()):
                problems.append(f"{rel}: 链接到 {target} 里不存在的锚点 → {href}")
        else:
            if not (site_dir / target).exists():
                problems.append(f"{rel}: 链接到不存在的静态资源 → {href}")

    for src in parser.assets:
        if not src or is_external(src) or is_non_static(src):
            continue
        if not (site_dir / src).exists():
            problems.append(f"{rel}: 引用了不存在的资源 → {src}")


def check_nav_and_index(paths: list[Path], pages: dict[str, PageParser],
                        site_dir: Path, problems: list[str]) -> None:
    """侧栏导航要列全其它页面; site.js 的 PAGES 清单也要覆盖所有页面。"""
    site_js = site_dir / "assets" / "site.js"
    js_text = site_js.read_text(encoding="utf-8") if site_js.exists() else ""

    for path in paths:
        rel = path.name
        sidebar_links = set(pages[rel].sidebar_links)
        for other in paths:
            if other.name == rel:
                continue
            # 只看**侧栏段内**的链接: 正文里出现同名链接不算数 ——
            # 否则侧栏漏了一页也会被判通过。
            if other.name not in sidebar_links:
                problems.append(f"{rel}: 侧栏缺少指向 {other.name} 的导航链接")
        if js_text and f'"{rel}"' not in js_text:
            problems.append(f"assets/site.js: PAGES 清单缺少 {rel}(搜索会漏掉这一页)")


def registry_counts() -> dict[str, int] | None:
    """从注册表取源数 / 端点数 / 族数。

    拿不到就返回 None —— 校验跳过, 不算失败(免得在没装包的环境里卡住 CI)。
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "registry.json"
            proc = subprocess.run(
                [sys.executable, "-m", "a_source_gateway.cli", "build-registry", "--out", str(out)],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0 or not out.exists():
                return None
            reg = json.loads(out.read_text(encoding="utf-8"))
    except Exception:
        return None

    sources = reg.get("sources", [])
    return {
        "sources": int(reg.get("count", len(sources))),
        "endpoints": sum(len(s.get("endpoints", [])) for s in sources),
        "families": len(reg.get("families", [])),
    }


def check_stat_numbers(site_dir: Path, problems: list[str], strict: bool = False) -> None:
    """首页统计条上的数字必须与注册表一致。

    源是经常加的 —— 文档里的数字如果不跟注册表挂钩, 就会悄悄变成"上个季度的数字"。
    这里把它变成机器可判定的: 对不上就报错, 改文档的人一眼看到该填多少。

    strict=True 时, 拿不到注册表也算失败 —— "降级不算通过"。
    """
    index = site_dir / "index.html"
    if not index.exists():
        return
    text = index.read_text(encoding="utf-8")
    stats = dict(re.findall(r'data-stat="(\w+)"[^>]*>(\d+)', text))
    if not stats:
        problems.append("index.html: 统计条缺少 data-stat 标记(无法与注册表核对)")
        return

    counts = registry_counts()
    if counts is None:
        msg = "拿不到注册表数字, 无法核对统计条"
        if strict:
            problems.append(f"index.html: {msg}(strict 模式: 降级不算通过)")
        else:
            print(f"  (提示: {msg}, 已跳过统计条核对)")
        return

    for key, want in counts.items():
        got = stats.get(key)
        if got is None:
            problems.append(f'index.html: 统计条缺少 data-stat="{key}"')
        elif int(got) != want:
            problems.append(
                f"index.html: 统计条 {key} 写的是 {got}, 注册表实际是 {want} —— 请同步文档")



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="check_docs.py",
        description="校验文档站: 外壳完整、内部链接可达、导航齐全、无占位符。")
    ap.add_argument("--site-dir", default=str(DEFAULT_SITE_DIR), help="站点目录(默认 site/)")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式: 拿不到注册表数字也算失败(降级不算通过)")
    args = ap.parse_args(argv)

    site_dir = Path(args.site_dir).resolve()
    if not site_dir.is_dir():
        print(f"站点目录不存在: {site_dir}", file=sys.stderr)
        return 1

    paths = sorted(site_dir.glob("*.html"))
    if not paths:
        print(f"{site_dir} 下没有 .html 页面", file=sys.stderr)
        return 1

    pages: dict[str, PageParser] = {}
    anchors: dict[str, set[str]] = {}
    problems: list[str] = []

    for path in paths:
        parser = parse_page(path)
        pages[path.name] = parser
        anchors[path.name] = runtime_anchors(parser)
        check_structure(path, parser, problems)

    for path in paths:
        check_links(path, pages[path.name], pages, anchors, site_dir, problems)

    check_nav_and_index(paths, pages, site_dir, problems)
    check_stat_numbers(site_dir, problems, strict=args.strict)

    if problems:
        print(f"文档站校验失败, {len(problems)} 个问题:")
        for p in problems:
            print("  -", p)
        return 1

    total_links = sum(len(p.links) for p in pages.values())
    total_blocks = sum(p.code_blocks for p in pages.values())
    print(f"文档站校验通过: {len(paths)} 页 / {total_links} 个链接 / {total_blocks} 个代码块")
    return 0


if __name__ == "__main__":
    sys.exit(main())
