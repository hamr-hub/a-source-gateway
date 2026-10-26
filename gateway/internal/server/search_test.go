package server

import (
	"encoding/json"
	"testing"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
)

// offlineServer builds a server that never talks to an upstream.
//
// The search tests must pass on a machine without network access (CI): the
// offline mode answers from the metadata and the cache only, so no request
// leaves the box and the answers are deterministic.
func offlineServer(t *testing.T) *Server {
	t.Helper()
	t.Setenv("A_GATEWAY_OFFLINE", "1")
	return realServer(t)
}

// searchDoc is the answer of POST /v1/search, decoded for the assertions.
type searchDoc struct {
	Meta struct {
		Requested     int    `json:"requested"`
		OK            int    `json:"ok"`
		Degraded      int    `json:"degraded"`
		Failed        int    `json:"failed"`
		WithData      int    `json:"with_data"`
		BudgetMS      int    `json:"budget_ms"`
		LatencyMS     int    `json:"latency_ms"`
		TraceID       string `json:"trace_id"`
		SchemaVersion string `json:"schema_version"`
	} `json:"meta"`
	Queries []string         `json:"queries"`
	Results []map[string]any `json:"results"`
}

// decodeSearch decodes the answer of a search request.
func decodeSearch(t *testing.T, body []byte) searchDoc {
	t.Helper()
	var doc searchDoc
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("响应不是合法 JSON: %v (%s)", err, body)
	}
	return doc
}

// mustSearchBody decodes a request body the way the handler does.
func mustSearchBody(t *testing.T, raw string) *parser.Object {
	t.Helper()
	v, err := parser.UnmarshalOrdered([]byte(raw))
	if err != nil {
		t.Fatalf("请求体不是合法 JSON: %v", err)
	}
	obj, ok := v.(*parser.Object)
	if !ok {
		t.Fatalf("请求体不是对象")
	}
	return obj
}

// TestSearchExplicitFanout covers the "queries" form: one request, one
// source, one endpoint — and the shape of the answer.
func TestSearchExplicitFanout(t *testing.T) {
	s := offlineServer(t)
	rec, body := do(t, s, "POST", "/v1/search",
		`{"queries":[{"source":"crossref","endpoint":"works"}],"limit":5}`)
	if rec.Code != 200 {
		t.Fatalf("POST /v1/search = %d, want 200: %s", rec.Code, body)
	}
	doc := decodeSearch(t, body)
	if doc.Meta.Requested != 1 {
		t.Errorf("meta.requested = %d, want 1", doc.Meta.Requested)
	}
	if len(doc.Results) != 1 {
		t.Fatalf("results = %d 条, want 1: %s", len(doc.Results), body)
	}
	if len(doc.Queries) != 1 || doc.Queries[0] != "crossref/works" {
		t.Errorf("queries = %v, want [crossref/works]", doc.Queries)
	}
	res := doc.Results[0]
	for _, key := range []string{"source", "endpoint", "degraded", "cached",
		"count", "total", "data", "fields", "errors"} {
		if _, ok := res[key]; !ok {
			t.Errorf("results[0] 缺 %q", key)
		}
	}
	if res["source"] != "crossref" {
		t.Errorf("results[0].source = %v, want crossref", res["source"])
	}
	if res["endpoint"] != "works" {
		t.Errorf("results[0].endpoint = %v, want works", res["endpoint"])
	}
	if _, ok := res["errors"].([]any); !ok {
		t.Errorf("results[0].errors = %#v, want an array", res["errors"])
	}
	// 上游是否可达取决于运行环境, 所以这里只断言 errors 是一个数组,
	// 不断言它是空的。
	if _, ok := res["degraded"].(bool); !ok {
		t.Errorf("results[0].degraded = %#v, want a bool", res["degraded"])
	}
	if _, ok := res["cached"].(bool); !ok {
		t.Errorf("results[0].cached = %#v, want a bool", res["cached"])
	}
	// requested = ok + degraded + failed: 三类互斥。
	if doc.Meta.Requested != doc.Meta.OK+doc.Meta.Degraded+doc.Meta.Failed {
		t.Errorf("meta.requested = %d, want ok+degraded+failed = %d",
			doc.Meta.Requested, doc.Meta.OK+doc.Meta.Degraded+doc.Meta.Failed)
	}
}

