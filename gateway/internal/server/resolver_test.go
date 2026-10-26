package server

import (
	"context"
	"errors"
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

// The resolution runs on the search index of the registry now. Its
// semantics must not move: exact id, alias, normalised id, normalised name,
// substring hit, and the shortest name wins when several sources match.
func TestResolveSourceSemanticsOnTheSearchIndex(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	cases := []struct{ key, want, why string }{
		{"sec_edgar", "sec_edgar", "精确 id"},
		{"SEC_EDGAR", "sec_edgar", "归一 id"},
		{"doi", "crossref", "别名"},
		{"DOI", "crossref", "归一别名"},
		{"SEC EDGAR", "sec_edgar", "归一名称"},
		{"cros", "crossref", "名称包含"},
		{"s", "crossref", "多个命中时取名字最短的源"},
		{"", "", "空键"},
		{"中文源", "", "纯中文键不匹配"},
		{"nothing-like-this", "", "未知键"},
	}
	for _, c := range cases {
		if got := r.ResolveSource(c.key); got != c.want {
			t.Errorf("ResolveSource(%q) = %q, want %q (%s)", c.key, got, c.want, c.why)
		}
	}
}

// ── 源发现(search) ────────────────────────────────────────────────────

// realRegistryResolver builds a resolver on top of the registry the data
// files produce. The search only reads the registry, so no upstream is
// needed.
func realRegistryResolver(t *testing.T) *Resolver {
	t.Helper()
	cfg := config.Load()
	reg, err := registry.Build(cfg)
	if err != nil {
		t.Fatalf("registry.Build: %v", err)
	}
	logger := log.New(io.Discard, "", 0)
	return NewResolver(cfg, reg, cache.New(4, 1<<20), nil, nil, metrics.New(), ratelimit.New(1), logger)
}

// listedIDs returns the ids of a source list projection.
func listedIDs(items []any) []string {
	out := make([]string, 0, len(items))
	for _, it := range items {
		obj, ok := it.(*parser.Object)
		if !ok {
			continue
		}
		id, _ := obj.Get("id")
		if s, ok := id.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// The keyword search has to reach the canonical id, the aliases and the
// endpoint ids: a caller that can call a source by its alias must be able
// to find it as well.
func TestListSourcesFindsCanonicalIDAliasAndEndpointID(t *testing.T) {
	r := realRegistryResolver(t)
	cases := []struct {
		q      string
		wantID string
		why    string
	}{
		{"sec_edgar", "sec_edgar", "canonical id"},
		{"edgar", "sec_edgar", "alias"},
		{"szse", "szse", "id 与别名重合"},
		{"szse_stock", "szse", "只有别名"},
		{"stock_list", "szse", "端点 id"},
	}
	for _, c := range cases {
		ids := listedIDs(r.ListSources("", c.q))
		found := false
		for _, id := range ids {
			if id == c.wantID {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("ListSources(q=%q) = %v, want it to contain %q (%s)", c.q, ids, c.wantID, c.why)
		}
	}
}

// A multi-word query matches on AND: every whitespace-separated term has to
// appear in the source's searchable text. Before this, the whole string was
// one substring match, so "OFAC SDN" matched nothing even though both words
// hit — the terms simply never occur adjacent to each other.
func TestListSourcesMultiWordQueryMatchesOnAND(t *testing.T) {
	r := realRegistryResolver(t)
	cases := []struct {
		q    string
		want []string
		why  string
	}{
		{"OFAC SDN", []string{"ofac_sdn"}, "两个词分散在 id 与名称里"},
		{"World Bank", []string{"world_bank", "world_bank_open_data_wdi_wgi"}, "名称本身带空格"},
		{"stock list", []string{"cninfo", "sse", "szse"}, "三个源都叫这个端点 id"},
		{"银行 名单", []string{"idb_adb_afdb_eib", "world_bank"}, "中文按子串匹配, 不分词"},
		{"证券 交易", []string{"cftc", "sse", "sse_notice", "szse"}, "中文多词"},
		{"sanctions zzzznotarealterm", nil, "AND: 有一个词不命中就整体不命中"},
	}
	for _, c := range cases {
		ids := listedIDs(r.ListSources("", c.q))
		if len(ids) != len(c.want) {
			t.Errorf("ListSources(q=%q) = %v, want %v (%s)", c.q, ids, c.want, c.why)
			continue
		}
		got := map[string]bool{}
		for _, id := range ids {
			got[id] = true
		}
		for _, id := range c.want {
			if !got[id] {
				t.Errorf("ListSources(q=%q) = %v, want %v (%s)", c.q, ids, c.want, c.why)
				break
			}
		}
	}
}

// Whitespace alone is no filter, the same way an empty family is no filter.
func TestListSourcesWhitespaceOnlyQueryIsNoFilter(t *testing.T) {
	r := realRegistryResolver(t)
	all := listedIDs(r.ListSources("", ""))
	blank := listedIDs(r.ListSources("", "   "))
	if len(all) == 0 {
		t.Fatal("the source list is empty")
	}
	if len(blank) != len(all) {
		t.Errorf("q=\"   \" -> %d sources, q=\"\" -> %d, want the same", len(blank), len(all))
	}
}

// The keyword search is case insensitive.
func TestListSourcesKeywordIsCaseInsensitive(t *testing.T) {
	r := realRegistryResolver(t)
	lower := listedIDs(r.ListSources("", "sec_edgar"))
	upper := listedIDs(r.ListSources("", "SEC_EDGAR"))
	if len(lower) == 0 {
		t.Fatalf("ListSources(q=%q) is empty", "sec_edgar")
	}
	if len(lower) != len(upper) {
		t.Errorf("q=sec_edgar -> %v, q=SEC_EDGAR -> %v, want the same hits", lower, upper)
	}
}

// The order of the list follows the order of the registry.
func TestListSourcesKeepsTheRegistryOrder(t *testing.T) {
	r := realRegistryResolver(t)
	ids := listedIDs(r.ListSources("", ""))
	if len(ids) == 0 {
		t.Fatal("the source list is empty")
	}
	var want []string
	for _, s := range r.Registry().Sources {
		want = append(want, s.ID)
	}
	if len(ids) != len(want) {
		t.Fatalf("the list has %d sources, want %d", len(ids), len(want))
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("the list is not in registry order: %v", ids)
		}
	}
}

// The family filter keeps its semantics: it is a substring match on the
// family of the source.
func TestListSourcesFamilyFilterStillWorks(t *testing.T) {
	r := realRegistryResolver(t)
	all := listedIDs(r.ListSources("", ""))
	filtered := listedIDs(r.ListSources("证券监管", ""))
	if len(filtered) == 0 {
		t.Fatal("the family filter matched nothing")
	}
	if len(filtered) >= len(all) {
		t.Errorf("the family filter matched %d of %d sources", len(filtered), len(all))
	}
	for _, id := range filtered {
		src := r.Registry().Source(id)
		if src == nil || !strings.Contains(src.Family, "证券监管") {
			t.Errorf("the family filter let %q through", id)
		}
	}
}

// The projection carries the aliases of the source: "搜到 → 知道怎么调"
// has to close inside one response.
func TestListSourcesProjectionCarriesTheAliases(t *testing.T) {
	r := realRegistryResolver(t)
	items := r.ListSources("", "szse")
	if len(items) == 0 {
		t.Fatal("ListSources(q=szse) is empty")
	}
	obj, ok := items[0].(*parser.Object)
	if !ok {
		t.Fatalf("item = %T", items[0])
	}
	raw, ok := obj.Get("aliases")
	if !ok {
		t.Fatal("the projection has no aliases")
	}
	list, ok := raw.([]string)
	if !ok {
		t.Fatalf("aliases = %T, want a string array", raw)
	}
	hit := false
	for _, a := range list {
		if a == "szse_stock" {
			hit = true
		}
	}
	if !hit {
		t.Errorf("aliases = %v, want it to contain szse_stock", list)
	}
}

// A source without aliases gets an empty array, not a missing key: the
// caller must be able to read the field without checking for it.
func TestListSourcesProjectionAlwaysCarriesTheAliasesKey(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {})
	items := r.ListSources("", "contact_source")
	if len(items) == 0 {
		t.Fatal("ListSources(q=contact_source) is empty")
	}
	obj, ok := items[0].(*parser.Object)
	if !ok {
		t.Fatalf("item = %T", items[0])
	}
	raw, ok := obj.Get("aliases")
	if !ok {
		t.Fatal("the projection has no aliases key")
	}
	list, ok := raw.([]string)
	if !ok {
		t.Fatalf("aliases = %T, want a string array", raw)
	}
	if len(list) != 0 {
		t.Errorf("aliases = %v, want an empty array", list)
	}
}

// ── 上游返回非 JSON 正文 ───────────────────────────────────────────────

// An upstream that answers with an HTML error page is not an empty result:
// the call has to degrade and hand out the direct upstream fallback.
func TestUpstreamHTMLDegradesAsNotJSON(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte("<!DOCTYPE html>\n<html><head><title>502 Bad Gateway</title></head></html>"))
	})
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	if !env.Meta.Degraded {
		t.Fatalf("the envelope must be degraded: %+v", env.Meta.Errors)
	}
	if !hasError(env.Meta.Errors, "upstream_not_json") {
		t.Errorf("errors = %+v, want an upstream_not_json error", env.Meta.Errors)
	}
	if env.Meta.Fallback == nil {
		t.Fatal("the envelope has no fallback block")
	}
	raw, _ := env.Meta.Fallback.Get("upstream_url")
	upstreamURL, _ := raw.(string)
	if upstreamURL == "" {
		t.Errorf("fallback = %v, want a non empty upstream_url", env.Meta.Fallback)
	}
	if list, ok := env.Data.([]any); !ok || len(list) != 0 {
		t.Errorf("data = %#v, want an empty list", env.Data)
	}
}

