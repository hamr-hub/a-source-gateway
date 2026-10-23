package server

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/cache"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/envelope"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/metrics"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/ratelimit"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/upstream"
)

// The budget of a call is 500ms, wall clock, everything included.
const budgetWallClock = 500 * time.Millisecond

// upstreamLatency is the latency of the fake upstream of the budget tests.
const upstreamLatency = 380 * time.Millisecond

// bigBodySize is the size of the JSON document of the budget tests. It is
// big enough to make the parsing plane the expensive part of the call.
const bigBodySize = 6_400_000

// pythonPlane returns an interpreter that can import the parsing plane.
//
// The test is skipped when no usable interpreter is found: the Go gateway
// has to build and pass its tests on a machine without a Python runtime.
func pythonPlane(t *testing.T) (string, string) {
	t.Helper()
	srcDir := config.Load().FindPythonSrcDir()
	candidates := []string{}
	if v := strings.TrimSpace(os.Getenv("A_GATEWAY_PARSER_PYTHON")); v != "" {
		candidates = append(candidates, v)
	}
	// The repository keeps a virtualenv with the package installed in
	// editable mode.
	if wd, err := os.Getwd(); err == nil {
		candidates = append(candidates, filepath.Join(wd, "..", "..", "..", ".venv", "bin", "python"))
	}
	candidates = append(candidates, "python3")
	for _, c := range candidates {
		bin, err := exec.LookPath(c)
		if err != nil {
			continue
		}
		cmd := exec.Command(bin, "-c", "import a_source_gateway.parser_worker")
		if srcDir != "" {
			cmd.Env = append(os.Environ(), "PYTHONPATH="+srcDir)
		}
		if err := cmd.Run(); err != nil {
			continue
		}
		return bin, srcDir
	}
	t.Skip("no python3 with the a_source_gateway package available")
	return "", ""
}

// bigJSONBody builds a JSON document of about the given size.
//
// The shape is the one of the crossref endpoint of the test registry, so the
// parsing plane has to walk through the whole list.
func bigJSONBody(t *testing.T, size int) []byte {
	t.Helper()
	var b strings.Builder
	b.Grow(size + 1024)
	b.WriteString(`{"message":{"total-results":1,"items":[`)
	first := true
	for b.Len() < size {
		if !first {
			b.WriteByte(',')
		}
		first = false
		b.WriteString(`{"id":"0123456789abcdef","title":"`)
		b.WriteString(strings.Repeat("a record title of the fake upstream ", 18))
		b.WriteString(`","value":123456789,"nested":{"a":1,"b":2,"c":[1,2,3]}}`)
	}
	b.WriteString(`]}}`)
	return []byte(b.String())
}

// pythonResolver builds a resolver whose parsing plane is the Python worker
// and whose upstream is slow and answers with a big document.
func pythonResolver(t *testing.T, body []byte) *Resolver {
	t.Helper()
	return budgetResolver(t, body, "python")
}

// budgetResolver builds a resolver with the given parsing plane and a slow
// upstream that answers with a big document.
func budgetResolver(t *testing.T, body []byte, mode string) *Resolver {
	t.Helper()
	python, srcDir := "", ""
	if mode == "python" {
		python, srcDir = pythonPlane(t)
	}
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(upstreamLatency)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	t.Cleanup(up.Close)

	cfg := config.Load()
	cfg.Contact = "test@example.com"
	cfg.BudgetMS = 500
	cfg.UpstreamBudgetMS = 400
	cfg.ReserveMS = 100
	cfg.MaxBodyBytes = 16 << 20
	cfg.WarmBudgetMS = 5000
	cfg.Offline = false

	logger := log.New(io.Discard, "", 0)
	parsers := parser.NewManager(context.Background(), mode, python, srcDir, 1000, logger)
	t.Cleanup(func() { _ = parsers.Close() })
	if mode == "python" && parsers.Name() != "python" {
		t.Skipf("the python parsing plane did not start (%q)", parsers.Name())
	}
	return NewResolver(
		cfg,
		testRegistry(up.URL),
		cache.New(32, 1<<20),
		upstream.New(upstream.Options{PoolSize: 4, VerifyTLS: true, UserAgent: cfg.UserAgent}),
		parsers,
		metrics.New(),
		ratelimit.New(4),
		logger,
	)
}

