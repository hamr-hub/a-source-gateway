package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
)

// envelopeOf runs a query and returns the decoded envelope.
func envelopeOf(t *testing.T, s *Server, target string) map[string]any {
	t.Helper()
	rec, body := do(t, s, "GET", target, "")
	if rec.Code != 200 {
		t.Fatalf("GET %s = %d", target, rec.Code)
	}
	var env map[string]any
	if err := json.Unmarshal(body, &env); err != nil {
		t.Fatalf("decode envelope: %v (%s)", err, body)
	}
	return env
}

func metaOf(t *testing.T, env map[string]any) map[string]any {
	t.Helper()
	meta, ok := env["meta"].(map[string]any)
	if !ok {
		t.Fatalf("the envelope has no meta block: %v", env)
	}
	return meta
}

// TestParserIsAlwaysReported covers the early error paths of the query.
func TestParserIsAlwaysReported(t *testing.T) {
	s := realServer(t)
	targets := []string{
		"/v1/query?source=crossref&endpoint=not-an-endpoint",    // unknown endpoint
		"/v1/query?source=crossref&endpoint=works&params=%7B%7D", // missing required param
		"/v1/query?source=sec_edgar&endpoint=works&params=%7B%7D", // contact required
	}
	for _, target := range targets {
		env := envelopeOf(t, s, target)
		meta := metaOf(t, env)
		parser, ok := meta["parser"].(string)
		if !ok || parser == "" {
			t.Errorf("%s: meta.parser = %v, want python or go", target, meta["parser"])
		}
		if parser != "python" && parser != "go" {
			t.Errorf("%s: meta.parser = %q", target, parser)
		}
	}
}

// TestTraceIDIsAlwaysPresent covers the early error paths and the header
// echo.
func TestTraceIDIsAlwaysPresent(t *testing.T) {
	s := realServer(t)
	for _, target := range []string{
		"/v1/query?source=crossref&endpoint=not-an-endpoint",
		"/v1/query?source=crossref&endpoint=works&params=%7B%7D",
		"/v1/query?source=sec_edgar&endpoint=works&params=%7B%7D",
	} {
		rec, body := do(t, s, "GET", target, "")
		if rec.Code != 200 {
			t.Fatalf("GET %s = %d", target, rec.Code)
		}
		var env map[string]any
		if err := json.Unmarshal(body, &env); err != nil {
			t.Fatal(err)
		}
		meta := metaOf(t, env)
		trace, ok := meta["trace_id"].(string)
		if !ok || trace == "" {
			t.Errorf("%s: meta.trace_id = %v, want a non empty id", target, meta["trace_id"])
			continue
		}
		if len(trace) != 32 {
			t.Errorf("%s: meta.trace_id = %q, want 32 hex characters", target, trace)
		}
		if got := rec.Header().Get(TraceHeader); got != trace {
			t.Errorf("%s: the %s header is %q, want %q", target, TraceHeader, got, trace)
		}
	}
}

// TestTraceIDOnTheClientErrors checks that a 4xx response also carries the
// trace id header.
func TestTraceIDOnTheClientErrors(t *testing.T) {
	s := realServer(t)
	for _, target := range []string{
		"/v1/query?source=nope&endpoint=works",
		"/v1/sources/nope",
		"/v1/sources/crossref/endpoints/nope",
		"/v1/query?source=crossref&endpoint=works&params=%7B",
	} {
		rec, _ := do(t, s, "GET", target, "")
		if rec.Code < 400 {
			t.Fatalf("GET %s = %d, want a client error", target, rec.Code)
		}
		if rec.Header().Get(TraceHeader) == "" {
			t.Errorf("GET %s: the %s header is missing", target, TraceHeader)
		}
	}
}

