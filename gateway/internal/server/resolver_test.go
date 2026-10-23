package server

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	neturl "net/url"
	"strings"
	"testing"
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
	// A mirror style endpoint: the target of the query is a parameter of
	// the upstream URL. scripts/scaffold_source.py can generate such a
	// template, so the "url" placeholder is reachable through the
	// documented onboarding flow.
	mirror := &registry.SourceSpec{
		ID:      "mirror_source",
		Name:    "镜像源",
		Family:  "其他",
		Aliases: []string{"mirror"},
		Auth:    registry.AuthSpec{Type: "none"},
		Endpoints: []*registry.EndpointSpec{{
			ID:          "proxy",
			Name:        "代理查询",
			Method:      "GET",
			URLTemplate: baseURL + "/proxy?url={url}&rows={size}",
			Params: []registry.ParamSpec{
				{Name: "url", In: "query", Type: "string", Required: true, Example: "https://example.org/data"},
				{Name: "size", In: "query", Type: "int", Default: 10},
			},
			CacheTTLS: 900,
			BudgetMS:  400,
			Response: registry.ResponseSpec{
				MediaType: "application/json",
				Shape:     "object",
				ItemPath:  "items",
			},
		}},
	}
	return &registry.Document{
		SchemaVersion: "1.0",
		GeneratedAt:   "2026-10-22",
		BudgetMS:      500,
		Count:         4,
		Families:      []string{"学术出版", "其他", "证券监管"},
		Sources:       []*registry.SourceSpec{crossref, sec, contact, mirror},
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
	ep := &registry.EndpointSpec{
		ID:          "x",
		Method:      "GET",
		URLTemplate: "https://example.org/?q={needed}",
		Params: []registry.ParamSpec{
			{Name: "needed", In: "query", Type: "string", Required: true},
		},
	}
	_, err := r.Render(ep, QueryRequest{Source: "x", Endpoint: "x", Params: params()}, 25)
	if err == nil {
		t.Fatal("expected an error")
	}
	if _, ok := err.(*MissingParamError); !ok {
		t.Errorf("error = %T, want *MissingParamError", err)
	}
}

// A required parameter falls back to the example the endpoint declares:
// that example is the answer to "how do I call this endpoint". Without it
// a plain params:{} call could not reach most of the endpoints.
func TestRenderFallsBackToExampleForRequiredParam(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "crossref", "works")
	url, err := r.Render(ep, QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params()}, 25)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if !strings.Contains(url, "filter=from-index-date") {
		t.Errorf("url = %q, want the example filter", url)
	}
}

// The credential precedence is: caller value > environment variable >
// default the endpoint declares.
func TestRenderCredentialPrecedence(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := &registry.EndpointSpec{
		ID:          "x",
		Method:      "GET",
		URLTemplate: "https://example.org/?k={api_key}",
		Params: []registry.ParamSpec{
			{Name: "api_key", In: "query", Type: "string", Default: "DEFAULT_KEY", Env: "A_GATEWAY_TEST_KEY"},
		},
	}
	url, err := r.Render(ep, QueryRequest{Source: "x", Endpoint: "x", Params: params()}, 25)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if !strings.Contains(url, "k=DEFAULT_KEY") {
		t.Errorf("url = %q, want the declared default", url)
	}

	t.Setenv("A_GATEWAY_TEST_KEY", "ENV_KEY")
	url, err = r.Render(ep, QueryRequest{Source: "x", Endpoint: "x", Params: params()}, 25)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if !strings.Contains(url, "k=ENV_KEY") {
		t.Errorf("url = %q, want the environment value", url)
	}

	p := parser.NewObject()
	p.Set("api_key", "CALLER_KEY")
	url, err = r.Render(ep, QueryRequest{Source: "x", Endpoint: "x", Params: p}, 25)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if !strings.Contains(url, "k=CALLER_KEY") {
		t.Errorf("url = %q, want the caller value", url)
	}
}

// A header whose value references an environment variable that is not set is
// dropped: a half filled credential is worse than no header at all.
func TestExpandEnvRefDropsHeaderWhenTheVariableIsUnset(t *testing.T) {
	if _, keep := expandEnvRef("Bearer ${A_GATEWAY_TEST_MISSING_TOKEN}"); keep {
		t.Error("a header with an unset variable must be dropped")
	}
	if _, keep := expandEnvRef("Bearer ${A_GATEWAY_TEST_MISSING_TOKEN} "); keep {
		t.Error("a header with an unset variable must be dropped")
	}
	t.Setenv("A_GATEWAY_TEST_TOKEN", "tok")
	got, keep := expandEnvRef("Bearer ${A_GATEWAY_TEST_TOKEN}")
	if !keep || got != "Bearer tok" {
		t.Errorf("expandEnvRef = %q, %v; want \"Bearer tok\", true", got, keep)
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

// TestRenderEncodesTheURLPlaceholder checks that the value of the "url"
// placeholder cannot smuggle a query parameter (or a fragment) into the
// upstream URL.
//
// The host of the template is fixed, so the value can only attack the query
// string: without the encoding it could add a parameter ("&admin=true") or
// cut the query off ("#").
func TestRenderEncodesTheURLPlaceholder(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "mirror_source", "proxy")
	raw := "http://169.254.169.254/latest/meta-data/&admin=true#frag"
	url, err := r.Render(ep, QueryRequest{Source: "mirror_source", Endpoint: "proxy",
		Params: params("url", raw)}, 25)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := neturl.Parse(url)
	if err != nil {
		t.Fatalf("the rendered URL is not a valid URL: %v (%s)", err, url)
	}
	if parsed.Fragment != "" {
		t.Errorf("url = %q, the fragment must be encoded", url)
	}
	q := parsed.Query()
	if got := q.Get("admin"); got != "" {
		t.Errorf("url = %q, the value introduced the parameter admin=%q", url, got)
	}
	if len(q) != 2 {
		t.Errorf("url = %q, the query has %d parameters, want url and rows", url, len(q))
	}
	if got := q.Get("url"); got != raw {
		t.Errorf("url = %q, the url parameter is %q, want %q", url, got, raw)
	}
	if !strings.Contains(url, "url=http%3A%2F%2F169.254.169.254%2Flatest%2Fmeta-data%2F%26admin%3Dtrue%23frag") {
		t.Errorf("url = %q, the value must be percent encoded", url)
	}
}

// TestRenderKeepsThePathSlashesOfPathParams is the counterpart of the test
// above: a path parameter keeps its slashes.
func TestRenderKeepsThePathSlashesOfPathParams(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	ep := endpointOf(t, r, "sec_edgar", "submissions")
	url, err := r.Render(ep, QueryRequest{Source: "sec_edgar", Endpoint: "submissions",
		Params: params("cik", "0000320193/extra")}, 25)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(url, "/submissions/CIK0000320193/extra.json") {
		t.Errorf("url = %q, a path parameter keeps its slashes", url)
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
		{"crossref", "crossref"},   // exact id
		{"doi", "crossref"},        // alias
		{"CROSSREF", "crossref"},   // normalised id
		{"Crossref", "crossref"},   // normalised name
		{"sec", "sec_edgar"},       // alias
		{"edgar", "sec_edgar"},     // alias
		{"SEC EDGAR", "sec_edgar"}, // normalised name
		{"", ""},                   // empty
		{"中文源", ""},                // no ASCII: no fuzzy match
		{"nothing-like-this", ""},  // unknown
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
