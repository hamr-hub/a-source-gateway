package parser

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

// Python worker protocol (newline-delimited JSON over stdin/stdout).
//
// Request:  {"id":"...","op":"parse","shape":"...","media_type":"...",
//	          "body_b64":"...","spec":{...}}
// Response: {"id":"...","ok":true,"items":[...],"total":123,"truncated":false,
//	          "fields":[...],"elapsed_ms":3.2}
//	          {"id":"...","ok":false,"error":"..."}
//
// "op":"ping" and "op":"shutdown" are supported as well.
const (
	workerModule = "a_source_gateway.parser_worker"
	pingTimeout  = 5 * time.Second
)

// PythonEngine talks to the Python parsing plane.
type PythonEngine struct {
	python string
	srcDir string

	mu     sync.Mutex
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Reader
	seq    int
	broken bool
}

// StartPython launches the Python worker and verifies that it answers a ping.
//
// It returns an error when the worker is missing or fails to start; callers
// are expected to fall back to the Go engine.
func StartPython(ctx context.Context, python, srcDir string) (*PythonEngine, error) {
	if python == "" {
		python = "python3"
	}
	e := &PythonEngine{python: python, srcDir: srcDir}
	cmd := exec.Command(python, "-m", workerModule)
	env := os.Environ()
	if srcDir != "" {
		env = append(env, "PYTHONPATH="+srcDir+string(os.PathListSeparator)+os.Getenv("PYTHONPATH"))
	}
	env = append(env, "PYTHONUNBUFFERED=1")
	cmd.Env = env
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("python worker stdin: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("python worker stdout: %w", err)
	}
	cmd.Stderr = &prefixWriter{prefix: "[parser] ", out: os.Stderr}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("python worker start: %w", err)
	}
	e.cmd = cmd
	e.stdin = stdin
	e.stdout = bufio.NewReader(stdout)

	pingCtx, cancel := context.WithTimeout(ctx, pingTimeout)
	defer cancel()
	if _, err := e.roundTrip(pingCtx, map[string]any{"op": "ping"}); err != nil {
		e.Close()
		return nil, fmt.Errorf("python worker ping: %w", err)
	}
	return e, nil
}

// Name implements Engine.
func (e *PythonEngine) Name() string { return "python" }

// Close shuts the worker down.
func (e *PythonEngine) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.cmd == nil {
		return nil
	}
	if !e.broken && e.stdin != nil {
		_, _ = e.writeLine(map[string]any{"op": "shutdown", "id": e.nextID()})
	}
	if e.stdin != nil {
		_ = e.stdin.Close()
	}
	done := make(chan struct{})
	go func() {
		_ = e.cmd.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		if e.cmd.Process != nil {
			_ = e.cmd.Process.Kill()
		}
		<-done
	}
	e.cmd = nil
	return nil
}

// Parse implements Engine.
func (e *PythonEngine) Parse(ctx context.Context, shape, mediaType string, body []byte, spec Spec) (*Result, error) {
	req := map[string]any{
		"op":         "parse",
		"shape":      shape,
		"media_type": mediaType,
		"body_b64":   base64.StdEncoding.EncodeToString(body),
		"trace_id":   spec.TraceID,
		"spec": map[string]any{
			"item_path":   spec.ItemPath,
			"total_field": spec.TotalField,
			"item_shape":  spec.ItemShape,
			"limit":       spec.Limit,
			"fields":      spec.Fields,
			"max_rows":    spec.MaxRows,
		},
	}
	obj, err := e.roundTrip(ctx, req)
	if err != nil {
		return nil, err
	}
	return resultFromWire(obj)
}

// resultFromWire maps a worker response onto a Result.
func resultFromWire(obj *Object) (*Result, error) {
	okVal, _ := obj.Get("ok")
	ok, _ := okVal.(bool)
	if !ok {
		msg, _ := obj.Get("error")
		s, _ := msg.(string)
		if s == "" {
			s = "python parser worker reported a failure"
		}
		return nil, errors.New(s)
	}
	res := &Result{}
	if data, ok := obj.Get("data"); ok && data != nil {
		res.Data = data
	}
	items, hasItems := obj.Get("items")
	if hasItems {
		if list, ok := items.([]any); ok {
			res.Items = list
			if res.Data == nil {
				res.Data = list
			}
		} else if res.Data == nil {
			res.Data = items
		}
	}
	if res.Data == nil {
		res.Data = []any{}
	}
	if total, ok := obj.Get("total"); ok && total != nil {
		if n, ok := ToInt(total); ok {
			res.Total = &n
		}
	}
	if trunc, ok := obj.Get("truncated"); ok {
		res.Truncated, _ = trunc.(bool)
	}
	if fields, ok := obj.Get("fields"); ok {
		if list, ok := fields.([]any); ok {
			for _, f := range list {
				if s, ok := f.(string); ok {
					res.Fields = append(res.Fields, s)
				}
			}
		}
	}
	if elapsed, ok := obj.Get("elapsed_ms"); ok {
		if f, ok := NumberOf(elapsed); ok {
			res.ElapsedMS = f
		}
	}
	return res, nil
}

func (e *PythonEngine) nextID() string {
	e.seq++
	return fmt.Sprintf("gw-%d", e.seq)
}

func (e *PythonEngine) writeLine(v any) ([]byte, error) {
	b, err := Marshal(v)
	if err != nil {
		return nil, err
	}
	b = append(b, '\n')
	if _, err := e.stdin.Write(b); err != nil {
		return nil, err
	}
	return b, nil
}

// roundTrip sends one request and waits for the matching response.
//
// The worker mutex serialises requests: the worker processes them one at a
// time anyway, and serialising keeps the request/response correlation simple.
func (e *PythonEngine) roundTrip(ctx context.Context, req map[string]any) (*Object, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.broken || e.cmd == nil {
		return nil, errors.New("python parser worker is not running")
	}
	req["id"] = e.nextID()
	id, _ := req["id"].(string)
	if _, err := e.writeLine(req); err != nil {
		e.broken = true
		return nil, err
	}

	type readResult struct {
		obj *Object
		err error
	}
	ch := make(chan readResult, 1)
	go func() {
		for {
			line, err := e.stdout.ReadBytes('\n')
			if err != nil {
				ch <- readResult{nil, err}
				return
			}
			line = bytes.TrimSpace(line)
			if len(line) == 0 {
				continue
			}
			v, err := UnmarshalOrdered(line)
			if err != nil {
				continue // not a protocol line, ignore
			}
			obj, ok := v.(*Object)
			if !ok {
				continue
			}
			gotID, _ := obj.Get("id")
			s, _ := gotID.(string)
			if s != "" && s != id {
				continue // a late response of an abandoned request
			}
			ch <- readResult{obj, nil}
			return
		}
	}()

	select {
	case <-ctx.Done():
		e.broken = true
		return nil, ctx.Err()
	case r := <-ch:
		if r.err != nil {
			e.broken = true
			return nil, r.err
		}
		return r.obj, nil
	}
}

// prefixWriter prefixes every written line, used for the worker stderr.
type prefixWriter struct {
	prefix string
	out    io.Writer
	mu     sync.Mutex
	buf    []byte
}

func (w *prefixWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf = append(w.buf, p...)
	for {
		idx := bytes.IndexByte(w.buf, '\n')
		if idx < 0 {
			break
		}
		line := strings.TrimRight(string(w.buf[:idx]), "\r")
		w.buf = w.buf[idx+1:]
		fmt.Fprintf(w.out, "%s%s\n", w.prefix, line)
	}
	return len(p), nil
}