// TestTraceIDIsReusedFromTheRequest checks that an inbound trace id is
// reused and echoed back.
func TestTraceIDIsReusedFromTheRequest(t *testing.T) {
	s := realServer(t)
	for _, header := range []string{TraceHeader, "X-Request-Id"} {
		req := httptest.NewRequest("GET", "/v1/query?source=crossref&endpoint=works&params=%7B%7D", nil)
		req.Header.Set(header, "abc-123")
		rec := httptest.NewRecorder()
		s.Handler().ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Fatalf("GET = %d", rec.Code)
		}
		var env map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
			t.Fatal(err)
		}
		meta := metaOf(t, env)
		if meta["trace_id"] != "abc-123" {
			t.Errorf("%s: meta.trace_id = %v, want abc-123", header, meta["trace_id"])
		}
		if got := rec.Header().Get(TraceHeader); got != "abc-123" {
			t.Errorf("%s: the %s header is %q", header, TraceHeader, got)
		}
	}
}

// TestLatencyIsNeverZeroOnTheEarlyPaths covers the latency of the early
// error returns.
func TestLatencyIsNeverZeroOnTheEarlyPaths(t *testing.T) {
	s := realServer(t)
	for _, target := range []string{
		"/v1/query?source=crossref&endpoint=not-an-endpoint",
		"/v1/query?source=crossref&endpoint=works&params=%7B%7D",
		"/v1/query?source=sec_edgar&endpoint=works&params=%7B%7D",
	} {
		env := envelopeOf(t, s, target)
		meta := metaOf(t, env)
		latency, ok := meta["latency_ms"].(float64)
		if !ok {
			t.Errorf("%s: meta.latency_ms = %v", target, meta["latency_ms"])
			continue
		}
		if latency < 1 {
			t.Errorf("%s: meta.latency_ms = %v, want at least 1", target, latency)
		}
	}
}

// TestUpstreamLatencyOnTimeout checks that the upstream block reports how
// long the gateway waited before the deadline.
func TestUpstreamLatencyOnTimeout(t *testing.T) {
	r := newTestResolver(t, func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(600 * time.Millisecond)
	}, func(c *config.Config) { c.UpstreamBudgetMS = 150 })
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	if !env.Meta.Degraded {
		t.Fatal("the envelope must be degraded")
	}
	if !hasError(env.Meta.Errors, "timeout") {
		t.Fatalf("errors = %+v, want a timeout", env.Meta.Errors)
	}
	if env.Meta.Upstream.LatencyMS < 100 {
		t.Errorf("upstream.latency_ms = %d, want the time spent before the deadline", env.Meta.Upstream.LatencyMS)
	}
	if env.Meta.Upstream.URL == "" || env.Meta.Upstream.Method != "GET" {
		t.Errorf("upstream = %+v, want the url and the method", env.Meta.Upstream)
	}
}

// TestParserNameIsReported checks the parser of a successful query.
func TestParserNameIsReported(t *testing.T) {
	r := newTestResolver(t, jsonHandler(`{"message":{"total-results":1,"items":[{"a":1}]}}`))
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	if env.Meta.Parser != "go" {
		t.Errorf("meta.parser = %q, want go", env.Meta.Parser)
	}
	if env.Meta.TraceID == "" {
		t.Error("meta.trace_id must be set")
	}
}

// TestStatsReportsTheParser checks that the active engine is visible in the
// statistics.
func TestStatsReportsTheParser(t *testing.T) {
	s := realServer(t)
	rec, body := do(t, s, "GET", "/v1/stats", "")
	if rec.Code != 200 {
		t.Fatalf("GET /v1/stats = %d", rec.Code)
	}
	var stats map[string]any
	if err := json.Unmarshal(body, &stats); err != nil {
		t.Fatal(err)
	}
	parser, _ := stats["parser"].(string)
	if parser != "python" && parser != "go" {
		t.Errorf("stats.parser = %v, want python or go", stats["parser"])
	}
	if !strings.Contains(string(body), `"parser"`) {
		t.Errorf("the stats do not report the parser: %s", body)
	}
}
