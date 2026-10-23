package parser

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// pythonPlane returns an interpreter that can import the parsing plane.
//
// The test is skipped when no usable interpreter is found: the Go gateway
// has to build and pass its tests on a machine without a Python runtime.
func pythonPlane(t *testing.T) (string, string) {
	t.Helper()
	srcDir := pythonSrcDir()
	candidates := []string{}
	if v := strings.TrimSpace(os.Getenv("A_GATEWAY_PARSER_PYTHON")); v != "" {
		candidates = append(candidates, v)
	}
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

// pythonSrcDir walks up to the directory that holds the a_source_gateway
// package, so that the worker can be started without an installed package.
func pythonSrcDir() string {
	dir, err := os.Getwd()
	if err != nil {
		return ""
	}
	for i := 0; i < 8; i++ {
		for _, cand := range []string{
			filepath.Join(dir, "a_source_gateway"),
			filepath.Join(dir, "src", "a_source_gateway"),
			filepath.Join(dir, "python", "src", "a_source_gateway"),
		} {
			if st, err := os.Stat(cand); err == nil && st.IsDir() {
				return filepath.Dir(cand)
			}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return ""
}

// bigBody builds a JSON document of about the given size.
func bigBody(t *testing.T, size int) []byte {
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
		b.WriteString(`","value":123456789}`)
	}
	b.WriteString(`]}}`)
	return []byte(b.String())
}

// TestPythonEngineWaitsForTheWorkerInAContextAwareWay covers the second
// root cause of the budget overrun: the worker slot used to be a plain
// mutex, so a request waited for the whole parse of the request in front of
// it, whatever its own deadline was.
//
// The second request must give up when its deadline expires, not when the
// first parse is done.
func TestPythonEngineWaitsForTheWorkerInAContextAwareWay(t *testing.T) {
	python, srcDir := pythonPlane(t)
	eng, err := StartPython(context.Background(), python, srcDir)
	if err != nil {
		t.Skipf("the python parsing plane is not available: %v", err)
	}
	t.Cleanup(func() { _ = eng.Close() })

	body := bigBody(t, 16_000_000)
	firstDone := make(chan struct{})
	first := make(chan error, 1)
	go func() {
		defer close(firstDone)
		_, err := eng.Parse(context.Background(), "object", "application/json", body, Spec{})
		first <- err
	}()
	// Give the first request the time to take the worker slot.
	time.Sleep(30 * time.Millisecond)

	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, err = eng.Parse(ctx, "object", "application/json", body, Spec{})
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("expected the second request to run out of time")
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("error = %v, want a deadline error", err)
	}
	// The second request has to give up while the first parse is still
	// running: it must not wait for the parse of the request in front of
	// it.
	select {
	case <-firstDone:
		t.Fatalf("the second request waited for the first parse (%dms)", elapsed.Milliseconds())
	default:
	}
	if elapsed > 200*time.Millisecond {
		t.Errorf("the second request waited %dms for the worker, it must stop at its deadline",
			elapsed.Milliseconds())
	}
	if err := <-first; err != nil {
		t.Errorf("the first request failed: %v", err)
	}

	// The worker is still usable after a request gave up: the late response
	// of the abandoned request is dropped, not handed to the next one.
	ctx2, cancel2 := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel2()
	res, err := eng.Parse(ctx2, "object", "application/json", []byte(`{"items":[{"a":1}]}`),
		Spec{ItemPath: "items"})
	if err != nil {
		t.Fatalf("the worker is not usable after a timeout: %v", err)
	}
	list, ok := res.Data.([]any)
	if !ok || len(list) != 1 {
		t.Fatalf("data = %#v", res.Data)
	}
}

// TestTheEnginesAgreeOnTheXMLProjection checks that both planes produce the
// same bytes for the XML projection, so that the same request produces the
// same answer whichever engine is active.
func TestTheEnginesAgreeOnTheXMLProjection(t *testing.T) {
	python, srcDir := pythonPlane(t)
	py, err := StartPython(context.Background(), python, srcDir)
	if err != nil {
		t.Skipf("the python parsing plane is not available: %v", err)
	}
	t.Cleanup(func() { _ = py.Close() })

	body := []byte("<root><a>1</a></root>")
	spec := Spec{}
	resPy, err := py.Parse(context.Background(), "xml", "application/xml", body, spec)
	if err != nil {
		t.Fatal(err)
	}
	resGo, err := NewGoEngine(100).Parse(context.Background(), "xml", "application/xml", body, spec)
	if err != nil {
		t.Fatal(err)
	}
	bPy, err := Marshal(resPy.Data)
	if err != nil {
		t.Fatal(err)
	}
	bGo, err := Marshal(resGo.Data)
	if err != nil {
		t.Fatal(err)
	}
	if string(bPy) != string(bGo) {
		t.Errorf("python = %s\ngo     = %s", bPy, bGo)
	}
	want := fmt.Sprintf(`{"_xml_bytes":%d,"_truncated":false}`, len(body))
	if string(bGo) != want {
		t.Errorf("go = %s, want %s", bGo, want)
	}
}

// TestPythonEngineKeepsTheResponseOrder checks that the responses of the
// worker are handed to the requests in order and never interleaved.
func TestPythonEngineKeepsTheResponseOrder(t *testing.T) {
	python, srcDir := pythonPlane(t)
	eng, err := StartPython(context.Background(), python, srcDir)
	if err != nil {
		t.Skipf("the python parsing plane is not available: %v", err)
	}
	t.Cleanup(func() { _ = eng.Close() })

	for i := 0; i < 5; i++ {
		body := []byte(`{"message":{"items":[{"n":"` + string(rune('0'+i)) + `"}]}}`)
		res, err := eng.Parse(context.Background(), "object", "application/json", body,
			Spec{ItemPath: "message.items"})
		if err != nil {
			t.Fatalf("parse %d: %v", i, err)
		}
		list, ok := res.Data.([]any)
		if !ok || len(list) != 1 {
			t.Fatalf("parse %d: data = %#v", i, res.Data)
		}
		row, ok := list[0].(*Object)
		if !ok {
			t.Fatalf("parse %d: row = %T", i, list[0])
		}
		v, _ := row.Get("n")
		if v != string(rune('0'+i)) {
			t.Fatalf("parse %d: n = %v, the responses are not in order", i, v)
		}
	}
}
