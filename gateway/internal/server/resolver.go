package server

import (
	"context"
	"errors"
	"fmt"
	"log"
	"math"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/cache"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/envelope"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/metrics"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/ratelimit"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/registry"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/upstream"
)

// DefaultPageSize is the upstream page size used when an endpoint does not
// declare one.
const DefaultPageSize = 25

// PageSizeParams are the parameter names that carry the upstream page size.
// They are normalised to one effective value so that "3 items" and "50
// items" hit the same cache entry.
var PageSizeParams = map[string]bool{
	"size": true, "rows": true, "per_page": true, "per-page": true,
	"page_size": true, "pageSize": true,
}

// CacheBuster is stripped before the request is sent upstream.
const CacheBuster = "_cb"

// PrewarmConcurrency is the number of sources warmed in parallel. Warming
// one by one takes more than ten minutes, so the cache stays cold — and
// every call degrades — for that long after a restart.
const PrewarmConcurrency = 6

// emptyPayloadTTLSeconds caps how long an empty upstream result stays cached.
// An upstream that answers 200 with an empty payload under load (seen with the
// SZSE list endpoint during prewarm) must not poison the cache for the whole
// endpoint TTL: the caller would get an empty shell for hours.
const emptyPayloadTTLSeconds = 30

var placeholderRe = regexp.MustCompile(`\{([a-zA-Z0-9_]+)\}`)

// MissingParamError reports a missing required parameter.
type MissingParamError struct {
	Missing []string
}

func (e *MissingParamError) Error() string {
	return "缺少必填参数 " + strings.Join(e.Missing, ", ")
}

// TemplateError reports an unusable URL template.
type TemplateError struct{ Err error }

func (e *TemplateError) Error() string { return e.Err.Error() }

// QueryRequest is one query against an endpoint.
type QueryRequest struct {
	Source   string
	Endpoint string
	Params   *parser.Object
	Fields   []string
	Limit    int
	Since    string
	// TimeoutMS is the total budget of the request.
	TimeoutMS int
	// TraceID correlates the request across the gateway logs, the upstream
	// call and the parser worker. An empty value is replaced by a generated
	// one.
	TraceID string
}

// cacheValue is what the cache stores for one upstream response.
type cacheValue struct {
	Data      any  `json:"_data"`
	Status    int  `json:"_status"`
	Total     *int `json:"_total"`
	Truncated bool `json:"_truncated"`
}

// MarshalJSON renders the cache value the same way the gateway renders
// documents, so that the measured cache size is the real payload size.
func (c *cacheValue) MarshalJSON() ([]byte, error) {
	o := parser.NewObject()
	o.Set("_data", c.Data)
	o.Set("_status", c.Status)
	if c.Total != nil {
		o.Set("_total", *c.Total)
	} else {
		o.Set("_total", nil)
	}
	o.Set("_truncated", c.Truncated)
	return o.MarshalJSON()
}

// Resolver executes queries against the registered sources.
type Resolver struct {
	cfg     *config.Config
	reg     *registry.Document
	index   *registry.SearchIndex
	cache   *cache.Cache
	client  *upstream.Client
	parsers *parser.Manager
	metrics *metrics.Metrics
	limiter *ratelimit.Limiter
	logger  *log.Logger
	offline bool

	mu           sync.Mutex
	tooLarge     map[string]bool
	warming      map[string]bool
	prewarmed    map[string]string
	prewarmDone  bool
	warmFills    int
	pageOverride map[string]int
}

// NewResolver builds a resolver.
func NewResolver(cfg *config.Config, reg *registry.Document, c *cache.Cache, client *upstream.Client,
	parsers *parser.Manager, m *metrics.Metrics, limiter *ratelimit.Limiter, logger *log.Logger) *Resolver {
	if logger == nil {
		logger = log.Default()
	}
	// The search index is normally built together with the registry. A
	// document that was assembled by hand (tests, tools) gets its index
	// built here, once: the request path never builds one.
	var index *registry.SearchIndex
	if reg != nil {
		index = reg.Search
		if index == nil {
			index = registry.BuildSearchIndex(reg.Sources)
		}
	} else {
		index = registry.BuildSearchIndex(nil)
	}
	return &Resolver{
		cfg:          cfg,
		reg:          reg,
		index:        index,
		cache:        c,
		client:       client,
		parsers:      parsers,
		metrics:      m,
		limiter:      limiter,
		logger:       logger,
		offline:      cfg.Offline,
		tooLarge:     map[string]bool{},
		warming:      map[string]bool{},
		prewarmed:    map[string]string{},
		pageOverride: map[string]int{},
	}
}

// Registry returns the registry the resolver serves.
func (r *Resolver) Registry() *registry.Document { return r.reg }

// Cache returns the response cache.
func (r *Resolver) Cache() *cache.Cache { return r.cache }

// PrewarmDone reports whether the background prewarm finished.
func (r *Resolver) PrewarmDone() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.prewarmDone
}

// Prewarmed returns the per source prewarm states.
func (r *Resolver) Prewarmed() map[string]string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string]string, len(r.prewarmed))
	for k, v := range r.prewarmed {
		out[k] = v
	}
	return out
}

// PendingWarms returns the number of running background refreshes.
func (r *Resolver) PendingWarms() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.warming)
}

// WarmFills returns the number of successful background refreshes.
func (r *Resolver) WarmFills() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.warmFills
}

// Offline reports whether the gateway runs in offline mode.
func (r *Resolver) Offline() bool { return r.offline }

// ParserName returns the name of the active parsing engine.
func (r *Resolver) ParserName() string {
	if r.parsers == nil {
		return "go"
	}
	return r.parsers.Name()
}

