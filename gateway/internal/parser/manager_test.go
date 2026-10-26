package parser

import (
	"bytes"
	"context"
	"errors"
	"log"
	"strings"
	"testing"
	"time"
)

// The upstream answered with an HTML error page: the worker reports it as
// "NotJSON". That is a content problem, not a worker problem — the worker
// answered correctly.
//
// The Python plane has to stay active: an error page of one upstream must
// not switch the parsing engine of the whole process, and the caller has to
// get the judgeable error (the envelope degrades as "upstream_not_json").
//
// The worker runs for real here (NDJSON over stdio), a stub would not prove
// that the worker answers correctly and stays usable.
func TestManagerKeepsThePythonPlaneOnANotJSONAnswer(t *testing.T) {
	python, srcDir := pythonPlane(t)
	var logs bytes.Buffer
	m := NewManager(context.Background(), "python", python, srcDir, 100, log.New(&logs, "", 0))
	t.Cleanup(func() { _ = m.Close() })
	if m.Name() != "python" {
		t.Skipf("the python parsing plane is not available: %s", m.Name())
	}
	engine := m.Engine()
	py, ok := engine.(*PythonEngine)
	if !ok {
		t.Fatalf("the active engine = %T", engine)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_, err := m.Parse(ctx, "object", "application/json",
		[]byte("<!DOCTYPE html><html><body>502 Bad Gateway</body></html>"), Spec{})
	if err == nil {
		t.Fatal("a body that is not JSON must be an error")
	}
	if !errors.Is(err, ErrNotJSON) {
		t.Fatalf("error = %v, want it to match ErrNotJSON", err)
	}
	if !strings.Contains(err.Error(), "<!DOCTYPE html>") {
		t.Errorf("error = %q, want the excerpt of the body", err)
	}

	// The worker answered correctly: it is neither broken nor closed, and
	// the parsing plane did not switch over to the Go engine.
	py.mu.Lock()
	broken := py.broken
	cmd := py.cmd
	py.mu.Unlock()
	if broken {
		t.Error("the worker must not be marked broken")
	}
	if cmd == nil {
		t.Error("the worker must not be closed")
	}
	if m.Name() != "python" {
		t.Errorf("the active engine = %q, want python", m.Name())
	}
	if m.Engine() != engine {
		t.Error("the active engine changed")
	}
	if strings.Contains(logs.String(), "worker failed") {
		t.Errorf("the manager must not report a worker failure: %s", logs.String())
	}

	// The next normal request still goes through the Python worker.
	res, err := m.Parse(ctx, "object", "application/json", []byte(`{"items":[{"a":1}]}`),
		Spec{ItemPath: "items"})
	if err != nil {
		t.Fatalf("the worker is not usable after a non JSON answer: %v", err)
	}
	list, ok := res.Data.([]any)
	if !ok || len(list) != 1 {
		t.Fatalf("data = %#v", res.Data)
	}
	if m.Name() != "python" {
		t.Errorf("the active engine = %q, want python", m.Name())
	}
	if m.Engine() != engine {
		t.Error("the active engine changed")
	}
}

// stubEngine is a fake worker: it answers whatever the test asks it to
// answer, so that the manager can be observed without a real worker.
type stubEngine struct {
	name string
	err  error
}

func (e *stubEngine) Name() string { return e.name }

func (e *stubEngine) Parse(ctx context.Context, shape, mediaType string, body []byte, spec Spec) (*Result, error) {
	return nil, e.err
}

func (e *stubEngine) Close() error { return nil }

// A body that is not the declared shape is a content problem, not a worker
// problem: the worker answered correctly.
//
// The Python plane has to stay active: an error page of one upstream must
// not switch the parsing engine of the whole process, and the caller has to
// get the judgeable error (the envelope degrades as "parse_error").
func TestManagerKeepsThePythonPlaneOnAShapeMismatch(t *testing.T) {
	var logs bytes.Buffer
	m := NewManager(context.Background(), "go", "", "", 100, log.New(&logs, "", 0))
	t.Cleanup(func() { _ = m.Close() })
	stub := &stubEngine{name: "python", err: NewNotShapeError("csv", "<!DOCTYPE html>", nil)}
	m.active = stub

	_, err := m.Parse(context.Background(), "csv", "text/csv", []byte("<!DOCTYPE html>"), Spec{})
	if err == nil {
		t.Fatal("a body that is not CSV must be an error")
	}
	if !errors.Is(err, ErrNotShape) {
		t.Fatalf("error = %v, want it to match ErrNotShape", err)
	}
	if m.Engine() != stub {
		t.Error("the parsing engine must not switch over")
	}
	if strings.Contains(logs.String(), "worker failed") {
		t.Errorf("the manager must not report a worker failure: %s", logs.String())
	}
}

// The worker reports a shape mismatch with its own error type name, which
// the two planes do not have to agree on. The wording is a contract: the
// manager recognises the answer by it, so that the Python plane stays
// active and the caller gets the judgeable error.
func TestManagerRecognisesAShapeMismatchFromTheWorkerWording(t *testing.T) {
	var logs bytes.Buffer
	m := NewManager(context.Background(), "go", "", "", 100, log.New(&logs, "", 0))
	t.Cleanup(func() { _ = m.Close() })
	stub := &stubEngine{name: "python", err: errors.New("上游返回的不是 CSV(疑似 XML/HTML 错误页); 正文片段: <!DOCTYPE html>")}
	m.active = stub

	_, err := m.Parse(context.Background(), "csv", "text/csv", []byte("<!DOCTYPE html>"), Spec{})
	if err == nil {
		t.Fatal("a body that is not CSV must be an error")
	}
	if !errors.Is(err, ErrNotShape) {
		t.Fatalf("error = %v, want it to match ErrNotShape", err)
	}
	want := "上游返回的不是 CSV(疑似 XML/HTML 错误页); 正文片段: <!DOCTYPE html>"
	if err.Error() != want {
		t.Errorf("error = %q, want %q", err.Error(), want)
	}
	if m.Engine() != stub {
		t.Error("the parsing engine must not switch over")
	}
	if strings.Contains(logs.String(), "worker failed") {
		t.Errorf("the manager must not report a worker failure: %s", logs.String())
	}
}

// A real worker failure still switches the engine over: the whitelist of
// the "the worker answered correctly" answers must not swallow it.
func TestManagerSwitchesTheEngineOnARealWorkerFailure(t *testing.T) {
	var logs bytes.Buffer
	m := NewManager(context.Background(), "go", "", "", 100, log.New(&logs, "", 0))
	t.Cleanup(func() { _ = m.Close() })
	m.active = &stubEngine{name: "python", err: errors.New("boom")}

	_, _ = m.Parse(context.Background(), "csv", "text/csv", []byte("a,b\n1,2\n"), Spec{})
	if m.Name() != "go" {
		t.Errorf("the active engine = %q, want go", m.Name())
	}
	if !strings.Contains(logs.String(), "worker failed") {
		t.Errorf("the manager must report the worker failure: %s", logs.String())
	}
}
