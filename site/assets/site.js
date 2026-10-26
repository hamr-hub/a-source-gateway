/* ============================================================================
   a-source-gateway 文档站 —— 站点脚本
   ----------------------------------------------------------------------------
   纯原生 JS,无依赖。负责:
     1. 主题(跟随系统 / 手动亮暗切换,记忆到 localStorage)
     2. 移动端侧栏抽屉
     3. 代码块复制按钮 + 极简语法着色
     4. 右栏目录(自动生成 + 滚动高亮)
     5. 站内搜索(跨页面索引,首次搜索时按需拉取各页并缓存)
   ========================================================================== */
(function () {
  "use strict";

  var THEME_KEY = "asg-docs-theme";
  var SEARCH_INDEX_KEY = "asg-docs-search-index";
  var SEARCH_INDEX_TTL = 1000 * 60 * 60 * 6; // 6 小时

  // 站内页面清单(搜索索引与"上一页/下一页"都用它)
  var PAGES = [
    { url: "index.html", title: "首页", section: "开始" },
    { url: "quickstart.html", title: "快速部署", section: "开始" },
    { url: "api.html", title: "API 参考", section: "接口" },
    { url: "envelope.html", title: "信封与降级", section: "接口" },
    { url: "debug.html", title: "API 调试台", section: "接口" },
    { url: "mcp.html", title: "MCP 集成", section: "接入" },
    { url: "adding-source.html", title: "加源指南", section: "接入" },
  ];

  /* ── 工具 ──────────────────────────────────────────────────────────── */

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments;
      var self = this;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(self, args);
      }, wait);
    };
  }

  /* ── 1. 主题 ────────────────────────────────────────────────────────── */

  function prefersDark() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function applyTheme(theme) {
    var root = document.documentElement;
    // "auto" 跟随系统: 由 matchMedia 决定, 而不是永远浅色。
    var isDark = theme === "dark" || (theme !== "light" && prefersDark());
    root.classList.toggle("dark", isDark);
    root.classList.toggle("light", theme === "light");
    var btn = $("#theme-toggle");
    if (btn) {
      btn.setAttribute("aria-label", isDark ? "切换到浅色主题" : "切换到深色主题");
      btn.setAttribute("title", isDark ? "切换到浅色主题" : "切换到深色主题");
      btn.innerHTML = isDark ? sunIcon() : moonIcon();
    }
  }

  function sunIcon() {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="4"/>' +
      '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2' +
      'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    );
  }

  function moonIcon() {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/></svg>'
    );
  }

  function initTheme() {
    var saved = null;
    try {
      saved = localStorage.getItem(THEME_KEY);
    } catch (e) {
      saved = null;
    }
    applyTheme(saved || "auto");

    var btn = $("#theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var current = document.documentElement.classList.contains("dark") ? "dark" : "light";
      var next = current === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        /* 隐私模式下写不了,忽略 */
      }
      applyTheme(next);
    });

    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function () {
        var savedNow = null;
        try {
          savedNow = localStorage.getItem(THEME_KEY);
        } catch (e) {
          savedNow = null;
        }
        if (!savedNow) applyTheme("auto");
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  }

  /* ── 2. 移动端抽屉 ─────────────────────────────────────────────────── */

  function initDrawer() {
    var toggle = $("#nav-toggle");
    var sidebar = $("#sidebar");
    if (!toggle || !sidebar) return;

    var backdrop = document.createElement("div");
    backdrop.className = "sidebar-backdrop";
    backdrop.hidden = true;
    document.body.appendChild(backdrop);

    function open() {
      sidebar.classList.add("is-open");
      backdrop.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
    }

    function close() {
      sidebar.classList.remove("is-open");
      backdrop.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
    }

    toggle.addEventListener("click", function () {
      if (sidebar.classList.contains("is-open")) close();
      else open();
    });
    backdrop.addEventListener("click", close);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && sidebar.classList.contains("is-open")) close();
    });
    $$(".nav-link", sidebar).forEach(function (a) {
      a.addEventListener("click", function () {
        if (window.matchMedia("(max-width: 900px)").matches) close();
      });
    });
  }

  /* ── 3. 代码块:复制 + 着色 ─────────────────────────────────────────── */

  var LANG_SPECS = {
    bash: { comment: "#", strings: ['"', "'"], keywords: ["curl", "python3", "make", "git", "docker", "docker-compose", "kubectl", "sudo", "export", "cd", "mkdir", "echo", "cat", "pip3", "pip", "npm", "node", "jq", "cp", "rm", "go", "pytest", "ruff", "mypy", "systemctl", "journalctl", "chmod"] },
    shell: { comment: "#", strings: ['"', "'"], keywords: [] },
    console: { comment: "#", strings: ['"', "'"], keywords: [] },
    json: { json: true },
    python: { comment: "#", strings: ['"', "'"], keywords: ["def", "class", "import", "from", "as", "if", "elif", "else", "for", "while", "return", "with", "try", "except", "finally", "raise", "None", "True", "False", "self", "print", "async", "await", "lambda", "yield", "not", "and", "or", "in", "is", "pass", "break", "continue"] },
  };

  function highlight(code, lang) {
    var key = String(lang || "").toLowerCase();
    var spec = LANG_SPECS[key];
    if (!spec) return escapeHtml(code);

    if (spec.json) {
      return code.replace(
        /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"\s*:)|("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*")|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
        function (m, keyPart, strPart, lit, num) {
          if (keyPart) return '<span class="tok-k">' + escapeHtml(keyPart) + "</span>";
          if (strPart) return '<span class="tok-s">' + escapeHtml(strPart) + "</span>";
          if (lit) return '<span class="tok-k">' + escapeHtml(lit) + "</span>";
          if (num) return '<span class="tok-n">' + escapeHtml(num) + "</span>";
          return escapeHtml(m);
        }
      );
    }

    var keywords = spec.keywords || [];
    // 不使用 lookbehind —— Safari 16.4 以前构造就会抛 SyntaxError,
    // 而高亮失败会连带后面的目录/搜索/翻页一起废掉。
    var kwRe = keywords.length ? new RegExp("(" + keywords.join("|") + ")", "g") : null;
    var wordChar = /[\w-]/;
    var out = "";
    var i = 0;
    var n = code.length;
    while (i < n) {
      var ch = code[i];

      // 注释
      if (spec.comment && code.startsWith(spec.comment, i)) {
        var end = code.indexOf("\n", i);
        if (end === -1) end = n;
        out += '<span class="tok-c">' + escapeHtml(code.slice(i, end)) + "</span>";
        i = end;
        continue;
      }

      // 字符串
      if (spec.strings && spec.strings.indexOf(ch) !== -1) {
        var j = i + 1;
        while (j < n) {
          if (code[j] === "\\") {
            j += 2;
            continue;
          }
          if (code[j] === ch) {
            j++;
            break;
          }
          if (code[j] === "\n") break;
          j++;
        }
        out += '<span class="tok-s">' + escapeHtml(code.slice(i, j)) + "</span>";
        i = j;
        continue;
      }

      // 关键字(只在词边界处命中)
      if (kwRe) {
        kwRe.lastIndex = i;
        var m = kwRe.exec(code);
        if (m && m.index === i) {
          var before = i > 0 ? code.charAt(i - 1) : "";
          var after = code.charAt(i + m[0].length) || "";
          var beforeOk = !before || !wordChar.test(before);
          var afterOk = !after || !wordChar.test(after);
          if (beforeOk && afterOk) {
            out += '<span class="tok-k">' + escapeHtml(m[0]) + "</span>";
            i += m[0].length;
            continue;
          }
        }
      }

      out += escapeHtml(ch);
      i++;
    }
    return out;
  }

  function initCodeBlocks() {
    $$(".code-block").forEach(function (block) {
      var head = $(".code-head", block);
      var pre = $("pre", block);
      if (!pre) return;
      var codeEl = $("code", pre) || pre;
      var raw = codeEl.textContent.replace(/\n$/, "");

      var lang = "";
      if (head) {
        var langEl = $(".code-lang", head);
        lang = langEl ? langEl.textContent.trim() : "";
      }
      if (!lang) lang = codeEl.className.replace(/^language-/, "").trim();

      codeEl.innerHTML = highlight(raw, lang);

      if (head && !$(".copy-btn", head)) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "copy-btn";
        btn.innerHTML =
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
          'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
          '<rect x="9" y="9" width="12" height="12" rx="2"/>' +
          '<path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg><span>复制</span>';
        btn.addEventListener("click", function () {
          copyText(raw).then(function () {
            btn.classList.add("is-done");
            var span = $("span", btn);
            if (span) span.textContent = "已复制";
            setTimeout(function () {
              btn.classList.remove("is-done");
              if (span) span.textContent = "复制";
            }, 1600);
          });
        });
        head.appendChild(btn);
      }
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch (e) {
        /* 忽略:仍 resolve,用户可手动选 */
      }
      document.body.removeChild(ta);
      resolve();
    });
  }

  /* ── 4. 标题锚点 + 目录 ────────────────────────────────────────────── */

  function slugify(text) {
    return text
      .toLowerCase()
      .replace(/[`"'*_[\]()]/g, "")
      .replace(/[^\w一-龥]+/g, "-")
      .replace(/^-+|-+$/g, "") || "section";
  }

  function initHeadings() {
    var main = $("#main");
    if (!main) return;

    // used 只装**标题**的 id —— 不用 Object.prototype 上的键名。
    // (scripts/check_docs.py 复刻的正是这套语义, 两边必须一致。)
    var used = Object.create(null);
    $$("h2, h3", main).forEach(function (h) {
      if (!h.id) {
        var base = slugify(h.textContent.replace(/\s+/g, " ").trim());
        var id = base;
        var i = 2;
        while (used[id]) {
          id = base + "-" + i++;
        }
        used[id] = true;
        h.id = id;
      } else {
        used[h.id] = true;
      }
      var anchor = document.createElement("a");
      anchor.className = "anchor";
      anchor.href = "#" + h.id;
      anchor.setAttribute("aria-hidden", "true");
      anchor.textContent = "#";
      h.appendChild(anchor);
    });
  }

  function initToc() {
    var toc = $("#toc");
    var main = $("#main");
    if (!toc || !main) return;

    var heads = $$("h2, h3", main).filter(function (h) {
      return h.id;
    });
    if (!heads.length) {
      toc.remove();
      return;
    }

    var title = document.createElement("div");
    title.className = "toc-title";
    title.textContent = "本页目录";
    toc.appendChild(title);

    var links = [];
    heads.forEach(function (h) {
      var a = document.createElement("a");
      a.href = "#" + h.id;
      a.textContent = h.textContent.replace(/#$/, "").trim();
      if (h.tagName === "H3") a.className = "toc-l3";
      toc.appendChild(a);
      links.push({ el: h, link: a });
    });

    var observer = null;
    if ("IntersectionObserver" in window) {
      observer = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            links.forEach(function (l) {
              l.link.classList.toggle("is-active", l.el === entry.target);
            });
          });
        },
        { rootMargin: "-70px 0px -75% 0px", threshold: 0 }
      );
      links.forEach(function (l) {
        observer.observe(l.el);
      });
    }

    // 没有 IntersectionObserver 时的兜底:滚动位置计算
    if (!observer) {
      var onScroll = function () {
        var best = null;
        links.forEach(function (l) {
          if (l.el.getBoundingClientRect().top <= 100) best = l;
        });
        links.forEach(function (l) {
          l.link.classList.toggle("is-active", best && l.el === best.el);
        });
      };
      window.addEventListener("scroll", onScroll, { passive: true });
      onScroll();
    }
  }

  /* ── 5. 站内搜索 ────────────────────────────────────────────────────── */

  var searchIndex = null;
  var searchLoading = null;

  function loadIndex() {
    if (searchIndex) return Promise.resolve(searchIndex);
    if (searchLoading) return searchLoading;

    var cached = null;
    try {
      var raw = sessionStorage.getItem(SEARCH_INDEX_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (parsed && parsed.ts && Date.now() - parsed.ts < SEARCH_INDEX_TTL) {
          searchIndex = parsed.items;
          return Promise.resolve(searchIndex);
        }
      }
    } catch (e) {
      cached = null;
    }

    searchLoading = Promise.all(
      PAGES.map(function (p) {
        return fetch(p.url)
          .then(function (r) {
            return r.ok ? r.text() : "";
          })
          .catch(function () {
            return "";
          })
          .then(function (html) {
            if (!html) return [];
            var doc = new DOMParser().parseFromString(html, "text/html");
            var main = doc.querySelector("#main") || doc.body;
            var items = [];
            // 标题不是 #main 的直接子节点(它们在 .doc 里), 所以要向下取,
            // 再用"到下一个标题为止"的区间收集正文。
            var heads = main.querySelectorAll("h1, h2, h3");
            Array.prototype.forEach.call(heads, function (h, i) {
              var text = "";
              var node = h.nextElementSibling;
              while (node && !/^H[123]$/.test(node.tagName)) {
                text += " " + node.textContent.replace(/\s+/g, " ").trim();
                node = node.nextElementSibling;
              }
              items.push({
                page: p.url,
                pageTitle: p.title,
                section: p.section,
                heading: h.textContent.replace(/\s+/g, " ").trim(),
                anchor: h.id || "",
                text: text,
              });
            });
            return items;
          });
      })
    ).then(function (groups) {
      var items = [];
      groups.forEach(function (g) {
        items = items.concat(g);
      });
      searchIndex = items;
      try {
        sessionStorage.setItem(
          SEARCH_INDEX_KEY,
          JSON.stringify({ ts: Date.now(), items: items })
        );
      } catch (e) {
        /* 配额不足则不缓存 */
      }
      searchLoading = null;
      return items;
    });

    return searchLoading;
  }

  function initSearch() {
    var input = $("#search-input");
    var panel = $("#search-results");
    if (!input || !panel) return;

    var activeIndex = -1;
    var hits = [];

    function close() {
      panel.hidden = true;
      panel.innerHTML = "";
      activeIndex = -1;
      hits = [];
    }

    function render(list, q) {
      panel.innerHTML = "";
      hits = list;
      activeIndex = -1;
      if (!list.length) {
        var empty = document.createElement("div");
        empty.className = "search-empty";
        empty.textContent = "没有匹配「" + q + "」的内容";
        panel.appendChild(empty);
        panel.hidden = false;
        return;
      }
      list.forEach(function (hit) {
        var a = document.createElement("a");
        a.className = "search-hit";
        a.href = hit.page + (hit.anchor ? "#" + hit.anchor : "");
        var t = document.createElement("div");
        t.className = "search-hit-title";
        t.textContent = hit.heading || hit.pageTitle;
        var path = document.createElement("div");
        path.className = "search-hit-path";
        path.textContent = hit.pageTitle + " › " + hit.page;
        a.appendChild(t);
        a.appendChild(path);
        if (hit.text) {
          var sn = document.createElement("div");
          sn.className = "search-hit-snippet";
          sn.textContent = hit.text.slice(0, 180);
          a.appendChild(sn);
        }
        panel.appendChild(a);
      });
      panel.hidden = false;
    }

    function score(hit, q) {
      var s = 0;
      var heading = (hit.heading || "").toLowerCase();
      var text = (hit.text || "").toLowerCase();
      var page = (hit.pageTitle || "").toLowerCase();
      if (heading.indexOf(q) !== -1) s += 100;
      if (page.indexOf(q) !== -1) s += 60;
      if (heading.indexOf(q) === 0) s += 40;
      if (text.indexOf(q) !== -1) s += 20;
      var count = text.split(q).length - 1;
      s += Math.min(count, 10) * 2;
      return s;
    }

    var run = debounce(function () {
      var q = input.value.trim().toLowerCase();
      if (q.length < 1) {
        close();
        return;
      }
      loadIndex().then(function (items) {
        var list = items
          .map(function (it) {
            return { it: it, s: score(it, q) };
          })
          .filter(function (x) {
            return x.s > 20;
          })
          .sort(function (a, b) {
            return b.s - a.s;
          })
          .slice(0, 12)
          .map(function (x) {
            return x.it;
          });
        render(list, q);
      });
    }, 160);

    input.addEventListener("input", run);
    input.addEventListener("focus", function () {
      if (input.value.trim()) run();
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        close();
        input.blur();
        return;
      }
      if (e.key === "Enter") {
        var first = $(".search-hit", panel);
        if (first) {
          window.location.href = first.getAttribute("href");
        }
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      var items = $$(".search-hit", panel);
      if (!items.length) return;
      e.preventDefault();
      activeIndex += e.key === "ArrowDown" ? 1 : -1;
      if (activeIndex < 0) activeIndex = items.length - 1;
      if (activeIndex >= items.length) activeIndex = 0;
      items.forEach(function (el, i) {
        el.classList.toggle("is-active", i === activeIndex);
      });
      items[activeIndex].scrollIntoView({ block: "nearest" });
    });

    document.addEventListener("click", function (e) {
      if (!panel.contains(e.target) && e.target !== input) close();
    });

    document.addEventListener("keydown", function (e) {
      var tag = (e.target && e.target.tagName) || "";
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag);
      if ((e.key === "/" && !typing) || ((e.metaKey || e.ctrlKey) && e.key === "k")) {
        e.preventDefault();
        input.focus();
        input.select();
      }
    });
  }

  /* ── 6. 目录页高亮 + 上/下一页 ──────────────────────────────────────── */

  function initNavState() {
    var here = location.pathname.split("/").pop() || "index.html";
    $$(".nav-link").forEach(function (a) {
      var href = a.getAttribute("href") || "";
      if (href === here) {
        a.classList.add("is-active");
        a.setAttribute("aria-current", "page");
      }
    });

    var idx = PAGES.findIndex(function (p) {
      return p.url === here;
    });
    var pager = $("#pager");
    if (!pager || idx === -1) return;
    var prev = PAGES[idx - 1];
    var next = PAGES[idx + 1];
    if (!prev && !next) {
      pager.remove();
      return;
    }
    if (prev) {
      var a1 = document.createElement("a");
      a1.className = "prev";
      a1.href = prev.url;
      a1.innerHTML =
        '<div class="pager-dir">← 上一页</div><div class="pager-title"></div>';
      $(".pager-title", a1).textContent = prev.title;
      pager.appendChild(a1);
    } else {
      var sp = document.createElement("span");
      sp.className = "prev";
      pager.appendChild(sp);
    }
    if (next) {
      var a2 = document.createElement("a");
      a2.className = "next";
      a2.href = next.url;
      a2.innerHTML =
        '<div class="pager-dir">下一页 →</div><div class="pager-title"></div>';
      $(".pager-title", a2).textContent = next.title;
      pager.appendChild(a2);
    }
  }

  /* ── 启动 ──────────────────────────────────────────────────────────── */

  function init() {
    // 每一步单独兜底: 一处出错不该把后面的目录/搜索/翻页一起带崩。
    var steps = [
      ["主题", initTheme],
      ["抽屉", initDrawer],
      ["标题锚点", initHeadings],
      ["代码块", initCodeBlocks],
      ["本页目录", initToc],
      ["站内搜索", initSearch],
      ["导航状态", initNavState],
    ];
    steps.forEach(function (step) {
      try {
        step[1]();
      } catch (err) {
        if (window.console && window.console.error) {
          window.console.error("[docs] " + step[0] + " 初始化失败:", err);
        }
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.ASG_DOCS = {
    pages: PAGES,
    highlight: highlight,
    copyText: copyText,
    escapeHtml: escapeHtml,
  };
})();
