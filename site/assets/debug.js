/* ============================================================================
   a-source-gateway 文档站 —— API 调试台
   ----------------------------------------------------------------------------
   纯原生 JS,无依赖。干三件事:
     1. 探测网关状态;加载源列表(按 family 分组, 支持关键词过滤)
     2. 选源 → 选端点 → 用 example/default 生成参数模板
     3. 发 POST /v1/query, 渲染统一信封, 生成等价 curl

   诚实原则(这条比"看起来能用"重要):
     · 网关不可达 → 原样显示代理返回的 detail / hint
     · 跨域被浏览器拦截 → 明说"网关有响应但被浏览器拦了", 不假装成功
     · 降级 → 明说这是降级, 给出"等几秒再点一次"的重试

   不往代码、URL 或 localStorage 里写任何凭据。
   ========================================================================== */
(function () {
  "use strict";

  var DOCS = window.ASG_DOCS || {};

  // site.js 已经提供 escapeHtml / copyText —— 直接用, 不重复实现
  var escapeHtml =
    DOCS.escapeHtml ||
    function (s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    };
  var copyText = DOCS.copyText || function () {
    return Promise.resolve();
  };

  function attr(s) {
    return escapeHtml(s).replace(/"/g, "&quot;");
  }

  function $(id) {
    return document.getElementById(id);
  }

  /* ── 状态 ──────────────────────────────────────────────────────────── */

  var state = {
    prefix: "/__gateway",
    sources: [],
    sourceTotal: 0,
    source: null,
    endpoints: [],
    endpoint: null,
    lastBody: null,
    inflight: false
  };

  var el = {};

  /* ── URL 拼接 ──────────────────────────────────────────────────────── */

  function api(path) {
    var p = String(state.prefix || "").replace(/\/+$/, "");
    return p + path;
  }

  function isSameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (e) {
      return false;
    }
  }

  /* ── 小工具 ────────────────────────────────────────────────────────── */

  function badge(text, kind) {
    return '<span class="badge' + (kind ? " " + kind : "") + '">' + escapeHtml(text) + "</span>";
  }

  function setBadge(node, text, kind) {
    if (!node) return;
    node.innerHTML = badge(text, kind);
  }

  function fmtJSON(v) {
    try {
      return JSON.stringify(v, null, 2);
    } catch (e) {
      return String(v);
    }
  }

  function newTraceId() {
    var rand = Math.random().toString(36).slice(2, 10);
    return "dbg-" + Date.now().toString(36) + "-" + rand;
  }

  /* ── 请求封装 + 失败分类 ───────────────────────────────────────────── */

  // fetch 失败时区分三种情况:
  //   unreachable —— 同源前缀都连不上(文档站/代理自己没了)
  //   cors       —— 跨源, 但 no-cors 探测能通 → 网关有响应, 被浏览器拦了
  //   network    —— 跨源, no-cors 探测也失败 → 网关真的连不上
  function classifyFetchError(err, url) {
    var msg = err && err.message ? err.message : "网络错误";
    if (isSameOrigin(url)) {
      return Promise.resolve({
        kind: "unreachable",
        title: "同源请求没送达",
        message:
          "没能连到 " + url + " —— " + msg + "。文档站(及其 /__gateway 代理)本身可能已经停了, " +
          "确认 python3 scripts/serve_docs.py 还在跑, 然后刷新本页。"
      });
    }
    return fetch(api("/livez"), { method: "GET", mode: "no-cors", cache: "no-store" })
      .then(function () {
        return {
          kind: "cors",
          title: "网关有响应, 但被浏览器跨域策略拦下了",
          message:
            "网关确实响应了(no-cors 探测成功), 但浏览器拒绝把跨域响应交给页面 —— " +
            "网关没有开 CORS 响应头。把地址前缀改回 /__gateway(同源代理), " +
            "或者让文档站页面本身与网关同源, 才能拿到响应。"
        };
      })
      .catch(function () {
        return {
          kind: "network",
          title: "连不上网关",
          message:
            "网关没有响应 —— " + msg + "。确认网关在跑(make up / make gateway-run), " +
            "并检查地址前缀里的主机与端口。"
        };
      });
  }

  function request(path, options) {
    var url = api(path);
    return fetch(url, options).then(
      function (res) {
        return res.text().then(function (text) {
          var json = null;
          try {
            json = text ? JSON.parse(text) : null;
          } catch (e) {
            json = null;
          }
          return { status: res.status, ok: res.ok, text: text, json: json, url: url };
        });
      },
      function (err) {
        return classifyFetchError(err, url).then(function (info) {
          var e = new Error(info.message);
          e.kind = info.kind;
          e.info = info;
          throw e;
        });
      }
    );
  }

  /* ── 渲染:消息区 ──────────────────────────────────────────────────── */

  function clearMsg() {
    if (el.msg) el.msg.innerHTML = "";
  }

  function msgBlock(kind, title, bodyHTML) {
    return (
      '<div class="callout ' + kind + '">' +
      '<div class="callout-title"><span class="callout-icon">' +
      (kind === "danger" ? "!" : kind === "warn" ? "!" : kind === "tip" ? "✓" : "i") +
      "</span>" + escapeHtml(title) + "</div>" +
      '<div class="callout-body">' + bodyHTML + "</div></div>"
    );
  }

  function showErrorBlock(title, lines, kind) {
    if (!el.msg) return;
    var html = "";
    lines.forEach(function (line) {
      html += "<p>" + escapeHtml(line) + "</p>";
    });
    el.msg.innerHTML = msgBlock(kind || "danger", title, html);
  }

  /* ── 渲染:源列表 ──────────────────────────────────────────────────── */

  function matchSource(s, q) {
    if (!q) return true;
    var hay = [s.id, s.name, s.org, s.country, s.summary].join(" ").toLowerCase();
    return hay.indexOf(q) !== -1;
  }

  function renderSources() {
    if (!el.srcList) return;
    var q = (el.srcFilter && el.srcFilter.value ? el.srcFilter.value : "").trim().toLowerCase();
    var matched = state.sources.filter(function (s) {
      return matchSource(s, q);
    });

    var order = [];
    var buckets = {};
    matched.forEach(function (s) {
      var fam = s.family || "未分类";
      if (!buckets[fam]) {
        buckets[fam] = [];
        order.push(fam);
      }
      buckets[fam].push(s);
    });

    el.srcList.innerHTML = "";
    if (!matched.length) {
      el.srcList.innerHTML =
        '<div class="endpoint-item" style="cursor:default">' +
        escapeHtml(state.sources.length ? "没有匹配的源,换个关键词" : "没有源 —— 网关可能返回了空列表") +
        "</div>";
    } else {
      order.forEach(function (fam) {
        var head = document.createElement("div");
        head.className = "group-title";
        head.textContent = fam + " (" + buckets[fam].length + ")";
        el.srcList.appendChild(head);

        buckets[fam].forEach(function (s) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "endpoint-item";
          if (state.source && state.source.id === s.id) btn.classList.add("is-selected");
          btn.setAttribute("data-src", s.id);
          var id = document.createElement("span");
          id.className = "ep-id";
          id.style.display = "block";
          id.textContent = s.name || s.id;
          var meta = document.createElement("span");
          meta.className = "ep-meta";
          meta.style.display = "block";
          meta.textContent =
            s.id + " · " + (s.org || "-") + " · " + (s.country || "-") +
            " · " + (s.endpoint_count || 0) + " 个端点";
          btn.appendChild(id);
          btn.appendChild(meta);
          if (s.summary) btn.title = s.summary;
          el.srcList.appendChild(btn);
        });
      });
    }

    if (el.srcCount) {
      var total = state.sourceTotal || state.sources.length;
      var text;
      if (q) {
        text = "过滤后 " + matched.length + " / " + total;
      } else if (total > state.sources.length) {
        text = "只显示前 " + state.sources.length + " / 共 " + total + " 个源";
      } else {
        text = total + " 个源";
      }
      setBadge(el.srcCount, text, q ? "info" : (total > state.sources.length ? "warn" : ""));
    }
  }

  /* ── 渲染:端点列表 ────────────────────────────────────────────────── */

  function renderEndpoints() {
    if (!el.epList) return;
    el.epList.innerHTML = "";
    if (!state.endpoints.length) {
      el.epList.innerHTML =
        '<div class="endpoint-item" style="cursor:default">这个源没有端点定义</div>';
      setBadge(el.epCount, "0", "");
      return;
    }
    state.endpoints.forEach(function (ep) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "endpoint-item";
      if (state.endpoint && state.endpoint.id === ep.id) btn.classList.add("is-selected");
      btn.setAttribute("data-ep", ep.id);
      var id = document.createElement("span");
      id.className = "ep-id";
      id.style.display = "block";
      id.textContent = ep.id;
      var meta = document.createElement("span");
      meta.className = "ep-meta";
      meta.style.display = "block";
      var required = (ep.params || []).filter(function (p) {
        return p.required;
      });
      meta.textContent =
        (ep.method || "GET") + " · " + (ep.name || "") +
        (required.length ? " · 必填 " + required.length + " 个参数" : " · 无必填参数");
      btn.appendChild(id);
      btn.appendChild(meta);
      if (ep.url_template) btn.title = ep.url_template;
      el.epList.appendChild(btn);
    });
    setBadge(el.epCount, state.endpoints.length + " 个端点", "");
  }

  /* ── 渲染:参数模板 ────────────────────────────────────────────────── */

  function paramTemplate(ep) {
    var tpl = [];
    (ep.params || []).forEach(function (p) {
      // 预填顺序: example → default。
      // 可选参数也要预填 —— 否则会把空值发出去, 上游拿到 "size=" 这种残缺参数。
      var value = "";
      if (p.example !== undefined && p.example !== null && p.example !== "") {
        value = p.example;
      } else if (p.default !== undefined && p.default !== null) {
        value = p.default;
      }
      tpl.push({
        name: p.name,
        type: p.type || "string",
        required: !!p.required,
        in: p.in || "query",
        desc: p.desc || "",
        value: value,
        missing: p.required && value === ""
      });
    });
    return tpl;
  }

  function renderParams() {
    if (!el.params) return;
    if (!state.endpoint) {
      el.params.innerHTML =
        '<p class="inline-hint">选好端点后,这里会按端点声明的 <code>example</code>(其次 <code>default</code>)生成参数模板。</p>';
      return;
    }
    var ep = state.endpoint;
    var resp = ep.response || {};
    var html = "";

    html +=
      '<p class="inline-hint">' +
      escapeHtml(ep.name || ep.id) + " · " + escapeHtml(ep.method || "GET") +
      " · <code>" + escapeHtml(ep.url_template || "") + "</code><br>" +
      "响应: " + escapeHtml(resp.shape || "-") +
      " · item_path=<code>" + escapeHtml(resp.item_path || "-") + "</code>" +
      " · total_field=<code>" + escapeHtml(resp.total_field || "-") + "</code>" +
      "</p>";

    var tpl = paramTemplate(ep);
    var missing = tpl.filter(function (p) {
      return p.missing;
    });
    if (missing.length) {
      html +=
        '<p class="inline-hint" style="color:var(--warn)">必填参数 ' +
        escapeHtml(
          missing
            .map(function (p) {
              return p.name;
            })
            .join(", ")
        ) +
        " 既没有 example 也没有 default —— 手工填一个,否则网关会返回 missing_param。</p>";
    }

    if (!tpl.length) {
      html += '<p class="inline-hint">这个端点没有声明参数。</p>';
    }

    tpl.forEach(function (p) {
      html +=
        '<div class="field">' +
        '<label for="dbg-p-' + attr(p.name) + '">' + escapeHtml(p.name) +
        ' <span class="hint">' + escapeHtml(p.type) + " · " + escapeHtml(p.in) +
        (p.required ? " · 必填" : " · 可选") +
        (p.desc ? " · " + escapeHtml(p.desc) : "") + "</span></label>" +
        '<input type="text" id="dbg-p-' + attr(p.name) + '" data-param="' + attr(p.name) +
        '" value="' + attr(p.value) + '" spellcheck="false" autocomplete="off">' +
        "</div>";
    });

    el.params.innerHTML = html;
  }

  function collectParams() {
    var out = {};
    if (!el.params) return out;
    var inputs = el.params.querySelectorAll("input[data-param]");
    Array.prototype.forEach.call(inputs, function (inp) {
      var name = inp.getAttribute("data-param");
      var v = inp.value;
      if (v === "" || v === null || v === undefined) return;
      out[name] = v;
    });
    return out;
  }

  /* ── curl ─────────────────────────────────────────────────────────── */

  function buildCurl(body) {
    // 必须与实际发出的 body 完全一致 —— 少带 fields / timeout_ms,
    // 复制出去的命令就不是"等价"的了。
    var payload = JSON.stringify({
      source: body.source,
      endpoint: body.endpoint,
      params: body.params,
      fields: body.fields || [],
      limit: body.limit,
      timeout_ms: body.timeout_ms
    });
    return (
      "curl -s -X POST " + api("/v1/query") + " \\\n" +
      "  -H 'Content-Type: application/json' \\\n" +
      "  -d " + shellQuote(payload)
    );
  }

  // 单引号包起来的 shell 字符串, 内部单引号要转义 —— 否则参数里带引号就把命令写坏了。
  function shellQuote(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
  }

  function updateCurl() {
    if (!el.curl) return;
    if (!state.lastBody) {
      el.curl.textContent = "（选好端点后自动生成）";
      return;
    }
    el.curl.textContent = buildCurl(state.lastBody);
  }

  /* ── 发送 ─────────────────────────────────────────────────────────── */

  function buildBody() {
    var limit = parseInt(el.limit && el.limit.value, 10);
    if (!limit || limit < 1) limit = 20;
    if (limit > 500) limit = 500;
    var timeout = parseInt(el.timeout && el.timeout.value, 10);
    if (!timeout || timeout < 50) timeout = 50;
    if (timeout > 500) timeout = 500;
    return {
      source: state.source ? state.source.id : "",
      endpoint: state.endpoint ? state.endpoint.id : "",
      params: collectParams(),
      fields: [],
      limit: limit,
      timeout_ms: timeout
    };
  }

  function setBusy(busy) {
    state.inflight = busy;
    if (!el.send) return;
    el.send.disabled = busy;
    el.send.textContent = busy ? "发送中…" : "发送请求";
  }

  function setRespStatus(parts) {
    if (!el.respStatus) return;
    el.respStatus.innerHTML = parts.join("");
  }

  function renderEnvelope(res) {
    var json = res.json;
    var meta = (json && json.meta) || {};
    var parts = [];

    parts.push(badge("HTTP " + res.status, res.ok ? "ok" : "danger"));
    if (meta.degraded) parts.push(badge("降级", "warn"));
    else if (meta.degraded === false) parts.push(badge("未降级", "ok"));
    if (meta.cached) parts.push(badge("命中缓存", "info"));
    if (meta.truncated) parts.push(badge("已截断", "warn"));
    if (typeof meta.latency_ms === "number") parts.push(escapeHtml(meta.latency_ms) + " ms");
    if (typeof meta.count === "number") {
      parts.push(
        escapeHtml(meta.count) +
          (meta.total !== null && meta.total !== undefined ? " / " + escapeHtml(meta.total) : "") +
          " 条"
      );
    }
    if (meta.parser) parts.push(escapeHtml("解析器 " + meta.parser));
    if (meta.upstream && meta.upstream.status !== null && meta.upstream.status !== undefined) {
      parts.push(escapeHtml("上游 HTTP " + meta.upstream.status));
    }
    if (meta.trace_id) parts.push(escapeHtml("trace " + meta.trace_id));
    setRespStatus(parts);

    var blocks = [];

    // 1) 降级 —— 这是设计的一部分, 说清楚下一步
    if (meta.degraded) {
      var fb = meta.fallback || {};
      // 原因不能写死: 上游超时取 min(端点 budget_ms, 剩余预算),
      // 也可能是上游报错或解析失败。以 meta.errors 里实际的错误类型为准。
      var reason = degradedReason(meta);
      var body = "<p>网关在预算内没取到数据, 这次是<strong>降级返回</strong>(不是故障)。</p>";
      body += "<p><strong>等几秒再点一次</strong> —— 网关正在后台补数, 同一个查询会命中缓存(" +
        "<code>cached=true</code>, <code>latency_ms</code> 常是 1ms)。也可以直接用下面的地址直连上游:</p>";
      if (fb.upstream_url) {
        body +=
          '<p><code>' + escapeHtml(fb.upstream_url) + "</code></p>";
      }
      if (fb.hint) body += "<p>" + escapeHtml(fb.hint) + "</p>";
      if (reason) body += "<p>" + escapeHtml(reason) + "</p>";
      blocks.push(msgBlock("warn", "降级: 等几秒再点一次", body));
      showRetry(true);
    } else {
      showRetry(false);
    }

    // 2) errors
    var errors = meta.errors || [];
    if (errors.length) {
      var ehtml = "";
      errors.forEach(function (e) {
        ehtml += "<p><code>" + escapeHtml(e.type || "error") + "</code> " +
          escapeHtml(e.message || "") + "</p>";
      });
      blocks.push(msgBlock("danger", "错误 (" + errors.length + ")", ehtml));
    }

    // 3) 代理不可达: 原样展示 detail / hint
    if (res.status === 502 && json && json.detail) {
      var ghtml = "<p>" + escapeHtml(json.detail) + "</p>";
      if (json.gateway) ghtml += "<p>网关地址: <code>" + escapeHtml(json.gateway) + "</code></p>";
      if (json.hint) ghtml += "<p>" + escapeHtml(json.hint) + "</p>";
      blocks.push(msgBlock("danger", "网关不可达(文档站代理返回的 502)", ghtml));
      showRetry(true);
    } else if (!res.ok && json && json.detail) {
      blocks.push(msgBlock("danger", "HTTP " + res.status, "<p>" + escapeHtml(json.detail) + "</p>"));
    } else if (!res.ok && !json) {
      blocks.push(
        msgBlock(
          "danger",
          "HTTP " + res.status,
          "<p>响应不是 JSON, 原文如下:</p><pre style=\"white-space:pre-wrap;word-break:break-word\">" +
            escapeHtml(res.text.slice(0, 2000)) + "</pre>"
        )
      );
    }

    if (blocks.length) {
      el.msg.innerHTML = blocks.join("");
    } else if (res.ok) {
      // 成功也留一句明确的话 —— 光看状态条不够, "到底拿到了没有"要一眼可见
      var okHtml = "<p>返回 <code>" + (meta.count || 0) + "</code> 条";
      if (meta.total !== null && meta.total !== undefined) {
        okHtml += "(上游总数 <code>" + meta.total + "</code>)";
      }
      okHtml += ", 耗时 <code>" + (meta.latency_ms || 0) + "ms</code>";
      okHtml += meta.cached ? ", <strong>命中缓存</strong>。" : ", 实时取自上游。";
      if (meta.truncated) okHtml += " 结果已按 limit 截断。";
      okHtml += "</p>";
      blocks.push(msgBlock("tip", "成功", okHtml));
      el.msg.innerHTML = blocks.join("");
    } else {
      clearMsg();
    }

    if (el.respJson) {
      el.respJson.textContent = json ? fmtJSON(json) : res.text;
    }
  }

  function showRetry(show) {
    if (el.retry) el.retry.hidden = !show;
  }

  // 降级原因: 按 meta.errors 里真实的错误类型说, 不写死预算数字。
  function degradedReason(meta) {
    var errs = meta.errors || [];
    var types = errs.map(function (e) {
      return e && e.type;
    });
    var budget = meta.budget_ms;
    if (types.indexOf("timeout") !== -1) {
      return "原因: 上游响应慢于本次可用预算(" + (budget || 500) + "ms 总预算里留给上游的那部分)。" +
        "跨境源常见; 调大 timeout_ms 或换更近的源会好一些。";
    }
    if (types.indexOf("upstream_error") !== -1) {
      return "原因: 上游返回了错误(见下面的错误列表)。多半是限速或 UA 没带联系邮箱。";
    }
    if (types.indexOf("parse_error") !== -1) {
      return "原因: 上游返回了数据但解析不出来 —— 多半是上游改了返回结构。";
    }
    if (types.indexOf("response_too_large") !== -1) {
      return "原因: 响应体太大, 超过了网关的解析上限。可以调小 limit 或换专用端点。";
    }
    if (types.length) {
      return "原因见下面的错误列表。";
    }
    return "";
  }

  function send() {
    if (state.inflight) return;
    if (!state.source || !state.endpoint) {
      showErrorBlock("还差一步", ["先选一个数据源, 再选一个端点。"], "warn");
      return;
    }
    var body = buildBody();
    state.lastBody = body;
    updateCurl();
    clearMsg();
    setBusy(true);
    setRespStatus(['<span class="spinner"></span>', escapeHtml("请求中…")]);

    var headers = { "Content-Type": "application/json" };
    // 自定义头会触发 CORS 预检, 直连模式下没有意义 —— 只在同源代理时带上
    if (isSameOrigin(api("/v1/query"))) headers["X-Request-Id"] = newTraceId();

    request("/v1/query", {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body)
    })
      .then(function (res) {
        setBusy(false);
        renderEnvelope(res);
      })
      .catch(function (err) {
        setBusy(false);
        var info = err.info || { title: "请求失败", message: err.message };
        var body2 = "<p>" + escapeHtml(info.message) + "</p>";
        body2 += "<p>请求: <code>" + escapeHtml(api("/v1/query")) + "</code></p>";
        el.msg.innerHTML = msgBlock("danger", info.title, body2);
        setRespStatus([badge("请求失败", "danger"), escapeHtml(err.kind || "")]);
        if (el.respJson) el.respJson.textContent = "";
        showRetry(true);
      });
  }

  /* ── 加载:状态 / 源 / 端点 ────────────────────────────────────────── */

  function probeGateway() {
    if (!el.status) return;
    setBadge(el.status, "检测中…", "");
    request("/livez")
      .then(function (res) {
        if (res.ok && res.json && res.json.ok) {
          setBadge(el.status, "在线", "ok");
          if (el.status) {
            el.status.title =
              "uptime " + (res.json.uptime_s || "?") + "s";
          }
        } else if (res.status === 502 && res.json && res.json.detail) {
          setBadge(el.status, "离线(网关不可达)", "danger");
          showErrorBlock(
            "网关不可达(文档站代理返回的 502)",
            [res.json.detail, res.json.gateway ? "网关地址: " + res.json.gateway : "", res.json.hint || ""]
              .filter(Boolean),
            "danger"
          );
        } else {
          setBadge(el.status, "异常 HTTP " + res.status, "warn");
        }
      })
      .catch(function (err) {
        var info = err.info || { title: "网关不可达", message: err.message };
        setBadge(el.status, "离线", "danger");
        showErrorBlock(info.title, [info.message], "danger");
      });
  }

  function loadSources() {
    if (!el.srcList) return;
    el.srcList.innerHTML = '<div class="endpoint-item" style="cursor:default">加载中…</div>';
    request("/v1/sources?limit=500")
      .then(function (res) {
        if (!res.ok) {
          var detail = res.json && res.json.detail ? res.json.detail : "HTTP " + res.status;
          el.srcList.innerHTML =
            '<div class="endpoint-item" style="cursor:default">加载失败: ' +
            escapeHtml(detail) + "</div>";
          setBadge(el.srcCount, "加载失败", "danger");
          return;
        }
        state.sources = (res.json && res.json.data) || [];
        // 源数超过一页上限时要说清楚 —— 静默截断会让人以为"只有这些源"。
        var meta = res.json && res.json.meta ? res.json.meta : {};
        state.sourceTotal = meta.count || state.sources.length;
        renderSources();
      })
      .catch(function (err) {
        var info = err.info || { title: "请求失败", message: err.message };
        el.srcList.innerHTML =
          '<div class="endpoint-item" style="cursor:default">' +
          escapeHtml(info.message) + "</div>";
        setBadge(el.srcCount, "加载失败", "danger");
      });
  }

  function selectSource(id) {
    request("/v1/sources/" + encodeURIComponent(id))
      .then(function (res) {
        if (!res.ok) {
          var detail = res.json && res.json.detail ? res.json.detail : "HTTP " + res.status;
          showErrorBlock("拿不到源 " + id, [detail], "danger");
          return;
        }
        var data = (res.json && res.json.data) || {};
        state.source = data;
        state.endpoints = data.endpoints || [];
        state.endpoint = null;
        renderSources();
        renderEndpoints();
        var pick = null;
        for (var i = 0; i < state.endpoints.length; i++) {
          if (state.endpoints[i].id !== "entry") {
            pick = state.endpoints[i];
            break;
          }
        }
        if (!pick && state.endpoints.length) pick = state.endpoints[0];
        if (pick) {
          selectEndpoint(pick.id);
        } else {
          renderParams();
          updateCurl();
        }
      })
      .catch(function (err) {
        var info = err.info || { title: "请求失败", message: err.message };
        showErrorBlock("拿不到源 " + id, [info.message], "danger");
      });
  }

  function selectEndpoint(epId) {
    var ep = null;
    for (var i = 0; i < state.endpoints.length; i++) {
      if (state.endpoints[i].id === epId) {
        ep = state.endpoints[i];
        break;
      }
    }
    if (!ep) return;
    state.endpoint = ep;
    renderEndpoints();
    renderParams();
    state.lastBody = buildBody();
    updateCurl();
  }

  /* ── 事件绑定 ─────────────────────────────────────────────────────── */

  function bind() {
    if (el.prefix) {
      el.prefix.addEventListener("change", function () {
        state.prefix = el.prefix.value.trim() || "/__gateway";
        el.prefix.value = state.prefix;
        state.source = null;
        state.endpoint = null;
        state.endpoints = [];
        state.lastBody = null;
        el.epList.innerHTML = '<div class="endpoint-item" style="cursor:default">先选一个源</div>';
        setBadge(el.epCount, "—", "");
        renderParams();
        updateCurl();
        probeGateway();
        loadSources();
      });
    }

    if (el.srcFilter) {
      el.srcFilter.addEventListener("input", renderSources);
    }

    if (el.srcList) {
      el.srcList.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest("[data-src]") : null;
        if (!btn) return;
        selectSource(btn.getAttribute("data-src"));
      });
    }

    if (el.epList) {
      el.epList.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest("[data-ep]") : null;
        if (!btn) return;
        selectEndpoint(btn.getAttribute("data-ep"));
      });
    }

    if (el.send) {
      el.send.addEventListener("click", send);
    }

    if (el.retry) {
      el.retry.addEventListener("click", function () {
        // 降级是后台补数在飞, 等一下再重发同一个请求
        setRespStatus(['<span class="spinner"></span>', escapeHtml("1.5 秒后重试…")]);
        setTimeout(function () {
          send();
        }, 1500);
      });
    }

    if (el.copyCurl) {
      el.copyCurl.addEventListener("click", function () {
        var text = el.curl ? el.curl.textContent : "";
        if (!text || text.indexOf("（") === 0) return;
        copyText(text).then(function () {
          var old = el.copyCurl.textContent;
          el.copyCurl.textContent = "已复制";
          setTimeout(function () {
            el.copyCurl.textContent = old;
          }, 1600);
        });
      });
    }

    var presets = document.querySelectorAll("[data-preset]");
    Array.prototype.forEach.call(presets, function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.getAttribute("data-preset");
        if (el.srcFilter) el.srcFilter.value = "";
        selectSource(id);
      });
    });

    // 预置示例卡片: 在新标签页打开网关的原始路径(走同源代理)
    var gwLinks = document.querySelectorAll("[data-gw]");
    Array.prototype.forEach.call(gwLinks, function (btn) {
      btn.addEventListener("click", function () {
        window.open(btn.getAttribute("data-gw"), "_blank", "noopener");
      });
    });
  }

  /* ── 启动 ─────────────────────────────────────────────────────────── */

  function init() {
    el.prefix = $("dbg-prefix");
    el.status = $("dbg-status");
    el.limit = $("dbg-limit");
    el.timeout = $("dbg-timeout");
    el.srcFilter = $("dbg-src-filter");
    el.srcList = $("dbg-src-list");
    el.srcCount = $("dbg-src-count");
    el.epList = $("dbg-ep-list");
    el.epCount = $("dbg-ep-count");
    el.params = $("dbg-params");
    el.curl = $("dbg-curl");
    el.send = $("dbg-send");
    el.retry = $("dbg-retry");
    el.copyCurl = $("dbg-copy-curl");
    el.respStatus = $("dbg-resp-status");
    el.respJson = $("dbg-resp-json");
    el.msg = $("dbg-resp-msg");

    if (!el.prefix) return; // 不是调试台页面(理论上不会发生)
    state.prefix = el.prefix.value.trim() || "/__gateway";

    bind();
    probeGateway();
    loadSources();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
