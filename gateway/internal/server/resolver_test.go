package server

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/cache"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/envelope"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/metrics"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/ratelimit"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/registry"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/upstream"
)

// testRegistry builds a small registry whose endpoints point at the test
// upstream.
func testRegistry(baseURL string) *registry.Document {
	crossref := &registry.SourceSpec{
		ID:      "crossref",
		Name:    "Crossref",
		Family:  "学术出版",
		Aliases: []string{"doi"},
		Auth:    registry.AuthSpec{Type: "none"},
		Endpoints: []*registry.EndpointSpec{{
			ID:          "works",
			Name:        "DOI 检索",
			Method:      "GET",
			URLTemplate: baseURL + "/works?filter={filter}&rows={size}&mailto={email}",
			Params: []registry.ParamSpec{
				{Name: "filter", In: "query", Type: "string", Required: true, Example: "from-index-date:2026-10-21"},
				{Name: "size", In: "query", Type: "int", Default: 10},
				// The real crossref endpoint does not mark the mailto as
				// required: an unset placeholder has to be dropped.
				{Name: "email", In: "query", Type: "string", Example: "you@example.com"},
			},
			CacheTTLS: 900,
			BudgetMS:  400,
			Response: registry.ResponseSpec{
				MediaType:  "application/json",
				Shape:      "object",
				ItemPath:   "message.items",
				TotalField: "message.total-results",
				ItemShape:  "list",
			},
		}},
	}
	sec := &registry.SourceSpec{
		ID:      "sec_edgar",
		Name:    "SEC EDGAR",
		Family:  "证券监管",
		Aliases: []string{"sec", "edgar"},
		Auth:    registry.AuthSpec{Type: "none"},
		Endpoints: []*registry.EndpointSpec{{
			ID:          "submissions",
			Name:        "公司申报记录",
			Method:      "GET",
			URLTemplate: baseURL + "/submissions/CIK{cik}.json",
			Params: []registry.ParamSpec{
				{Name: "cik", In: "path", Type: "string", Required: true, Example: "0000320193"},
			},
			CacheTTLS: 900,
			BudgetMS:  400,
			Response: registry.ResponseSpec{
				MediaType: "application/json",
				Shape:     "object",
				ItemPath:  "filings.recent.accessionNumber",
			},
		}},
	}
	contact := &registry.SourceSpec{
		ID:                "contact_source",
		Name:              "需要邮箱的源",
		Family:            "其他",
		UARequiresContact: true,
		Endpoints: []*registry.EndpointSpec{{
			ID:          "entry",
			Name:        "入口",
			Method:      "GET",
			URLTemplate: baseURL + "/entry",
			CacheTTLS:   900,
			BudgetMS:    400,
			Response:    registry.ResponseSpec{MediaType: "application/json", Shape: "object"},
		}},
	}
	return &registry.Document{
		SchemaVersion: "1.0",
		GeneratedAt:   "2026-10-22",
		BudgetMS:      500,
		Count:         3,
		Families:      []string{"学术出版", "其他", "证券监管"},
		Sources:       []*registry.SourceSpec{crossref, sec, contact},
	}
}

func newTestResolver(t *testing.T, handler http.HandlerFunc, opts ...func(*config.Config)) *Resolver {
	t.Helper()
	up := httptest.NewServer(handler)
	t.Cleanup(up.Close)

	cfg := config.Load()
	cfg.Contact = "test@example.com"
	cfg.BudgetMS = 500
	cfg.UpstreamBudgetMS = 400
	cfg.ReserveMS = 100
	cfg.MaxBodyBytes = 1024 * 1024
	cfg.WarmBudgetMS = 1000
	cfg.Offline = false
	for _, o := range opts {
		o(cfg)
	}
	logger := log.New(io.Discard, "", 0)
	return NewResolver(
		cfg,
		testRegistry(up.URL),
		cache.New(32, 1<<20),
		upstream.New(upstream.Options{PoolSize: 4, VerifyTLS: true, UserAgent: cfg.UserAgent}),
		parser.NewManager(context.Background(), "go", "python3", "", 1000, logger),
		metrics.New(),
		ratelimit.New(4),
		logger,
	)
}