// Query executes a query and returns the response envelope.
//
// The order of operations is the one of the Python reference: resolve the
// source, find the endpoint, check the contact requirement, render the URL,
// look into the cache, fetch, parse, project and finally build the envelope.
func (r *Resolver) Query(req QueryRequest, budgetMS *int) *envelope.Envelope {
	t0 := time.Now()
	strict := budgetMS == nil
	budget := 0
	if budgetMS != nil {
		budget = *budgetMS
	} else {
		t := req.TimeoutMS
		if t == 0 {
			t = 500
		}
		budget = t
		if budget > 500 {
			budget = 500
		}
	}

	meta := envelope.NewMeta()
	meta.Source = req.Source
	meta.Endpoint = req.Endpoint
	meta.BudgetMS = budget
	// The parsing engine and the trace id are reported on every path, also
	// on the early error returns.
	meta.Parser = r.ParserName()
	meta.TraceID = req.TraceID
	if meta.TraceID == "" {
		meta.TraceID = envelope.NewTraceID()
	}

	// The hard deadline of the call: the upstream call, the parse and the
	// serialisation all have to be done before it.
	deadline := t0.Add(time.Duration(budget) * time.Millisecond)
	// The deadline of the work phases (upstream and parse). The reserve is
	// kept for the serialisation: the projection, the cache write and the
	// encoding of the envelope. The parse phase never gets a fresh budget,
	// it only gets what is left of the work deadline.
	workDeadline := deadline.Add(-time.Duration(r.cfg.ReserveMS) * time.Millisecond)

	rid := r.ResolveSource(req.Source)
	src := r.reg.Source(rid)
	if src == nil {
		meta.AddError("not_found", fmt.Sprintf("未知数据源 %s", req.Source))
		meta.LatencyMS = elapsedMS(time.Since(t0))
		return buildEnvelope(meta, nil, req.Fields)
	}
	req.Source = rid
	meta.Source = rid

	ep := src.Endpoint(req.Endpoint)
	if ep == nil {
		meta.AddError("endpoint_not_found", fmt.Sprintf("%s 没有端点 %s", req.Source, req.Endpoint))
		meta.LatencyMS = elapsedMS(time.Since(t0))
		return buildEnvelope(meta, nil, req.Fields)
	}

	// The source demands a contact address in the User-Agent but none is
	// configured: hand out an actionable hint instead of eating a 403.
	if src.UARequiresContact && r.cfg.Contact == "" {
		meta.Degraded = true
		meta.AddError("contact_required", fmt.Sprintf("%s 要求 User-Agent 里带联系邮箱, 当前未配置(A_GATEWAY_CONTACT)", src.Name))
		meta.SetFallback(src.Homepage, "GET", "设置环境变量 A_GATEWAY_CONTACT=you@example.com 后重试")
		meta.LatencyMS = elapsedMS(time.Since(t0))
		return buildEnvelope(meta, []any{}, req.Fields)
	}

	epKey := req.Source + "|" + req.Endpoint
	if r.isTooLarge(epKey) {
		meta.Degraded = true
		meta.AddError("response_too_large", "该端点是文件型大响应, 网关只做摘要; 请直连上游")
		meta.SetFallback(ep.URLTemplate, ep.Method, "文件型大响应建议直连上游或走批量下载, 网关不做整体透传")
		meta.LatencyMS = elapsedMS(time.Since(t0))
		return buildEnvelope(meta, []any{}, req.Fields)
	}

	effPage := r.effectivePageSize(epKey, ep)
	rendered, err := r.Render(ep, req, effPage)
	if err != nil {
		var missing *MissingParamError
		if errors.As(err, &missing) {
			meta.AddError("missing_param", missing.Error())
		} else {
			meta.AddError("bad_template", err.Error())
		}
		meta.LatencyMS = elapsedMS(time.Since(t0))
		return buildEnvelope(meta, nil, req.Fields)
	}
	url := rendered
	meta.Upstream = envelope.Upstream{URL: url, Method: ep.Method}
	cacheKey := CacheKey(req.Source, req.Endpoint, url, req.Params, effPage)

	// ── cache lookup ────────────────────────────────────────────────
	val, fresh, age := r.cache.Get(cacheKey)
	if fresh {
		meta.Cached = true
		meta.CacheAgeS = age
		var data any
		cv := toCacheValue(val)
		if cv != nil {
			if cv.Status > 0 {
				s := cv.Status
				meta.Upstream.Status = &s
			}
			var total *int
			var truncated bool
			data, total, truncated = parser.Trim(cv.Data, cv.Total, req.Limit, req.Fields, cv.Truncated)
			meta.Count = countOf(data)
			meta.Total = total
			meta.Truncated = truncated
		}
		meta.LatencyMS = elapsedMS(time.Since(t0))
		r.observe(&meta, true)
		return buildEnvelope(meta, data, req.Fields)
	}
	if val != nil && !fresh {
		// stale-while-revalidate: the stale value is returned immediately and
		// refreshed in the background; the caller never waits for the
		// upstream.
		meta.Cached = true
		meta.CacheAgeS = age
		meta.Degraded = true
		meta.AddError("stale", fmt.Sprintf("缓存已过期 %ds, 返回陈旧值并在后台刷新", age))
		cv := toCacheValue(val)
		var data any
		if cv != nil {
			if cv.Status > 0 {
				s := cv.Status
				meta.Upstream.Status = &s
			}
			var total *int
			var truncated bool
			data, total, truncated = parser.Trim(cv.Data, cv.Total, req.Limit, req.Fields, cv.Truncated)
			meta.Count = countOf(data)
			meta.Total = total
			meta.Truncated = truncated
		}
		meta.LatencyMS = int(time.Since(t0).Milliseconds())
		r.scheduleWarm(ep, epKey, url, cacheKey)
		r.observe(&meta, true)
		return buildEnvelope(meta, data, req.Fields)
	}

	if r.offline {
		meta.Offline = true
		meta.Degraded = true
		meta.AddError("offline", "网关处于离线模式, 仅提供元数据与缓存")
		if val != nil {
			cv := toCacheValue(val)
			var data any
			var total *int
			if cv != nil {
				data, total, _ = parser.Trim(cv.Data, cv.Total, req.Limit, req.Fields, cv.Truncated)
			}
			meta.Cached = true
			meta.CacheAgeS = age
			meta.Count = countOf(data)
			meta.Total = total
			meta.LatencyMS = elapsedMS(time.Since(t0))
			return buildEnvelope(meta, data, req.Fields)
		}
		meta.LatencyMS = elapsedMS(time.Since(t0))
		return buildEnvelope(meta, []any{}, req.Fields)
	}

	// ── live fetch ──────────────────────────────────────────────────
	// The upstream call is bounded by the work deadline: the reserve is
	// not available to the upstream, it is kept for the parse and the
	// serialisation.
	remaining := budget - int(time.Since(t0).Milliseconds()) - r.cfg.ReserveMS
	cap := r.cfg.UpstreamBudgetMS
	if strict {
		if ep.BudgetMS > 0 {
			cap = ep.BudgetMS
		}
	} else {
		cap = remaining
	}
	timeoutMS := cap
	if remaining < timeoutMS {
		timeoutMS = remaining
	}
	if timeoutMS < 50 {
		timeoutMS = 50
	}

	res, upstreamLatency, err := r.fetch(ep, epKey, url, time.Duration(timeoutMS)*time.Millisecond)
	if err != nil {
		// The upstream block reports how long the call ran before it failed.
		meta.Upstream.LatencyMS = elapsedMS(upstreamLatency)
		meta.Degraded = true
		if errors.Is(err, upstream.ErrTimeout) || errors.Is(err, context.DeadlineExceeded) {
			meta.AddError("timeout", fmt.Sprintf("上游超过 %dms 预算, 已降级", timeoutMS))
			meta.SetFallback(url, ep.Method, "网关预算内取不到;可直连上游, 或稍后重试(后台正在补数)")
		} else {
			meta.AddError(upstreamErrorType(err), truncateMsg(err.Error()))
			meta.SetFallback(url, ep.Method, "网关取数失败;可直连上游")
		}
		r.scheduleWarm(ep, epKey, url, cacheKey)
		data, _, _ := r.fallbackData(val)
		meta.Cached = val != nil
		meta.CacheAgeS = age
		meta.Count = countOf(data)
		meta.LatencyMS = elapsedMS(time.Since(t0))
		if meta.LatencyMS > budget {
			meta.AddError("budget_exceeded", fmt.Sprintf("实际 %dms 超过预算 %dms", meta.LatencyMS, budget))
		}
		r.observe(&meta, false)
		return buildEnvelope(meta, data, req.Fields)
	}

	meta.Upstream.LatencyMS = elapsedMS(res.Latency)
	status := res.Status
	meta.Upstream.Status = &status

	if res.Truncated {
		// The body exceeded the cap: the fragment cannot be parsed, so the
		// gateway degrades explicitly instead of returning an empty result.
		meta.Degraded = true
		meta.AddError("response_too_large", fmt.Sprintf("响应超过 %dMB 上限被截断;请减小 limit 或直连上游", r.cfg.MaxBodyBytes/1024/1024))
		meta.SetFallback(url, ep.Method, "减小 limit 后重试, 或直连上游")
		r.scheduleWarm(ep, epKey, url, cacheKey)
		data, _, _ := r.fallbackData(val)
		meta.Cached = val != nil
		meta.CacheAgeS = age
		meta.Count = countOf(data)
		meta.LatencyMS = elapsedMS(time.Since(t0))
		return buildEnvelope(meta, data, req.Fields)
	}

	payload, parseErr := r.parseInBudget(ep, res.Body, meta.TraceID, workDeadline)
	// The engine that actually produced the data is reported, also when the
	// Python worker failed and the Go engine took over.
	meta.Parser = r.ParserName()
	if parseErr != nil {
		meta.Degraded = true
		if status >= 400 {
			// The upstream answered with an error status: the status
			// belongs into the envelope. The parse error only says what
			// came back (an HTML error page for example).
			meta.AddErrorStatus("upstream_error", status, truncateMsg(string(res.Body)))
		}
		if isDeadlineError(parseErr) {
			// The parse did not fit into the remaining budget: the call
			// degrades exactly like an upstream timeout, the caller gets
			// the fallback block instead of a late answer.
			meta.AddError("timeout", fmt.Sprintf("解析超过剩余预算, 已降级(上游 %dms)", meta.Upstream.LatencyMS))
			meta.SetFallback(url, ep.Method, "网关预算内解析不完;可直连上游, 或稍后重试(后台正在补数)")
		} else if errors.Is(parseErr, parser.ErrNotJSON) {
			// The upstream answered something that is not JSON (an HTML
			// error page for example). The call is degraded and hands out
			// the direct upstream fallback: an empty envelope would look
			// like "the source has no data", which it is not.
			meta.AddError("upstream_not_json", truncateMsg(parseErr.Error()))
			meta.SetFallback(url, ep.Method, "上游返回的不是 JSON(可能是错误页);请直连上游查看原始响应")
		} else {
			meta.AddError("parse_error", truncateMsg(parseErr.Error()))
			meta.SetFallback(url, ep.Method, "网关取数失败;可直连上游")
		}
		r.scheduleWarm(ep, epKey, url, cacheKey)
		data, _, _ := r.fallbackData(val)
		meta.Cached = val != nil
		meta.CacheAgeS = age
		meta.Count = countOf(data)
		meta.LatencyMS = elapsedMS(time.Since(t0))
		r.observe(&meta, false)
		return buildEnvelope(meta, data, req.Fields)
	}

	if status < 400 {
		ttl := r.adaptiveTTL(ep, cacheKey)
		if isEmptyPayload(payload.data) {
			// 空结果只短暂缓存: 上游在高并发下(例如预热同时打几十个源)偶尔返回
			// 200 + 空数据, 缓存下来会让调用方在 TTL 内一直拿到空壳。
			// 30 秒后自愈, 且下一次调用会重新取数。
			if ttl > emptyPayloadTTLSeconds {
				ttl = emptyPayloadTTLSeconds
			}
		}
		// The cache write serialises the whole payload. When the budget is
		// already used up, the background refresh fills the entry instead
		// of pushing the call past its deadline.
		if time.Now().Before(deadline) {
			r.cache.Put(cacheKey, &cacheValue{Data: payload.data, Status: status, Total: payload.total, Truncated: payload.truncated}, ttl)
		} else {
			r.scheduleWarm(ep, epKey, url, cacheKey)
		}
	}
	data, total, truncated := parser.Trim(payload.data, payload.total, req.Limit, req.Fields, payload.truncated)
	meta.Count = countOf(data)
	meta.Total = total
	meta.Truncated = truncated
	if status >= 400 {
		meta.Degraded = true
		meta.AddErrorStatus("upstream_error", status, truncateMsg(string(res.Body)))
	}
	meta.LatencyMS = elapsedMS(time.Since(t0))
	if meta.LatencyMS > budget {
		meta.AddError("budget_exceeded", fmt.Sprintf("实际 %dms 超过预算 %dms", meta.LatencyMS, budget))
	}
	r.observe(&meta, false)
	return buildEnvelope(meta, data, req.Fields)
}