// TestPythonPlaneStaysInsideTheBudget reproduces the scenario of the code
// review: a slow upstream (380ms) plus a big JSON document must not push the
// total wall clock of the call beyond the budget.
//
// The parsing plane is the Python worker, the default of the gateway.
func TestPythonPlaneStaysInsideTheBudget(t *testing.T) {
	body := bigJSONBody(t, bigBodySize)
	r := pythonResolver(t, body)

	start := time.Now()
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	elapsed := time.Since(start)
	t.Logf("python plane: wall clock %dms, upstream %dms, errors %+v",
		elapsed.Milliseconds(), env.Meta.Upstream.LatencyMS, env.Meta.Errors)

	if elapsed > budgetWallClock {
		t.Fatalf("the call took %dms, the budget is %dms", elapsed.Milliseconds(), budgetWallClock.Milliseconds())
	}
	// The parse does not fit into the budget, so the call has to degrade
	// like an upstream timeout: the caller gets the fallback block instead
	// of a late answer.
	if !env.Meta.Degraded {
		t.Fatalf("the envelope must be degraded: %+v", env.Meta)
	}
	if !hasError(env.Meta.Errors, "timeout") {
		t.Errorf("errors = %+v, want a timeout", env.Meta.Errors)
	}
	if hasError(env.Meta.Errors, "parse_error") {
		t.Errorf("errors = %+v, a parse that does not fit is a timeout, not a parse error", env.Meta.Errors)
	}
	if env.Meta.Fallback == nil || env.Meta.Fallback.Len() == 0 {
		t.Error("a degraded call must carry the fallback block")
	}
}

// TestGoPlaneStaysInsideTheBudget covers the same scenario with the Go
// parsing plane: the budget has to hold whichever engine is active.
func TestGoPlaneStaysInsideTheBudget(t *testing.T) {
	body := bigJSONBody(t, bigBodySize)
	r := budgetResolver(t, body, "go")

	start := time.Now()
	env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
		Params: params("filter", "x"), Limit: 20, TimeoutMS: 500}, nil)
	elapsed := time.Since(start)
	t.Logf("go plane: wall clock %dms, upstream %dms, errors %+v",
		elapsed.Milliseconds(), env.Meta.Upstream.LatencyMS, env.Meta.Errors)

	if elapsed > budgetWallClock {
		t.Fatalf("the call took %dms, the budget is %dms", elapsed.Milliseconds(), budgetWallClock.Milliseconds())
	}
}

// TestPythonPlaneConcurrentCallsStayInsideTheBudget covers the second
// scenario of the code review: two concurrent calls must not make the second
// one inherit the parse time of the first one.
func TestPythonPlaneConcurrentCallsStayInsideTheBudget(t *testing.T) {
	body := bigJSONBody(t, bigBodySize)
	r := pythonResolver(t, body)

	type result struct {
		elapsed time.Duration
		env     *envelope.Envelope
	}
	results := make([]result, 2)
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			start := time.Now()
			env := r.Query(QueryRequest{Source: "crossref", Endpoint: "works",
				Params: params("filter", string(rune('a'+i))), Limit: 20, TimeoutMS: 500}, nil)
			results[i] = result{elapsed: time.Since(start), env: env}
		}(i)
	}
	wg.Wait()
	for i, res := range results {
		t.Logf("concurrent call %d: wall clock %dms, errors %+v",
			i, res.elapsed.Milliseconds(), res.env.Meta.Errors)
		if res.elapsed > budgetWallClock {
			t.Errorf("call %d took %dms, the budget is %dms", i,
				res.elapsed.Milliseconds(), budgetWallClock.Milliseconds())
		}
	}
}