func params(kv ...any) *parser.Object {
	o := parser.NewObject()
	for i := 0; i+1 < len(kv); i += 2 {
		key, ok := kv[i].(string)
		if !ok {
			continue
		}
		o.Set(key, kv[i+1])
	}
	return o
}

func endpointOf(t *testing.T, r *Resolver, source, endpoint string) *registry.EndpointSpec {
	t.Helper()
	src := r.reg.Source(source)
	if src == nil {
		t.Fatalf("source %q not found", source)
	}
	ep := src.Endpoint(endpoint)
	if ep == nil {
		t.Fatalf("endpoint %q not found", endpoint)
	}
	return ep
}

// ── URL rendering ────────────────────────────────────────────────────

func TestRenderPathParamKeepsSlash(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "sec_edgar", "submissions")
	url, err := r.Render(ep, QueryRequest{Source: "sec_edgar", Endpoint: "submissions",
		Params: params("cik", "0000320193")}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(url, "/submissions/CIK0000320193.json") {
		t.Errorf("url = %q", url)
	}
}

func TestRenderEncodesQueryParam(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "crossref", "works")
	url, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "from-index-date:2026-10-21", "email", "a@b.com")}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(url, "filter=from-index-date%3A2026-10-21") {
		t.Errorf("url = %q", url)
	}
}

func TestRenderNormalisesPageSize(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "crossref", "works")
	a, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x", "email", "a@b.com", "size", 3)}, 25)
	if err != nil {
		t.Fatal(err)
	}
	b, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x", "email", "a@b.com", "size", 50)}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if a != b {
		t.Errorf("page size not normalised:\n%s\n%s", a, b)
	}
	if !strings.Contains(a, "rows=25") {
		t.Errorf("url = %q, want rows=25", a)
	}
}

func TestRenderStripsCacheBuster(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "crossref", "works")
	url, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x", "email", "a@b.com", "_cb", "abc")}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(url, "_cb") {
		t.Errorf("url = %q, the cache buster must not be sent upstream", url)
	}
}

func TestRenderFillsEmailFromContact(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "crossref", "works")
	url, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x")}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(url, "mailto=test%40example.com") {
		t.Errorf("url = %q, want the contact address", url)
	}
}

func TestRenderMissingRequiredParam(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "crossref", "works")
	_, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params()}, 25)
	if err == nil {
		t.Fatal("expected an error")
	}
	if _, ok := err.(*MissingParamError); !ok {
		t.Errorf("error = %T, want *MissingParamError", err)
	}
}

func TestRenderDropsEmptyQueryValues(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "crossref", "works")
	url, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x", "email", "a@b.com", "empty", "")}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(url, "empty") {
		t.Errorf("url = %q, empty values must be dropped", url)
	}
}

// ── cache key ────────────────────────────────────────────────────────

func TestCacheKeyFormat(t *testing.T) {
	p := params("filter", "x", "email", "a@b.com", "size", 3, "_cb", "zz")
	key := CacheKey("crossref", "works", "https://example.org/works", p, 25)
	// The email is excluded from the key and the page size is normalised.
	want := "crossref|works|https://example.org/works|[('_cb', 'zz'), ('filter', 'x'), ('size', 25)]"
	if key != want {
		t.Errorf("key  = %s\nwant = %s", key, want)
	}
}

func TestCacheKeyIgnoresEmailAndNormalisesPageSize(t *testing.T) {
	a := CacheKey("s", "e", "u", params("size", 3, "email", "a@b.com"), 25)
	b := CacheKey("s", "e", "u", params("size", 50, "email", "x@y.com"), 25)
	if a != b {
		t.Errorf("keys differ:\n%s\n%s", a, b)
	}
}

// ── source resolution ────────────────────────────────────────────────