// parseResult carries the outcome of a parse.
type parseResult struct {
	data      any
	total     *int
	truncated bool
}

// parse runs the active parser engine for an endpoint.
//
// The context bounds the parse: the caller derives it from the request
// budget, so a parse that does not fit is cancelled instead of overrunning
// the budget.
func (r *Resolver) parse(ctx context.Context, ep *registry.EndpointSpec, body []byte, traceID string) (*parseResult, error) {
	spec := parser.Spec{
		ItemPath:    ep.Response.ItemPath,
		TotalField:  ep.Response.TotalField,
		ItemShape:   ep.Response.ItemShape,
		ItemFlatten: ep.Response.ItemFlatten,
		Limit:       0, // the gateway applies the limit itself, see Trim
		Fields:      nil,
		MaxRows:     r.cfg.MaxParseRows,
		TraceID:     traceID,
	}
	res, err := r.parsers.Parse(ctx, ep.Response.Shape, ep.Response.MediaType, body, spec)
	if err != nil {
		return nil, err
	}
	return &parseResult{data: res.Data, total: res.Total, truncated: res.Truncated}, nil
}

// parseInBudget runs the parse under the deadline that is left.
//
// The parse phase never gets a fresh budget: it runs with the time that is
// left of the work deadline (the request budget minus the elapsed time
// minus the reserve). A parse that starts with 20ms left is cancelled after
// 20ms, not after a fresh budget; a parse that does not fit degrades the
// call like an upstream timeout.
func (r *Resolver) parseInBudget(ep *registry.EndpointSpec, body []byte, traceID string, workDeadline time.Time) (*parseResult, error) {
	ctx, cancel := context.WithDeadline(context.Background(), workDeadline)
	defer cancel()
	return r.parse(ctx, ep, body, traceID)
}