// TestSearchUnknownSourceKeepsTheRequestGreen checks that a broken source
// does not break the request: the answer is still 200, the failure is
// reported inside the entry of that source.
func TestSearchUnknownSourceKeepsTheRequestGreen(t *testing.T) {
	s := offlineServer(t)
	rec, body := do(t, s, "POST", "/v1/search",
		`{"queries":[{"source":"__no_such_source__","endpoint":"x"}]}`)
	if rec.Code != 200 {
		t.Fatalf("POST /v1/search = %d, want 200: %s", rec.Code, body)
	}
	doc := decodeSearch(t, body)
	if doc.Meta.Requested != 1 {
		t.Errorf("meta.requested = %d, want 1", doc.Meta.Requested)
	}
	if doc.Meta.Failed != 1 {
		t.Errorf("meta.failed = %d, want 1", doc.Meta.Failed)
	}
	if doc.Meta.OK != 0 {
		t.Errorf("meta.ok = %d, want 0", doc.Meta.OK)
	}
	if len(doc.Results) != 1 {
		t.Fatalf("results = %d 条, want 1: %s", len(doc.Results), body)
	}
	errs, ok := doc.Results[0]["errors"].([]any)
	if !ok || len(errs) == 0 {
		t.Fatalf("results[0].errors = %#v, want at least one error", doc.Results[0]["errors"])
	}
	first, ok := errs[0].(map[string]any)
	if !ok {
		t.Fatalf("results[0].errors[0] = %#v, want an object", errs[0])
	}
	if first["type"] != "not_found" {
		t.Errorf("results[0].errors[0].type = %v, want not_found", first["type"])
	}
}

// TestSearchValidationErrors covers the 422 contract: the detail list carries
// the FastAPI shape (type / loc / msg / input) and a nested field is one
// single location segment ("queries.0.params").
func TestSearchValidationErrors(t *testing.T) {
	s := offlineServer(t)
	cases := []struct {
		name string
		body string
		typ  string
		loc  []string
	}{
		{"body 不是合法 JSON", `{`, "json_invalid", []string{"body"}},
		{"body 不是对象", `[]`, "model_attributes_type", []string{"body"}},
		{"queries 不是数组", `{"queries":{}}`, "list_type", []string{"body", "queries"}},
		{"queries[0] 不是对象", `{"queries":[1]}`, "dict_type", []string{"body", "queries.0"}},
		{"缺 source", `{"queries":[{}]}`, "missing", []string{"body", "queries.0.source"}},
		{"source 不是字符串", `{"queries":[{"source":1}]}`, "string_type", []string{"body", "queries.0.source"}},
		{"endpoint 不是字符串", `{"queries":[{"source":"crossref","endpoint":1}]}`,
			"string_type", []string{"body", "queries.0.endpoint"}},
		{"params 不是对象", `{"queries":[{"source":"crossref","params":[]}]}`,
			"dict_type", []string{"body", "queries.0.params"}},
		{"fields 不是数组", `{"queries":[{"source":"crossref","fields":{}}]}`,
			"list_type", []string{"body", "queries.0.fields"}},
		{"fields[0] 不是字符串", `{"queries":[{"source":"crossref","fields":[1]}]}`,
			"string_type", []string{"body", "queries.0.fields.0"}},
		{"queries[].limit 不是整数", `{"queries":[{"source":"crossref","limit":"x"}]}`,
			"int_type", []string{"body", "queries.0.limit"}},
		{"顶层 limit 不是整数", `{"queries":[{"source":"crossref"}],"limit":"x"}`,
			"int_type", []string{"body", "limit"}},
		{"顶层 max_sources 不是整数", `{"q":"sec","max_sources":"x"}`,
			"int_type", []string{"body", "max_sources"}},
		{"q 不是字符串", `{"q":1}`, "string_type", []string{"body", "q"}},
		{"既没有 queries 也没有 q", `{}`, "missing", []string{"body", "queries"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec, body := do(t, s, "POST", "/v1/search", tc.body)
			if rec.Code != 422 {
				t.Fatalf("POST /v1/search = %d, want 422: %s", rec.Code, body)
			}
			var doc struct {
				Detail []struct {
					Type string   `json:"type"`
					Loc  []string `json:"loc"`
					Msg  string   `json:"msg"`
				} `json:"detail"`
			}
			if err := json.Unmarshal(body, &doc); err != nil {
				t.Fatalf("响应不是合法 JSON: %v (%s)", err, body)
			}
			if len(doc.Detail) != 1 {
				t.Fatalf("detail = %d 条, want 1: %s", len(doc.Detail), body)
			}
			d := doc.Detail[0]
			if d.Type != tc.typ {
				t.Errorf("detail[0].type = %q, want %q", d.Type, tc.typ)
			}
			if len(d.Loc) != len(tc.loc) {
				t.Fatalf("detail[0].loc = %v, want %v", d.Loc, tc.loc)
			}
			for i, want := range tc.loc {
				if d.Loc[i] != want {
					t.Errorf("detail[0].loc[%d] = %q, want %q", i, d.Loc[i], want)
				}
			}
			if d.Msg == "" {
				t.Error("detail[0].msg 是空的")
			}
		})
	}
}