func TestResolveSourceOrder(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	cases := []struct{ key, want string }{
		{"crossref", "crossref"},        // exact id
		{"doi", "crossref"},             // alias
		{"CROSSREF", "crossref"},        // normalised id
		{"Crossref", "crossref"},        // normalised name
		{"sec", "sec_edgar"},            // alias
		{"edgar", "sec_edgar"},          // alias
		{"SEC EDGAR", "sec_edgar"},      // normalised name
		{"", ""},                        // empty
		{"中文源", ""},                    // no ASCII: no fuzzy match
		{"nothing-like-this", ""},       // unknown
	}
	for _, c := range cases {
		if got := r.ResolveSource(c.key); got != c.want {
			t.Errorf("ResolveSource(%q) = %q, want %q", c.key, got, c.want)
		}
	}
}

// ── query pipeline ───────────────────────────────────────────────────

func jsonHandler(payload string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(payload))
	}
}

func TestQueryProjectsAndCaches(t *testing.T) {
	calls := 0
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		calls++
		_, _ = w.Write([]byte(`{"message":{"total-results":1726791,"items":[{"DOI":"10.1/0","title":"t0"}]}}`))
	})
	req := QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}
	env := r.Query(req, nil)
	if env.Meta.Degraded {
		t.Fatalf("degraded: %+v", env.Meta.Errors)
	}
	if env.Meta.Total == nil || *env.Meta.Total != 1726791 {
		t.Errorf("total = %v", env.Meta.Total)
	}
	if env.Meta.Count != 1 {
		t.Errorf("count = %d", env.Meta.Count)
	}
	if !env.Meta.Truncated {
		t.Error("truncated must be true when the total is bigger than the list")
	}
	if env.Meta.Cached {
		t.Error("the first call must not be a cache hit")
	}
	// The second call is served from the cache.
	env2 := r.Query(req, nil)
	if !env2.Meta.Cached {
		t.Errorf("the second call must hit the cache: %+v", env2.Meta.Errors)
	}
	if calls != 1 {
		t.Errorf("upstream calls = %d, want 1", calls)
	}
}

func TestQueryProjectsFields(t *testing.T) {
	r := newTestResolver(t, jsonHandler(`{"message":{"total-results":1,"items":[{"a":1,"b":2,"c":3}]}}`))
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Fields: []string{"a"}, Limit: 5, TimeoutMS: 500}, nil)
	if env.Meta.Degraded {
		t.Fatalf("degraded: %+v", env.Meta.Errors)
	}
	list, ok := env.Data.([]any)
	if !ok || len(list) != 1 {
		t.Fatalf("data = %#v", env.Data)
	}
	row, ok := list[0].(*parser.Object)
	if !ok {
		t.Fatalf("row = %T", list[0])
	}
	if row.Len() != 1 {
		t.Errorf("row = %v, want only the projected field", row.Keys())
	}
}

func TestStaleWhileRevalidateReturnsStaleValue(t *testing.T) {
	r := newTestResolver(t, jsonHandler(`{"message":{"total-results":1,"items":[{"a":1}]}}`))
	req := QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}
	env := r.Query(req, nil)
	if env.Meta.Degraded {
		t.Fatalf("degraded: %+v", env.Meta.Errors)
	}
	// Expire the entry.
	key := CacheKey("crossref", "works", env.Meta.Upstream.URL, req.Params, 25)
	r.cache.Put(key, &cacheValue{Data: mustItems(t, env), Status: 200, Total: intPtr(1)}, 0)
	time.Sleep(1100 * time.Millisecond)

	env2 := r.Query(req, nil)
	if !env2.Meta.Cached {
		t.Error("the stale entry must still be served from the cache")
	}
	if !env2.Meta.Degraded {
		t.Error("a stale response must be degraded")
	}
	if !hasError(env2.Meta.Errors, "stale") {
		t.Errorf("errors = %+v, want a stale error", env2.Meta.Errors)
	}
	if list, ok := env2.Data.([]any); !ok || len(list) != 1 {
		t.Errorf("data = %#v, want the stale value", env2.Data)
	}
}

func TestDegradedEnvelopeOnUpstreamError(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("upstream is unhappy"))
	})
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	if !env.Meta.Degraded {
		t.Error("the envelope must be degraded")
	}
	if !hasError(env.Meta.Errors, "upstream_error") {
		t.Errorf("errors = %+v", env.Meta.Errors)
	}
	if env.Meta.Upstream.Status == nil || *env.Meta.Upstream.Status != 500 {
		t.Errorf("upstream status = %v, want 500", env.Meta.Upstream.Status)
	}
}