// isDeadlineError reports whether the error is a deadline or a cancellation.
func isDeadlineError(err error) bool {
	return errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled)
}

// fetch performs one upstream call inside the given budget.
//
// The elapsed time is returned even when the call fails, so that a timeout
// can report how long the gateway waited for the upstream.
func (r *Resolver) fetch(ep *registry.EndpointSpec, epKey, url string, timeout time.Duration) (*upstream.Result, time.Duration, error) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	release, err := r.limiter.Acquire(ctx, epKey)
	if err != nil {
		return nil, time.Since(start), err
	}
	defer release()
	headers := map[string]string{}
	if ep.Headers != nil {
		for _, k := range ep.Headers.Keys() {
			v, _ := ep.Headers.Get(k)
			if s, ok := v.(string); ok {
				// 端点专有请求头里的 ${VAR} 用环境变量替换: 凭据不进数据文件。
				if expanded, keep := expandEnvRef(s); keep {
					headers[k] = expanded
				}
			}
		}
	}
	res, err := r.client.Fetch(ctx, ep.Method, url, headers, r.cfg.MaxBodyBytes)
	return res, time.Since(start), err
}

// envRefRe matches a ${VAR} reference inside a header value.
var envRefRe = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// expandEnvRef replaces ${VAR} references with the environment value.
//
// A header whose value references a variable that is not set is dropped
// entirely: sending "Bearer " with no credential is worse than not sending
// the header at all.
func expandEnvRef(s string) (string, bool) {
	if !strings.Contains(s, "${") {
		return s, true
	}
	matches := envRefRe.FindAllStringSubmatch(s, -1)
	if len(matches) == 0 {
		return s, true
	}
	out := s
	for _, m := range matches {
		v := os.Getenv(m[1])
		if v == "" {
			return "", false
		}
		out = strings.ReplaceAll(out, m[0], v)
	}
	return out, true
}