// The message of the error has to show what the upstream answered, so that
// the caller sees that it was an error page.
func TestUpstreamNotJSONErrorCarriesTheBodyExcerpt(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("<!DOCTYPE html><html><body>Service Unavailable</body></html>"))
	})
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	var msg string
	for _, e := range env.Meta.Errors {
		if e.Type == "upstream_not_json" {
			msg = e.Message
		}
	}
	if msg == "" {
		t.Fatalf("errors = %+v, want an upstream_not_json error", env.Meta.Errors)
	}
	if !strings.Contains(msg, "<!DOCTYPE html>") {
		t.Errorf("message = %q, want the head of the body", msg)
	}
}

// newRawShapeResolver builds a resolver whose only endpoint answers with the
// given shape, so that the pass through of a body that is not JSON can be
// checked.
func newRawShapeResolver(t *testing.T, shape string) *Resolver {
	t.Helper()
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("<root><a>1</a></root>"))
	}))
	t.Cleanup(up.Close)

	cfg := config.Load()
	cfg.Contact = "test@example.com"
	cfg.BudgetMS = 500
	cfg.UpstreamBudgetMS = 400
	cfg.ReserveMS = 100
	cfg.MaxBodyBytes = 1024 * 1024
	cfg.Offline = false

	src := &registry.SourceSpec{
		ID:      "raw_source",
		Name:    "Raw Source",
		Family:  "其他",
		Aliases: []string{"raw"},
		Auth:    registry.AuthSpec{Type: "none"},
		Endpoints: []*registry.EndpointSpec{{
			ID:          "raw",
			Name:        "原始正文",
			Method:      "GET",
			URLTemplate: up.URL + "/raw",
			CacheTTLS:   900,
			BudgetMS:    400,
			Response:    registry.ResponseSpec{MediaType: "text/plain", Shape: shape},
		}},
	}
	doc := &registry.Document{
		SchemaVersion: registry.SchemaVersion,
		GeneratedAt:   "2026-10-22",
		BudgetMS:      500,
		Count:         1,
		Families:      []string{"其他"},
		Sources:       []*registry.SourceSpec{src},
	}
	logger := log.New(io.Discard, "", 0)
	return NewResolver(cfg, doc, cache.New(8, 1<<20),
		upstream.New(upstream.Options{PoolSize: 2, VerifyTLS: true, UserAgent: cfg.UserAgent}),
		parser.NewManager(context.Background(), "go", "python3", "", 1000, logger),
		metrics.New(), ratelimit.New(2), logger)
}

