package server

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/cache"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/metrics"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/ratelimit"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/registry"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/upstream"
)

// realServer builds a server on top of the registry of the repository data
// files.
func realServer(t *testing.T) *Server {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	root := filepath.Dir(filepath.Dir(wd))
	cfg := config.Load()
	cfg.DataDir = filepath.Join(root, "data")
	reg, err := registry.Build(cfg)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	logger := log.New(io.Discard, "", 0)
	return New(cfg,
		NewResolver(cfg, reg, cache.New(16, 1<<20),
			upstream.New(upstream.Options{PoolSize: 2, VerifyTLS: true}),
			parser.NewManager(context.Background(), "go", "python3", "", 1000, logger),
			metrics.New(), ratelimit.New(2), logger),
		metrics.New(), logger)
}

func do(t *testing.T, s *Server, method, target string, body string) (*httptest.ResponseRecorder, []byte) {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, target, reader)
	rec := httptest.NewRecorder()
	s.Handler().ServeHTTP(rec, req)
	return rec, rec.Body.Bytes()
}

func TestRoutesMetadata(t *testing.T) {
	s := realServer(t)

	rec, body := do(t, s, "GET", "/", "")
	if rec.Code != 200 {
		t.Fatalf("GET / = %d", rec.Code)
	}
	var root map[string]any
	if err := json.Unmarshal(body, &root); err != nil {
		t.Fatal(err)
	}
	want := float64(len(s.resolver.Registry().Sources))
	if root["sources"].(float64) != want {
		t.Errorf("sources = %v, want %v", root["sources"], want)
	}

	for _, path := range []string{"/healthz", "/livez", "/readyz", "/metrics", "/v1/schema", "/v1/stats"} {
		rec, _ := do(t, s, "GET", path, "")
		if rec.Code != 200 {
			t.Errorf("GET %s = %d", path, rec.Code)
		}
	}

	rec, body = do(t, s, "GET", "/v1/sources", "")
	if rec.Code != 200 {
		t.Fatalf("GET /v1/sources = %d", rec.Code)
	}
	var list map[string]any
	if err := json.Unmarshal(body, &list); err != nil {
		t.Fatal(err)
	}
	if n := len(list["data"].([]any)); int(n) != len(s.resolver.Registry().Sources) {
		t.Errorf("sources listed = %d, want %d", n, len(s.resolver.Registry().Sources))
	}

	rec, _ = do(t, s, "GET", "/v1/sources?family=%E5%AD%A6%E6%9C%AF&limit=5", "")
	if rec.Code != 200 {
		t.Errorf("GET /v1/sources?family = %d", rec.Code)
	}

	rec, _ = do(t, s, "GET", "/v1/sources/crossref", "")
	if rec.Code != 200 {
		t.Errorf("GET /v1/sources/crossref = %d", rec.Code)
	}
	rec, _ = do(t, s, "GET", "/v1/sources/nope", "")
	if rec.Code != 404 {
		t.Errorf("GET /v1/sources/nope = %d, want 404", rec.Code)
	}
	rec, _ = do(t, s, "GET", "/v1/sources/crossref/endpoints/works", "")
	if rec.Code != 200 {
		t.Errorf("GET endpoint = %d", rec.Code)
	}
	rec, _ = do(t, s, "GET", "/v1/sources/crossref/endpoints/nope", "")
	if rec.Code != 404 {
		t.Errorf("GET unknown endpoint = %d, want 404", rec.Code)
	}
	rec, _ = do(t, s, "GET", "/v1/nothing", "")
	if rec.Code != 404 {
		t.Errorf("GET /v1/nothing = %d, want 404", rec.Code)
	}
}

func TestQueryStatusCodes(t *testing.T) {
	s := realServer(t)

	// An unknown source is a client error.
	rec, _ := do(t, s, "GET", "/v1/query?source=nope&endpoint=works", "")
	if rec.Code != 404 {
		t.Errorf("GET /v1/query (unknown source) = %d, want 404", rec.Code)
	}
	rec, _ = do(t, s, "POST", "/v1/query", `{"source":"nope","endpoint":"works"}`)
	if rec.Code != 404 {
		t.Errorf("POST /v1/query (unknown source) = %d, want 404", rec.Code)
	}
	// Malformed params are a client error.
	rec, _ = do(t, s, "GET", "/v1/query?source=crossref&endpoint=works&params=%7B", "")
	if rec.Code != 400 {
		t.Errorf("GET /v1/query (broken params) = %d, want 400", rec.Code)
	}
	// A limit outside of the allowed range is a validation error.
	rec, _ = do(t, s, "POST", "/v1/query", `{"source":"crossref","endpoint":"works","limit":900}`)
	if rec.Code != 422 {
		t.Errorf("POST /v1/query (limit 900) = %d, want 422", rec.Code)
	}
	rec, _ = do(t, s, "POST", "/v1/query", `{"source":"crossref","endpoint":"works","timeout_ms":10}`)
	if rec.Code != 422 {
		t.Errorf("POST /v1/query (timeout 10) = %d, want 422", rec.Code)
	}
	// A missing field is a validation error.
	rec, _ = do(t, s, "POST", "/v1/query", `{"source":"crossref"}`)
	if rec.Code != 422 {
		t.Errorf("POST /v1/query (missing endpoint) = %d, want 422", rec.Code)
	}
}

func TestQueryEnvelopeShape(t *testing.T) {
	s := realServer(t)
	rec, body := do(t, s, "GET", "/v1/query?source=crossref&endpoint=works&params=%7B%7D", "")
	if rec.Code != 200 {
		t.Fatalf("GET /v1/query = %d", rec.Code)
	}
	if rec.Header().Get("X-Gateway-Latency-Ms") == "" {
		t.Error("the latency header is missing")
	}
	var env map[string]any
	if err := json.Unmarshal(body, &env); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"meta", "data", "fields"} {
		if _, ok := env[key]; !ok {
			t.Errorf("the envelope is missing %q", key)
		}
	}
	meta := env["meta"].(map[string]any)
	for _, key := range []string{"request_id", "schema_version", "source", "endpoint", "upstream",
		"latency_ms", "budget_ms", "cached", "cache_age_s", "degraded", "offline", "count",
		"total", "truncated", "next_cursor", "rate_limit", "fallback", "errors"} {
		if _, ok := meta[key]; !ok {
			t.Errorf("the meta block is missing %q", key)
		}
	}
	// The offline mode is not active, but the source demands a contact
	// address, so the response is degraded with a contact error.
	errs := meta["errors"].([]any)
	if len(errs) == 0 {
		t.Error("expected a contact_required error")
	}
}

func TestSchemaDocumentMatchesTheReference(t *testing.T) {
	s := realServer(t)
	rec, body := do(t, s, "GET", "/v1/schema", "")
	if rec.Code != 200 {
		t.Fatalf("GET /v1/schema = %d", rec.Code)
	}
	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"schema_version", "description", "source_spec", "endpoint_spec",
		"request", "response_envelope"} {
		if _, ok := doc[key]; !ok {
			t.Errorf("the schema document is missing %q", key)
		}
	}
	if doc["schema_version"] != "1.0" {
		t.Errorf("schema_version = %v", doc["schema_version"])
	}
}

func TestMetricsExposition(t *testing.T) {
	s := realServer(t)
	rec, body := do(t, s, "GET", "/metrics", "")
	if rec.Code != 200 {
		t.Fatalf("GET /metrics = %d", rec.Code)
	}
	if !strings.Contains(string(body), "asg_queries_total") {
		t.Errorf("metrics = %s", body)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/plain") {
		t.Errorf("content type = %q", ct)
	}
}