// fallbackData returns the cached payload of a failed request.
//
// The reference implementation returns the cached payload without a total
// and without a truncation flag, because the cached document is a fallback
// and not a regular result.
func (r *Resolver) fallbackData(val any) (any, *int, bool) {
	if val == nil {
		return []any{}, nil, false
	}
	cv := toCacheValue(val)
	if cv == nil {
		return []any{}, nil, false
	}
	return cv.Data, nil, false
}

func toCacheValue(val any) *cacheValue {
	switch t := val.(type) {
	case *cacheValue:
		return t
	case *parser.Object:
		// A value that was written by an older build.
		o := parser.NewObject()
		if v, ok := t.Get("_data"); ok {
			o.Set("_data", v)
		}
		if v, ok := t.Get("_status"); ok {
			o.Set("_status", v)
		}
		cv := &cacheValue{}
		if v, ok := o.Get("_data"); ok {
			cv.Data = v
		}
		if v, ok := o.Get("_status"); ok {
			if n, ok := parser.ToInt(v); ok {
				cv.Status = n
			}
		}
		return cv
	}
	return nil
}

// adaptiveTTL shortens the TTL of hot entries.
func (r *Resolver) adaptiveTTL(ep *registry.EndpointSpec, cacheKey string) int {
	base := ep.CacheTTLS
	if base <= 0 {
		base = r.cfg.CacheTTLSeconds
	}
	if r.cache.Hits(cacheKey) >= 3 {
		if half := base / 4; half > 30 {
			return half
		}
		return 30
	}
	return base
}

func (r *Resolver) effectivePageSize(epKey string, ep *registry.EndpointSpec) int {
	r.mu.Lock()
	if v, ok := r.pageOverride[epKey]; ok && v > 0 {
		r.mu.Unlock()
		return v
	}
	r.mu.Unlock()
	if ep.PageSize != nil && *ep.PageSize > 0 {
		return *ep.PageSize
	}
	return DefaultPageSize
}

func (r *Resolver) isTooLarge(epKey string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.tooLarge[epKey]
}

// scheduleWarm refreshes a cache entry in the background with the long warm
// budget. A failure is never silent: it is counted and logged.
func (r *Resolver) scheduleWarm(ep *registry.EndpointSpec, epKey, url, cacheKey string) {
	r.mu.Lock()
	if r.warming[cacheKey] {
		r.mu.Unlock()
		return
	}
	r.warming[cacheKey] = true
	r.mu.Unlock()

	go func() {
		defer func() {
			r.mu.Lock()
			delete(r.warming, cacheKey)
			r.mu.Unlock()
		}()
		budget := time.Duration(r.cfg.WarmBudgetMS) * time.Millisecond
		res, _, err := r.fetch(ep, epKey, url, budget)
		if err == nil && res.Status >= 500 {
			// An occasional 5xx is retried once after a short delay.
			time.Sleep(time.Second)
			res, _, err = r.fetch(ep, epKey, url, budget)
		}
		if err != nil {
			r.metrics.WarmError(fmt.Sprintf("%s: %v", epKey, err))
			r.logger.Printf("warning: background refresh failed for %s: %v", epKey, err)
			return
		}
		if res.Truncated {
			// A truncated fragment has no cache value: the endpoint is
			// remembered as a file-like response so that later queries hand
			// out the "fetch it upstream directly" hint.
			r.mu.Lock()
			r.tooLarge[epKey] = true
			r.warmFills++
			r.mu.Unlock()
			r.metrics.WarmFill()
			return
		}
		if res.Status < 400 {
			// The background refresh has its own budget: the parse of a
			// big document must not be cut off at the user request budget.
			parseCtx, cancel := context.WithTimeout(context.Background(), budget)
			payload, perr := r.parse(parseCtx, ep, res.Body, "")
			cancel()
			if perr == nil {
				ttl := r.adaptiveTTL(ep, cacheKey)
				// The background refresh must apply the same short TTL to an
				// empty payload, otherwise the empty shell it fetched would
				// be pinned for the whole endpoint TTL.
				if isEmptyPayload(payload.data) && ttl > emptyPayloadTTLSeconds {
					ttl = emptyPayloadTTLSeconds
				}
				r.cache.Put(cacheKey, &cacheValue{Data: payload.data, Status: res.Status, Total: payload.total, Truncated: payload.truncated}, ttl)
			} else {
				r.metrics.WarmError(fmt.Sprintf("%s: %v", epKey, perr))
				r.logger.Printf("warning: background refresh of %s could not be parsed: %v", epKey, perr)
				return
			}
		}
		r.mu.Lock()
		r.warmFills++
		r.mu.Unlock()
		r.metrics.WarmFill()
	}()
}

