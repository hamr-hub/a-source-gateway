# 性能基线与 500ms 契约

> 测量方法、实测数字、以及"为什么会踩这些坑"。

---

## 1. 契约

**任何一次 `/v1/query` 或 MCP `query_source` 调用,都必须在 500ms 内返回。**

达不到时的行为**不是超时挂死**,而是返回一个降级信封:

```jsonc
{
  "meta": {
    "degraded": true,
    "errors": [{"type": "timeout", "message": "上游超过 400ms 预算, 已降级"}],
    "fallback": {
      "upstream_url": "https://api.crossref.org/works?filter=...",
      "method": "GET",
      "hint": "网关预算内取不到;可直连上游, 或稍后重试(后台正在补数)"
    }
  },
  "data": []
}
```

元数据类接口(不触网)自然远低于预算。

---

## 2. 怎么测

```bash
# 自拉服务跑全套(冷路径 + 热路径 + 数据正确性)
a-source-gateway bench --rounds 3 --cold-rounds 2 --out bench-report.json

# 测已在跑的服务
a-source-gateway bench --url http://127.0.0.1:8848 --rounds 5
```

### 2.1 冷路径怎么强制

普通请求第二次就命中缓存,测不出冷路径。基准用保留参数 `_cb`(只进缓存键、不发给上游)
制造 cache miss:

```jsonc
{"source":"crossref","endpoint":"works",
 "params":{"filter":"from-index-date:2026-10-21","_cb":"1727..."}}
```

### 2.2 测什么

| 维度 | 判据 |
|---|---|
| 延迟 | p95 与 max 都不超过 500 ms |
| 数据正确性 | 热路径的每个用例都要 `count > 0`,且 `total` 与独立探测一致 |
| 降级可见性 | 冷路径允许降级,但 `meta.degraded` / `meta.fallback` 必须正确给出 |

**PASS 的定义是"延迟契约成立"**,不是"每个源都拿到数据"——后者受上游限流与网络波动影响,
不属于网关可控范围,基准会把它单独列出来。

---

## 3. 实测基线

12 个源(覆盖 JSON / Atom XML / TSV / CSV / map 形响应),3 轮热路径 + 24 次强制冷路径。

| 指标 | 实测 |
|---|---|
| 元数据查询 p95 | **4.7 ms** |
| 热路径中位 | **1.6 ms** |
| 热路径 p95(混入冷路径样本) | 405.5 ms |
| 冷路径(强制 cache miss)最差 | **421.7 ms** |
| 超预算用例 | **0** |
| 热路径有数据 | 10–12 / 12 |

各源返回的 `total` 与独立探测一致:

| 源 | total | 独立探测对照 |
|---|---|---|
| SEC submissions | 1002 | ✅ Apple 近期申报 1001 + 1 |
| SEC company_tickers | 10425 | ✅ 与 `company_tickers.json` 条目数一致 |
| ClinicalTrials.gov | 14525 | ✅ 与区间计数一致 |
| OpenAlex | 3913431 | ✅ 近 30 天新发表量 |
| Crossref | 1892151 | ✅ from-index-date 命中量 |
| GLEIF | 3470190 | ✅ LEI 总量 |
| Eurostat | 12279 | ✅ 目录行数 |
| World Bank | 29544 | ✅ 指标总数 |
| DataCite | 139779462 | ✅ 数据集 DOI 总量 |
| ORCID | 30229218 | ✅ 研究者总数 |
| GBIF | 1075 | ✅ "Puma" 匹配物种数 |

### 3.1 环境性波动

SEC 系源在**连续高频访问**下会出现上游 403 或后台补数未完成。隔离复测(单源、间隔访问)
完全正常:首次 401 ms 降级 → 约 1 s 后补数完成 → 二次查询 **0 ms** 返回 `total=1002`。

基准因此做了两个设计:

1. 未配置 `A_GATEWAY_CONTACT` 时**跳过**需要邮箱 UA 的源(SEC 会直接 403);
2. 热路径阶段**主动轮询到缓存真的热了**再开始计时,而不是只等"没有补数任务在跑"
   ——后者有竞态(补数任务可能在预热请求返回之后才被登记)。

---

## 4. 预算怎么分配

```
总预算 500ms
├─ 元数据查询      ~0ms      内存索引
└─ 数据查询
   ├─ 缓存命中      ~1ms
   ├─ 缓存过期      ~1ms      stale-while-revalidate: 返陈旧值 + 后台刷新
   └─ 缓存未命中
      ├─ 上游取数   ≤400ms     asyncio.timeout 总墙钟
      ├─ 解析+投影  ≤ 60ms     正文上限 1MB / CSV 最多 15000 行
      └─ 序列化     ≤ 40ms     预留
```

---

## 5. 踩过的坑(以及为什么必须这样写)

### 5.1 必须用总墙钟 deadline

httpx 的 `timeout=` 是**每次 socket 操作**的超时,不是总时长。慢速持续吐数据的大响应
(Eurostat 目录 2MB)会让总耗时远超预期。