// TestSearchKeywordFanout covers the "q" form: the keyword selects the
// sources with the same index GET /v1/sources?q= uses, and max_sources caps
// how many of them are queried.
func TestSearchKeywordFanout(t *testing.T) {
	s := offlineServer(t)
	rec, body := do(t, s, "POST", "/v1/search", `{"q":"sec","max_sources":2}`)
	if rec.Code != 200 {
		t.Fatalf("POST /v1/search = %d, want 200: %s", rec.Code, body)
	}
	doc := decodeSearch(t, body)
	if doc.Meta.Requested > 2 {
		t.Errorf("meta.requested = %d, want <= 2 (max_sources)", doc.Meta.Requested)
	}
	if len(doc.Results) != doc.Meta.Requested {
		t.Errorf("results = %d 条, want meta.requested = %d", len(doc.Results), doc.Meta.Requested)
	}
	if len(doc.Queries) != doc.Meta.Requested {
		t.Errorf("queries = %d 条, want meta.requested = %d", len(doc.Queries), doc.Meta.Requested)
	}
	if doc.Meta.Requested < 1 {
		t.Errorf("meta.requested = %d, want >= 1 (关键词 \"sec\" 至少要选出一个源)", doc.Meta.Requested)
	}
	for i, res := range doc.Results {
		src, _ := res["source"].(string)
		ep, _ := res["endpoint"].(string)
		if src == "" || ep == "" {
			t.Errorf("results[%d] 的 source/endpoint 是空的: %v", i, res)
			continue
		}
		if want := src + "/" + ep; doc.Queries[i] != want {
			t.Errorf("queries[%d] = %q, want %q", i, doc.Queries[i], want)
		}
	}
}

// TestSearchPrefersQueriesOverTheKeyword checks that the explicit list wins
// when both forms are present.
func TestSearchPrefersQueriesOverTheKeyword(t *testing.T) {
	s := offlineServer(t)
	rec, body := do(t, s, "POST", "/v1/search",
		`{"queries":[{"source":"crossref","endpoint":"works"}],"q":"sec","max_sources":2}`)
	if rec.Code != 200 {
		t.Fatalf("POST /v1/search = %d, want 200: %s", rec.Code, body)
	}
	doc := decodeSearch(t, body)
	if doc.Meta.Requested != 1 {
		t.Errorf("meta.requested = %d, want 1 (queries 优先)", doc.Meta.Requested)
	}
	if len(doc.Results) != 1 {
		t.Fatalf("results = %d 条, want 1: %s", len(doc.Results), body)
	}
	if doc.Results[0]["source"] != "crossref" {
		t.Errorf("results[0].source = %v, want crossref", doc.Results[0]["source"])
	}
	if doc.Queries[0] != "crossref/works" {
		t.Errorf("queries[0] = %q, want crossref/works", doc.Queries[0])
	}
}