// Prewarm issues one minimal query per source to warm the connection pool
// and the cache. It never blocks the startup.
func (r *Resolver) Prewarm() {
	if r.offline {
		return
	}
	go func() {
		budget := r.cfg.WarmBudgetMS
		sem := make(chan struct{}, PrewarmConcurrency)
		var wg sync.WaitGroup
		for _, src := range r.reg.Sources {
			var ep *registry.EndpointSpec
			for _, e := range src.Endpoints {
				if e.ID != registry.GenericEndpointID {
					ep = e
					break
				}
			}
			if ep == nil {
				continue
			}
			wg.Add(1)
			go func(src *registry.SourceSpec, ep *registry.EndpointSpec) {
				defer wg.Done()
				sem <- struct{}{}
				defer func() { <-sem }()
				req := QueryRequest{
					Source:    src.ID,
					Endpoint:  ep.ID,
					Params:    prewarmParams(ep, r.cfg.Contact),
					Fields:    []string{},
					Limit:     1,
					TimeoutMS: 450,
				}
				env := r.Query(req, &budget)
				state := "ok"
				if env.Meta.Degraded {
					state = "degraded"
				}
				r.mu.Lock()
				r.prewarmed[src.ID] = state
				r.mu.Unlock()
			}(src, ep)
		}
		wg.Wait()
		r.mu.Lock()
		r.prewarmDone = true
		r.mu.Unlock()
	}()
}

// prewarmParams builds the parameter set a typical caller would send.
//
// It mirrors the query path: a parameter with a default takes its default, a
// required one that only has an example takes that example, and a contact
// bound parameter (email / mailto) falls back to the configured contact.
//
// Prewarming a *different* URL than the one callers use fills the cache with
// an entry nobody asks for, so the source stays cold and every call
// degrades. That is why this must not drift from Render.
func prewarmParams(ep *registry.EndpointSpec, contact string) *parser.Object {
	params := parser.NewObject()
	for _, p := range ep.Params {
		switch {
		case p.Env != "":
			if v := os.Getenv(p.Env); v != "" {
				params.Set(p.Name, v)
			}
		}
		if _, ok := params.Get(p.Name); !ok {
			switch {
			case p.Default != nil:
				params.Set(p.Name, p.Default)
			case p.Required && p.Example != nil:
				params.Set(p.Name, p.Example)
			}
		}
		if (p.Name == "email" || p.Name == "mailto") && isFalsy(paramsValue(params, p.Name)) && contact != "" {
			params.Set(p.Name, contact)
		}
	}
	return params
}

func (r *Resolver) observe(meta *envelope.Meta, cached bool) {
	types := make([]string, 0, len(meta.Errors))
	for _, e := range meta.Errors {
		types = append(types, e.Type)
	}
	r.metrics.Observe(metrics.Observation{
		Source:     meta.Source,
		Degraded:   meta.Degraded,
		Cached:     cached,
		LatencyMS:  meta.LatencyMS,
		ErrorTypes: types,
	})
}

// ── source resolution ────────────────────────────────────────────────

// ResolveSource maps a user supplied key onto a source id.
//
// The order is: exact id, normalised alias, normalised id, normalised name,
// substring hit. A pure non-ASCII key (for example a Chinese name) is not
// matched at all.
//
// The exact hits are served by the search index of the registry: the index
// is built once, so a request neither scans the sources nor allocates a
// normalised copy of the metadata.
func (r *Resolver) ResolveSource(key string) string {
	if key == "" {
		return ""
	}
	if r.index == nil {
		return ""
	}
	// 精确 id: 原样命中, 优先于任何别名。
	if id, ok := r.index.LookupExact(key); ok {
		return id
	}
	norm := registry.NormalizeKey(key)
	if norm == "" {
		return ""
	}
	// 别名 / 归一 id / 归一 name: 索引里按同一优先级写入, 一次命中即可。
	if id, ok := r.index.LookupToken(norm); ok {
		return id
	}
	// 兜底: 名称或 id 包含该关键词。多个命中时取名字最短的源。
	hits := []registry.SearchEntry{}
	for _, e := range r.index.Entries() {
		if strings.Contains(e.NormName, norm) || strings.Contains(e.NormID, norm) {
			hits = append(hits, e)
		}
	}
	if len(hits) == 1 {
		return hits[0].Source.ID
	}
	if len(hits) > 1 {
		best := hits[0]
		for _, h := range hits[1:] {
			if len(h.Source.Name) < len(best.Source.Name) {
				best = h
			}
		}
		return best.Source.ID
	}
	return ""
}

// GetSource returns the full source description of a source id.
func (r *Resolver) GetSource(sourceID string) *registry.SourceSpec {
	rid := r.ResolveSource(sourceID)
	if rid == "" {
		return nil
	}
	return r.reg.Source(rid)
}

// ListSources returns the lightweight projection of the source list.
//
// The keyword is matched against the haystack of the source: the canonical
// id, every alias, the name, the org, the country, the family, the summary
// and every endpoint id. The haystack is computed when the registry is
// built, so the search itself allocates nothing.
func (r *Resolver) ListSources(family, q string) []any {
	ql := strings.ToLower(q)
	out := []any{}
	if r.index == nil {
		return out
	}
	for _, e := range r.index.Entries() {
		s := e.Source
		if family != "" && !strings.Contains(s.Family, family) {
			continue
		}
		if ql != "" && !strings.Contains(e.Haystack, ql) {
			continue
		}
		item := parser.NewObject()
		item.Set("id", s.ID)
		item.Set("name", s.Name)
		// 别名让"搜到 → 知道怎么调"闭环: 搜到源之后调用方立刻知道
		// 该用哪个简称去调用。
		aliases := s.Aliases
		if aliases == nil {
			aliases = []string{}
		}
		item.Set("aliases", aliases)
		item.Set("family", s.Family)
		item.Set("org", s.Org)
		item.Set("country", s.Country)
		item.Set("summary", truncateRunes(s.Summary, 160))
		item.Set("link_status", s.LinkStatus)
		if s.Scale.Total != nil {
			item.Set("scale", *s.Scale.Total)
		} else {
			item.Set("scale", nil)
		}
		item.Set("endpoint_count", len(s.Endpoints))
		out = append(out, item)
	}
	return out
}