// The xml and the text shape are not supposed to carry JSON: their body is
// passed through and the call must not be degraded.
func TestXMLAndTextShapesAreNotDegraded(t *testing.T) {
	for _, shape := range []string{"xml", "text"} {
		r := newRawShapeResolver(t, shape)
		env := r.Query(QueryRequest{Source: "raw_source", Endpoint: "raw",
			Limit: 20, TimeoutMS: 500}, nil)
		if env.Meta.Degraded {
			t.Errorf("shape %s: the envelope must not be degraded: %+v", shape, env.Meta.Errors)
		}
		for _, e := range env.Meta.Errors {
			if e.Type == "upstream_not_json" {
				t.Errorf("shape %s: the call must not be marked as a non JSON upstream", shape)
			}
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

// A cache hit must serve the same records as the first call.
//
// The cache used to store the parse result wrapper instead of the projected
// payload, so every cache hit answered with an empty object (the wrapper has
// no exported fields). The first call looked fine, the second one looked like
// the source had no data at all.
func TestCacheHitReturnsTheSameRecords(t *testing.T) {
	r := newTestResolver(t, jsonHandler(`{"message":{"total-results":2,"items":[{"a":1,"b":2},{"a":3,"b":4}]}}`))
	req := QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}

	first := r.Query(req, nil)
	if first.Meta.Degraded {
		t.Fatalf("degraded: %+v", first.Meta.Errors)
	}
	second := r.Query(req, nil)
	if !second.Meta.Cached {
		t.Fatalf("the second call must hit the cache: %+v", second.Meta.Errors)
	}
	firstItems, ok := first.Data.([]any)
	if !ok || len(firstItems) == 0 {
		t.Fatalf("first call returned no records: %#v", first.Data)
	}
	secondItems, ok := second.Data.([]any)
	if !ok || len(secondItems) != len(firstItems) {
		t.Fatalf("cache hit lost the records: first=%#v second=%#v", first.Data, second.Data)
	}
	if second.Meta.Count != first.Meta.Count {
		t.Errorf("count = %d, want %d", second.Meta.Count, first.Meta.Count)
	}
}

// An upstream that answers 200 with an empty payload must not stay cached
// for the whole endpoint TTL: the caller would keep getting an empty shell.
func TestEmptyUpstreamResultIsNotCachedForLong(t *testing.T) {
	r := newTestResolver(t, jsonHandler(`{"message":{"total-results":0,"items":[]}}`))
	req := QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}

	r.Query(req, nil)
	// The entry must expire quickly: an empty upstream answer under load must
	// not pin an empty shell for the whole endpoint TTL.
	var found bool
	for key, entry := range r.cache.Entries() {
		if key == "" {
			continue
		}
		if cv := toCacheValue(entry.Value); cv != nil && isEmptyPayload(cv.Data) {
			found = true
			if ttl := time.Until(entry.ExpiresAt); ttl > emptyPayloadTTLSeconds*time.Second {
				t.Errorf("an empty result must use the short TTL, got %v", ttl)
			}
		}
	}
	if !found {
		t.Fatal("no empty entry in the cache, want a short lived one")
	}
}

// TestUpstreamErrorTypeVocabulary pins the shared error type vocabulary.
//
// 调用方是按 errors[].type 分支的, 所以同一个失败类别在两个平面必须给同一个
// 名字: Python 参照面在 resolver.py 的 _transport_error_type 里用同一套词表。
// 词表外的名字会漏掉调用方的分支。
func TestUpstreamErrorTypeVocabulary(t *testing.T) {
	cases := []struct {
		msg  string
		want string
	}{
		{`Get "https://x.example/y": dial tcp: lookup x.example: no such host`, "connect_error"},
		{`Get "https://x.example/y": dial tcp 1.2.3.4:443: connect: connection refused`, "connect_error"},
		{`Get "https://x.example/y": tls: failed to verify certificate: x509: unknown authority`, "connect_error"},
		{`Get "https://x.example/y": read tcp 1.2.3.4:443: connection reset by peer`, "read_error"},
		{`Get "https://x.example/y": EOF`, "read_error"},
		{`Get "https://x.example/y": unexpected EOF`, "read_error"},
		{`Get "https://x.example/y": server closed idle connection`, "read_error"},
		{`Get "https://x.example/y": some other transport failure`, "upstream_error"},
	}
	for _, c := range cases {
		got := upstreamErrorType(errors.New(c.msg))
		if got != c.want {
			t.Errorf("upstreamErrorType(%q) = %q, want %q", c.msg, got, c.want)
		}
	}
}

// 多词查询按 AND 匹配: 空格分隔的每个词都要出现在源的搜索文本里。
//
// 以前是整串子串匹配, 于是 "OFAC SDN"、"银行 名单" 这类查询一个都不命中 ——
// 明明每个词分别都能命中。这是搜索广度上的一个实打实的缺口: 调用方写下两个
// 都合理的词, 却拿到空结果。
func TestListSourcesMultipleTermsMatchWithAnd(t *testing.T) {
	r := realRegistryResolver(t)

	// 这几组都是**实测**共现的词对(在真实注册表上验过命中数), 不是猜的。
	for _, c := range []struct{ q, wantID string }{
		{"sec edgar", "sec_edgar"},
		{"szse stock", "szse"},
		{"OFAC SDN", "ofac_sdn"},
		{"World Bank", "world_bank"},
		{"stock list", "sse"},
	} {
		ids := listedIDs(r.ListSources("", c.q))
		if len(ids) == 0 {
			t.Errorf("ListSources(q=%q) is empty —— 多词查询应当按 AND 命中", c.q)
			continue
		}
		if c.wantID != "" {
			found := false
			for _, id := range ids {
				if id == c.wantID {
					found = true
					break
				}
			}
			if !found {
				t.Errorf("ListSources(q=%q) = %v, want it to contain %q", c.q, ids, c.wantID)
			}
		}
	}
}

// AND 语义不能放宽成"包含任意一个词": 那会把真正相关的源挤出扇出上限。
func TestListSourcesMultipleTermsRequireEveryTerm(t *testing.T) {
	r := realRegistryResolver(t)
	// "sanctions" 命中很多源, 但 "zzzznotarealterm" 哪个源都不含 ——
	// 所以 AND 的结果必须为空。
	ids := listedIDs(r.ListSources("", "sanctions zzzznotarealterm"))
	if len(ids) != 0 {
		t.Errorf("ListSources(q=%q) = %v, want no hits (AND 要求每个词都命中)",
			"sanctions zzzznotarealterm", ids)
	}
}

// 单词查询的行为不能变(这是兼容性底线)。
func TestListSourcesSingleTermIsUnchanged(t *testing.T) {
	r := realRegistryResolver(t)
	for _, q := range []string{"sec_edgar", "edgar", "szse", "stock_list", "sanctions"} {
		if len(listedIDs(r.ListSources("", q))) == 0 {
			t.Errorf("ListSources(q=%q) is empty —— 单词查询的行为不能变", q)
		}
	}
}

// 空白字符视作没有筛选(与 family 的空值同义), 而不是"搜一个空格"。
func TestListSourcesBlankQueryIsNoFilter(t *testing.T) {
	r := realRegistryResolver(t)
	blank := len(listedIDs(r.ListSources("", "   ")))
	all := len(listedIDs(r.ListSources("", "")))
	if blank != all {
		t.Errorf("空白 q 命中 %d 个源, 空 q 命中 %d 个 —— 空白应视作没有筛选", blank, all)
	}
}