// TestSearchKeepsTheParameterOrder checks that the parameter order of a
// query survives the request: the keys keep the order the caller wrote them
// in, and the rendered upstream URL — and with it the cache key — is the same
// for both orders.
//
// The URL is not part of the search answer, so it is compared on the resolver
// (the very function the handler uses to render it).
func TestSearchKeepsTheParameterOrder(t *testing.T) {
	s := offlineServer(t)

	first := `{"queries":[{"source":"crossref","endpoint":"works","params":{"filter":"from-index-date:2026-10-21","email":"a@b.com"}}]}`
	second := `{"queries":[{"source":"crossref","endpoint":"works","params":{"email":"a@b.com","filter":"from-index-date:2026-10-21"}}]}`

	entriesFirst := searchEntries(t, first)
	entriesSecond := searchEntries(t, second)
	if len(entriesFirst) != 1 || len(entriesSecond) != 1 {
		t.Fatalf("queries = %d / %d 条, want 1", len(entriesFirst), len(entriesSecond))
	}
	wantFirst := []string{"filter", "email"}
	wantSecond := []string{"email", "filter"}
	if got := entriesFirst[0].params.Keys(); !sameKeys(got, wantFirst) {
		t.Errorf("params 的键序 = %v, want %v", got, wantFirst)
	}
	if got := entriesSecond[0].params.Keys(); !sameKeys(got, wantSecond) {
		t.Errorf("params 的键序 = %v, want %v", got, wantSecond)
	}

	ep := endpointOf(t, s.resolver, "crossref", "works")
	urlFirst, err := s.resolver.Render(ep, QueryRequest{
		Source:   entriesFirst[0].source,
		Endpoint: entriesFirst[0].endpoint,
		Params:   entriesFirst[0].params,
	}, 25)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	urlSecond, err := s.resolver.Render(ep, QueryRequest{
		Source:   entriesSecond[0].source,
		Endpoint: entriesSecond[0].endpoint,
		Params:   entriesSecond[0].params,
	}, 25)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if urlFirst != urlSecond {
		t.Errorf("两种键序渲染出的 URL 不一致:\n%s\n%s", urlFirst, urlSecond)
	}

	// HTTP 这一层: 两种键序都要 200, 并且指向同一个源与端点。
	for _, raw := range []string{first, second} {
		rec, body := do(t, s, "POST", "/v1/search", raw)
		if rec.Code != 200 {
			t.Fatalf("POST /v1/search = %d, want 200: %s", rec.Code, body)
		}
		doc := decodeSearch(t, body)
		if len(doc.Results) != 1 {
			t.Fatalf("results = %d 条, want 1: %s", len(doc.Results), body)
		}
		if doc.Results[0]["source"] != "crossref" || doc.Results[0]["endpoint"] != "works" {
			t.Errorf("results[0] = %v, want crossref/works", doc.Results[0])
		}
	}
}

// searchEntries parses the "queries" array of a search body the way the
// handler does.
func searchEntries(t *testing.T, raw string) []searchRequest {
	t.Helper()
	entries, verr := parseSearchEntries(mustSearchBody(t, raw))
	if verr != nil {
		t.Fatalf("parseSearchEntries: %s", verr.msg)
	}
	return entries
}

// sameKeys reports whether two key lists are equal.
func sameKeys(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}