// Describe returns the description of a source and one of its endpoints.
func (r *Resolver) Describe(sourceID, endpointID string) *parser.Object {
	rid := r.ResolveSource(sourceID)
	if rid == "" {
		return nil
	}
	s := r.reg.Source(rid)
	if s == nil {
		return nil
	}
	if endpointID == "" {
		return nil
	}
	ep := s.Endpoint(endpointID)
	if ep == nil {
		return nil
	}
	src := parser.NewObject()
	for _, k := range []string{"id", "name", "family", "org", "country", "summary",
		"homepage", "link_status", "auth", "limits", "scale", "increment", "crawl",
		"proxy_policy", "risk"} {
		switch k {
		case "id":
			src.Set(k, s.ID)
		case "name":
			src.Set(k, s.Name)
		case "family":
			src.Set(k, s.Family)
		case "org":
			src.Set(k, s.Org)
		case "country":
			src.Set(k, s.Country)
		case "summary":
			src.Set(k, s.Summary)
		case "homepage":
			src.Set(k, s.Homepage)
		case "link_status":
			src.Set(k, s.LinkStatus)
		case "auth":
			src.Set(k, s.Auth)
		case "limits":
			src.Set(k, s.Limits)
		case "scale":
			src.Set(k, s.Scale)
		case "increment":
			src.Set(k, s.Increment)
		case "crawl":
			src.Set(k, s.Crawl)
		case "proxy_policy":
			src.Set(k, s.ProxyPolicy)
		case "risk":
			src.Set(k, s.Risk)
		}
	}
	out := parser.NewObject()
	out.Set("source", src)
	out.Set("endpoint", ep)
	return out
}

// ── URL rendering ────────────────────────────────────────────────────

// Render builds the upstream URL of an endpoint.
//
// Every placeholder value is percent encoded, the "url" one included: a
// caller must not be able to smuggle a query parameter or a fragment into
// the upstream URL. Only the path parameters keep their slashes.
// Parameters that the template does not use are appended as a query string,
// empty values are dropped and the cache buster never reaches the upstream.
func (r *Resolver) Render(ep *registry.EndpointSpec, req QueryRequest, pageSize int) (string, error) {
	params := parser.NewObject()
	if req.Params != nil {
		for _, k := range req.Params.Keys() {
			v, _ := req.Params.Get(k)
			params.Set(k, v)
		}
	}
	for _, p := range ep.Params {
		// 凭据注入: 数据文件里只写环境变量名, 真正的值由部署时提供。
		// 优先级: 调用方显式传的值 > 环境变量 > 端点声明的默认值。
		if _, ok := params.Get(p.Name); !ok && p.Env != "" {
			if v := os.Getenv(p.Env); v != "" {
				params.Set(p.Name, v)
			}
		}
		if _, ok := params.Get(p.Name); !ok && p.Default != nil {
			params.Set(p.Name, p.Default)
		}
		// 必填参数的兜底: 端点声明的 example 就是"这个端点怎么调"的答案。
		// 没有它, 调用方按文档那样 params:{} 调不动大多数端点。
		if _, ok := params.Get(p.Name); !ok && p.Required && p.Example != nil {
			params.Set(p.Name, p.Example)
		}
		if (p.Name == "email" || p.Name == "mailto") && isFalsy(paramsValue(params, p.Name)) && r.cfg.Contact != "" {
			params.Set(p.Name, r.cfg.Contact)
		}
	}
	missing := []string{}
	for _, p := range ep.Params {
		if !p.Required {
			continue
		}
		if _, ok := params.Get(p.Name); !ok {
			missing = append(missing, p.Name)
		}
	}
	if len(missing) > 0 {
		return "", &MissingParamError{Missing: missing}
	}
	if _, ok := params.Get("url"); !ok {
		params.Set("url", "")
	}
	// The cache buster is only used for the cache key, never upstream.
	params.Delete(CacheBuster)

	if pageSize <= 0 {
		pageSize = DefaultPageSize
	}
	for _, k := range params.Keys() {
		if !PageSizeParams[k] {
			continue
		}
		v := paramsValue(params, k)
		if v == nil || v == "" {
			continue
		}
		params.Set(k, pageSize)
	}

	pathParams := map[string]bool{}
	for _, p := range ep.Params {
		if p.In == "path" {
			pathParams[p.Name] = true
		}
	}

	raw := ep.URLTemplate
	out := placeholderRe.ReplaceAllStringFunc(raw, func(m string) string {
		key := placeholderRe.FindStringSubmatch(m)[1]
		v, ok := params.Get(key)
		if !ok {
			// A placeholder without a value renders as an empty string, the
			// empty query value is dropped afterwards.
			return ""
		}
		sv := pyStr(v)
		// Every placeholder value is percent encoded, the "url" one
		// included. A raw value would let the caller add a query parameter
		// ("&admin=true") or cut the query off ("#") on a mirror style
		// endpoint, so it is encoded like every other value.
		if pathParams[key] {
			return quoteValue(sv, "/")
		}
		return quoteValue(sv, "")
	})

	used := map[string]bool{}
	for _, m := range placeholderRe.FindAllStringSubmatch(raw, -1) {
		used[m[1]] = true
	}
	extra := []string{}
	for _, k := range params.Keys() {
		if used[k] || k == "url" {
			continue
		}
		v := paramsValue(params, k)
		if v == nil || v == "" {
			continue
		}
		extra = append(extra, encodeQuery(k, v)...)
	}
	if len(extra) > 0 {
		sep := "?"
		if strings.Contains(out, "?") {
			sep = "&"
		}
		out += sep + strings.Join(extra, "&")
	}
	// Drop empty query values (mailto= and friends).
	if idx := strings.Index(out, "?"); idx >= 0 {
		base := out[:idx]
		q := out[idx+1:]
		parts := []string{}
		for _, kv := range strings.Split(q, "&") {
			if kv == "" {
				continue
			}
			if strings.HasSuffix(kv, "=") {
				continue
			}
			eq := strings.Index(kv, "=")
			if eq < 0 {
				parts = append(parts, kv)
				continue
			}
			if kv[eq+1:] == "" {
				continue
			}
			parts = append(parts, kv)
		}
		out = base
		if len(parts) > 0 {
			out += "?" + strings.Join(parts, "&")
		}
	}
	return out, nil
}