```python
async with asyncio.timeout(timeout_s):
    async with client.stream("GET", url) as r:
        body, truncated = await read_capped(r, cap)
```

### 5.2 客户端不能设 timeout

```python
httpx.AsyncClient(timeout=httpx.Timeout(5.0))   # ❌ 独立于预算
httpx.AsyncClient(timeout=httpx.Timeout(None))  # ✅ 预算只由 asyncio.timeout 控制
```

客户端级 read timeout 会在 5 秒掐断 29MB 的 OFAC XML,**后台补数静默失败** ——
异常被吞、`warm_fills` 不涨、缓存永远填不上。这个坑花了一轮才定位。

### 5.3 客户端级 timeout 失败要有指标

后台补数失败必须记进 `/metrics`(`asg_warm_errors_total`),不能 `except: pass`。
否则"缓存永远填不上"这类问题无从排查。

### 5.4 页大小必须归一

不归一时,"要 3 条"和"要 50 条"是两个缓存键,预热全部落空。而且上游可能因为
`rows=50` 返回超上限的响应 → 截断 → JSON 解析失败 → **看起来像"查到了但没数据"**。

### 5.5 响应截断必须显式降级

截断时不能退回 `_text` 然后当成"成功但无数据"。正确做法是显式报 `response_too_large`
并给出直连指引。

### 5.6 TLS 用系统信任库

httpx 默认用 certifi 证书包,`treasury.gov` 的证书链在里面没有 → `CERTIFICATE_VERIFY_FAILED`。
改用 `ssl.create_default_context()`(系统库)即可。

### 5.7 User-Agent 要带联系方式

SEC 在 UA 里看不到联系邮箱时**直接 403**。网关因此:

- 未配置 `A_GATEWAY_CONTACT` 时,对要求邮箱 UA 的源直接返回 `contact_required` + 明确 hint
  (0 ms),而不是让用户去猜为什么 403;
- 配置后自动把邮箱填进端点里需要 `email`/`mailto` 的参数(如 OpenAlex 的礼貌池)。

### 5.8 自定义 Dialer 会把 HTTP/2 关掉(Go 侧)

Go 标准库有一条保守规则:**只要 Transport 设了自定义 `Dial` / `DialContext` / `DialTLS`
或 `TLSClientConfig`,就默认不启用 HTTP/2**。网关的上游 transport 为了设置连接超时与
KeepAlive 设了 `DialContext` —— 于是**每次 HTTPS 调用都退回 HTTP/1.1**。

代价是实打实的:同一主机的第 N 次调用要重新走一次 TCP + TLS 握手(境外 2 个 RTT),
而预热阶段会成组地打同一主机的多个端点(政府政策库、交易所列表都在一个域名下)。

修法是显式把协议打开:`Transport.ForceAttemptHTTP2 = true`(ALPN 协商,上游不支持 h2
就自动回落 HTTP/1.1,不会失败)。开关:`A_GATEWAY_HTTP2=0` 强制 HTTP/1.1 ——
只在上游的 h2 实现有问题时才需要。

> 与 Python 参照面的**有意分歧**:httpx 要用 h2 得装 `h2` 包,本项目不把它列为依赖。
> Python 平面是回落路径,Go 网关才是主运行时 —— 传输协议不属于信封/注册表的语义契约。

---

## 6. 调参

| 环境变量 | 默认 | 什么时候调 |
|---|---|---|
| `A_GATEWAY_BUDGET_MS` | 500 | **不建议调高** —— 这是对外契约 |
| `A_GATEWAY_UPSTREAM_BUDGET_MS` | 400 | 上游普遍变慢时可降到 300,给解析留更多余量 |
| `A_GATEWAY_RESERVE_MS` | 100 | 解析占比高时调到 150 |
| `A_GATEWAY_MAX_BODY_BYTES` | 1048576 | 需要更大的 JSON 响应时可调大,但会推高解析耗时 |
| `A_GATEWAY_WARM_BUDGET_MS` | 30000 | 慢上游(EUROSTAT 目录实测 7.5–30 s)需要更长的补数预算 |
| `A_GATEWAY_CACHE_TTL_S` | 900 | 数据新鲜度要求高时调小 |
| `A_GATEWAY_HTTP2` | 1 | 上游 h2 实现有问题时设为 `0`(见 §5.8) |

---

## 7. 压测自己

```bash
# 冷路径: 每次都强制 cache miss
for i in $(seq 1 20); do
  curl -s -X POST http://127.0.0.1:8848/v1/query -H 'Content-Type: application/json' \
    -d "{\"source\":\"crossref\",\"endpoint\":\"works\",
         \"params\":{\"filter\":\"from-index-date:2026-10-21\",\"_cb\":\"$i\"}}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['meta']['latency_ms'],'ms', d['meta']['degraded'])"
done

# 看指标
curl -s http://127.0.0.1:8848/metrics | grep asg_
```