// TestOpenAPIDocumentDeclaresTheSearchRoute checks that the hand written
// OpenAPI description knows the search route. The document is a literal, so a
// new route has to be declared by hand.
func TestOpenAPIDocumentDeclaresTheSearchRoute(t *testing.T) {
	raw, err := OpenAPIDocument().MarshalJSON()
	if err != nil {
		t.Fatalf("OpenAPIDocument: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}
	paths, ok := doc["paths"].(map[string]any)
	if !ok {
		t.Fatal("文档缺 paths 对象")
	}
	search, ok := paths["/v1/search"].(map[string]any)
	if !ok {
		t.Fatalf("paths 里缺 %q", "/v1/search")
	}
	post, ok := search["post"].(map[string]any)
	if !ok {
		t.Fatal("/v1/search 缺 post operation")
	}
	if _, ok := post["requestBody"]; !ok {
		t.Error("/v1/search 的 post 缺 requestBody")
	}
	responses, ok := post["responses"].(map[string]any)
	if !ok {
		t.Fatal("/v1/search 的 post 缺 responses")
	}
	for _, code := range []string{"200", "422"} {
		if _, ok := responses[code]; !ok {
			t.Errorf("/v1/search 的 responses 里缺 %s", code)
		}
	}

	// 请求体要描述两种形态。
	props := searchSchemaProps(t, post)
	for _, key := range []string{"queries", "q", "limit", "max_sources"} {
		if _, ok := props[key]; !ok {
			t.Errorf("/v1/search 的请求体缺 %q", key)
		}
	}
	// queries 的元素要描述那五个字段。
	queries, ok := props["queries"].(map[string]any)
	if !ok {
		t.Fatal("/v1/search 的请求体里 queries 不是对象")
	}
	items, ok := queries["items"].(map[string]any)
	if !ok {
		t.Fatal("/v1/search 的请求体里 queries.items 不是对象")
	}
	itemProps, ok := items["properties"].(map[string]any)
	if !ok {
		t.Fatal("/v1/search 的请求体里 queries.items.properties 不是对象")
	}
	for _, key := range []string{"source", "endpoint", "params", "limit", "fields"} {
		if _, ok := itemProps[key]; !ok {
			t.Errorf("/v1/search 的 queries[] 缺 %q", key)
		}
	}

	// /v1/query 的声明不能被这次改动带坏: 它同时有 get 与 post。
	query, ok := paths["/v1/query"].(map[string]any)
	if !ok {
		t.Fatal("paths 里缺 /v1/query")
	}
	for _, method := range []string{"get", "post"} {
		if _, ok := query[method]; !ok {
			t.Errorf("/v1/query 缺 %s operation", method)
		}
	}
}

// TestOpenAPIDocumentServedOverHTTPIncludesSearch checks the same thing on
// the document the gateway actually serves.
func TestOpenAPIDocumentServedOverHTTPIncludesSearch(t *testing.T) {
	s := offlineServer(t)
	rec, body := do(t, s, "GET", "/openapi.json", "")
	if rec.Code != 200 {
		t.Fatalf("GET /openapi.json = %d, want 200", rec.Code)
	}
	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}
	paths, ok := doc["paths"].(map[string]any)
	if !ok {
		t.Fatal("文档缺 paths 对象")
	}
	search, ok := paths["/v1/search"].(map[string]any)
	if !ok {
		t.Fatalf("paths 里缺 %q", "/v1/search")
	}
	if _, ok := search["post"]; !ok {
		t.Error("/v1/search 缺 post operation")
	}
}

// searchSchemaProps returns the properties of the request body schema of an
// operation.
func searchSchemaProps(t *testing.T, op map[string]any) map[string]any {
	t.Helper()
	body, ok := op["requestBody"].(map[string]any)
	if !ok {
		t.Fatal("operation 缺 requestBody")
	}
	content, ok := body["content"].(map[string]any)
	if !ok {
		t.Fatal("requestBody 缺 content")
	}
	media, ok := content["application/json"].(map[string]any)
	if !ok {
		t.Fatal("requestBody 缺 application/json")
	}
	schema, ok := media["schema"].(map[string]any)
	if !ok {
		t.Fatal("application/json 缺 schema")
	}
	props, ok := schema["properties"].(map[string]any)
	if !ok {
		t.Fatal("schema 缺 properties")
	}
	return props
}