// CacheKey builds the cache key of a request.
//
// Only the upstream request content matters: the limit and the field
// projection are applied by the gateway and do not change the upstream
// response.
func CacheKey(source, endpoint, url string, params *parser.Object, pageSize int) string {
	type item struct {
		k string
		v any
	}
	items := []item{}
	if params != nil {
		for _, k := range params.Keys() {
			if k == "email" {
				continue
			}
			v, _ := params.Get(k)
			if PageSizeParams[k] {
				v = pageSize
			}
			items = append(items, item{k, v})
		}
	}
	sort.SliceStable(items, func(i, j int) bool { return items[i].k < items[j].k })
	parts := make([]string, 0, len(items))
	for _, it := range items {
		parts = append(parts, "("+parser.PyRepr(it.k)+", "+parser.PyRepr(it.v)+")")
	}
	return source + "|" + endpoint + "|" + url + "|[" + strings.Join(parts, ", ") + "]"
}

// ── helpers ──────────────────────────────────────────────────────────

// elapsedMS returns the elapsed time in whole milliseconds.
//
// A request that did any work never reports zero milliseconds, so that a
// fast response is still observable as "1ms" instead of "0ms".
func elapsedMS(d time.Duration) int {
	ms := int(math.Round(float64(d.Microseconds()) / 1000))
	if ms < 1 && d > 0 {
		ms = 1
	}
	return ms
}

func buildEnvelope(meta envelope.Meta, data any, fields []string) *envelope.Envelope {
	if fields == nil {
		fields = []string{}
	}
	return &envelope.Envelope{Meta: meta, Data: data, Fields: fields}
}

func countOf(data any) int {
	switch t := data.(type) {
	case []any:
		return len(t)
	case nil:
		return 0
	default:
		return 1
	}
}

// isEmptyPayload reports whether an upstream payload carries no records.
// An empty object (the shape a top level array payload degrades to) counts
// as empty too, because it holds no usable record either.
func isEmptyPayload(data any) bool {
	switch t := data.(type) {
	case nil:
		return true
	case []any:
		return len(t) == 0
	case *parser.Object:
		return len(t.Keys()) == 0
	default:
		return false
	}
}

func paramsValue(o *parser.Object, key string) any {
	v, _ := o.Get(key)
	return v
}

// pyStr renders a parameter value the way Python's str() would.
func pyStr(v any) string {
	if v == nil {
		return "None"
	}
	return fmt.Sprintf("%v", v)
}

// isFalsy mirrors the Python truthiness test of a parameter value.
func isFalsy(v any) bool {
	switch t := v.(type) {
	case nil:
		return true
	case bool:
		return !t
	case string:
		return t == ""
	case []any:
		return len(t) == 0
	case *parser.Object:
		return t.Len() == 0
	case int:
		return t == 0
	case int64:
		return t == 0
	case float64:
		return t == 0
	}
	return false
}

// quoteValue percent encodes a value, keeping the characters of safe.
func quoteValue(s, safe string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
			c == '_' || c == '.' || c == '-' || c == '~' || strings.IndexByte(safe, c) >= 0 {
			b.WriteByte(c)
			continue
		}
		b.WriteString("%")
		const hex = "0123456789ABCDEF"
		b.WriteByte(hex[c>>4])
		b.WriteByte(hex[c&0x0f])
	}
	return b.String()
}

// encodeQuery encodes one query parameter, repeating the key for lists.
func encodeQuery(key string, value any) []string {
	enc := url.QueryEscape
	switch t := value.(type) {
	case []any:
		out := make([]string, 0, len(t))
		for _, item := range t {
			out = append(out, enc(key)+"="+enc(fmt.Sprintf("%v", item)))
		}
		return out
	case string:
		return []string{enc(key) + "=" + enc(t)}
	default:
		return []string{enc(key) + "=" + enc(fmt.Sprintf("%v", value))}
	}
}

// upstreamErrorType maps a transport error onto the error type names of the
// Python reference (httpx exception class names).
func upstreamErrorType(err error) string {
	msg := err.Error()
	switch {
	case strings.Contains(msg, "no such host"):
		return "ConnectError"
	case strings.Contains(msg, "connection refused"):
		return "ConnectError"
	case strings.Contains(msg, "connection reset"):
		return "ReadError"
	case strings.Contains(msg, "certificate"):
		return "ConnectError"
	case strings.Contains(msg, "context canceled"):
		return "ReadError"
	}
	return "UpstreamError"
}

func truncateRunes(s string, n int) string {
	rs := []rune(s)
	if len(rs) <= n {
		return s
	}
	return string(rs[:n])
}

func truncateMsg(s string) string {
	rs := []rune(s)
	if len(rs) <= 300 {
		return s
	}
	return string(rs[:300])
}