func TestDegradedEnvelopeOnTimeout(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(600 * time.Millisecond)
		_, _ = w.Write([]byte(`{"message":{"total-results":0,"items":[]}}`))
	}, func(c *config.Config) { c.UpstreamBudgetMS = 120 })
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	if !env.Meta.Degraded {
		t.Error("the envelope must be degraded")
	}
	if !hasError(env.Meta.Errors, "timeout") {
		t.Errorf("errors = %+v, want a timeout error", env.Meta.Errors)
	}
	if env.Meta.Fallback == nil || env.Meta.Fallback.Len() == 0 {
		t.Error("a timeout must carry the fallback block")
	}
}

func TestDegradedEnvelopeOnOversizedBody(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(strings.Repeat("x", 5000)))
	}, func(c *config.Config) { c.MaxBodyBytes = 100 })
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	if !env.Meta.Degraded {
		t.Error("the envelope must be degraded")
	}
	if !hasError(env.Meta.Errors, "response_too_large") {
		t.Errorf("errors = %+v, want a response_too_large error", env.Meta.Errors)
	}
}

func TestContactRequiredDegrades(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {},
		func(c *config.Config) { c.Contact = "" })
	env := r.Query(QueryRequest{Source: "contact_source", Endpoint: "entry",
		Limit: 20, TimeoutMS: 500}, nil)
	if !env.Meta.Degraded {
		t.Error("the envelope must be degraded")
	}
	if !hasError(env.Meta.Errors, "contact_required") {
		t.Errorf("errors = %+v", env.Meta.Errors)
	}
}

func TestOfflineModeNeverCallsUpstream(t *testing.T) {
	calls := 0
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		calls++
		_, _ = w.Write([]byte(`{"message":{"total-results":0,"items":[]}}`))
	}, func(c *config.Config) { c.Offline = true })
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	if calls != 0 {
		t.Errorf("upstream calls = %d, want 0 in offline mode", calls)
	}
	if !env.Meta.Offline || !env.Meta.Degraded {
		t.Errorf("meta = %+v, want offline and degraded", env.Meta)
	}
	if !hasError(env.Meta.Errors, "offline") {
		t.Errorf("errors = %+v, want an offline error", env.Meta.Errors)
	}
}

func TestUnknownSourceEnvelope(t *testing.T) {
	r := newTestResolver(t, jsonHandler(`{}`))
	env := r.Query(QueryRequest{Source: "nope", Endpoint: "works", Limit: 20, TimeoutMS: 500}, nil)
	if !hasError(env.Meta.Errors, "not_found") {
		t.Errorf("errors = %+v, want a not_found error", env.Meta.Errors)
	}
}

// ── helpers ──────────────────────────────────────────────────────────

func hasError(errs []envelope.Error, typ string) bool {
	for _, e := range errs {
		if e.Type == typ {
			return true
		}
	}
	return false
}

func intPtr(n int) *int { return &n }

func mustItems(t *testing.T, env *envelope.Envelope) any {
	t.Helper()
	return env.Data
}

// TestRenderDropsUnsetPlaceholders checks that a placeholder without a
// value renders as an empty value and is dropped from the query string.
func TestRenderDropsUnsetPlaceholders(t *testing.T) {
	// Without a contact address the mailto placeholder stays empty and the
	// empty query value is dropped, exactly like the reference.
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {},
		func(c *config.Config) { c.Contact = "" })
	ep := endpointOf(t, r, "crossref", "works")
	url, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x")}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(url, "nil") {
		t.Errorf("url = %q, an unset placeholder must not render as <nil>", url)
	}
	if strings.Contains(url, "mailto") {
		t.Errorf("url = %q, the empty mailto must be dropped", url)
	}
	if !strings.Contains(url, "filter=x") || !strings.Contains(url, "rows=25") {
		t.Errorf("url = %q", url)
	}
}
