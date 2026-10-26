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
//
//	"body_b64":"...","spec":{...}}
//
// Response: {"id":"...","ok":true,"items":[...],"total":123,"truncated":false,
//
//	"fields":[...],"elapsed_ms":3.2}
//	{"id":"...","ok":false,"error":"...","error_type":"ParseError"}
//
// "op":"ping" and "op":"shutdown" are supported as well.
//
// The error type "NotJSON" means the body was not JSON (the upstream
// answered an HTML error page for example). It is mapped onto the very
// same error the Go engine produces, so that the envelope of a call looks
// the same whichever engine is active.
const (
	workerModule = "a_source_gateway.parser_worker"
	pingTimeout  = 5 * time.Second
	// closeWait is how long the shutdown waits for the worker slot before
	// it stops the worker without the polite shutdown request.
	closeWait = 500 * time.Millisecond
	// workerErrorTypeNotJSON is the error type the worker uses for a body
	// that is not JSON.
	workerErrorTypeNotJSON = "NotJSON"
)

// PythonEngine talks to the Python parsing plane.
type PythonEngine struct {
	python string
	srcDir string

	// sem serialises the protocol. The worker answers one request at a
	// time, so the gateway keeps a single request in flight and the
	// responses stay in order. The acquisition is context aware: a request
	// whose deadline expires while it waits stops waiting instead of
	// inheriting the parse time of the request in front of it.
	sem chan struct{}

	mu      sync.Mutex
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	stdout  *bufio.Reader
	pending map[string]chan workerReply
	seq     int
	broken  bool
}

// workerReply is one response line of the worker.
type workerReply struct {
	obj *Object
	err error
}

// StartPython launches the Python worker and verifies that it answers a ping.
//
// It returns an error when the worker is missing or fails to start; callers
// are expected to fall back to the Go engine.
func StartPython(ctx context.Context, python, srcDir string) (*PythonEngine, error) {
	if python == "" {
		python = "python3"
	}
	e := &PythonEngine{
		python:  python,
		srcDir:  srcDir,
		sem:     make(chan struct{}, 1),
		pending: map[string]chan workerReply{},
	}
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
	go e.readLoop()

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
	cmd := e.cmd
	stdin := e.stdin
	broken := e.broken
	e.mu.Unlock()
	if cmd == nil {
		return nil
	}
	// Ask the worker to stop. The request is best effort: the worker also
	// exits when its stdin is closed.
	if !broken && stdin != nil {
		select {
		case e.sem <- struct{}{}:
			e.mu.Lock()
			_, _ = e.writeLine(map[string]any{"op": "shutdown", "id": e.nextID()})
			e.mu.Unlock()
			<-e.sem
		case <-time.After(closeWait):
			// A request is still in flight; skip the polite shutdown.
		}
	}
	if stdin != nil {
		_ = stdin.Close()
	}
	done := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		<-done
	}
	e.mu.Lock()
	e.cmd = nil
	e.mu.Unlock()
	return nil
}

// Parse implements Engine.
func (e *PythonEngine) Parse(ctx context.Context, shape, mediaType string, body []byte, spec Spec) (*Result, error) {
	encoded, err := encodeBody(ctx, body)
	if err != nil {
		return nil, err
	}
	req := map[string]any{
		"op":         "parse",
		"shape":      shape,
		"media_type": mediaType,
		"body_b64":   encoded,
		"trace_id":   spec.TraceID,
		"spec": map[string]any{
			"item_path":    spec.ItemPath,
			"total_field":  spec.TotalField,
			"item_shape":   spec.ItemShape,
			"item_flatten": spec.ItemFlatten,
			"limit":        spec.Limit,
			"fields":       spec.Fields,
			"max_rows":     spec.MaxRows,
		},
	}
	obj, err := e.roundTrip(ctx, req)
	if err != nil {
		return nil, err
	}
	return resultFromWire(obj)
}

// encodeBody encodes the body for the wire.
//
// The encoding of a big body is not free, so it is done in chunks and the
// deadline is honoured between the chunks: the encoding belongs to the
// parse phase and must not push the call past its budget.
func encodeBody(ctx context.Context, body []byte) (string, error) {
	var b strings.Builder
	b.Grow(base64.StdEncoding.EncodedLen(len(body)))
	enc := base64.NewEncoder(base64.StdEncoding, &b)
	const chunk = 96 * 1024 // a multiple of three, so the encoder holds nothing back
	for i := 0; i < len(body); i += chunk {
		end := i + chunk
		if end > len(body) {
			end = len(body)
		}
		if _, err := enc.Write(body[i:end]); err != nil {
			return "", err
		}
		if err := ctx.Err(); err != nil {
			return "", err
		}
	}
	if err := enc.Close(); err != nil {
		return "", err
	}
	return b.String(), nil
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
		// The worker reports a body that is not JSON with the error type
		// "NotJSON". It becomes the very same error the Go engine
		// produces, so that the envelope of a call looks the same
		// whichever engine is active.
		if errType, _ := obj.Get("error_type"); errType == workerErrorTypeNotJSON {
			// The worker sends the excerpt of the body in its own field;
			// the message of the worker is only the fallback.
			preview, _ := obj.Get("body_preview")
			text, _ := preview.(string)
			if text == "" {
				text = s
			}
			return nil, NewNotJSONError(notJSONSnippet([]byte(text)), errors.New(s))
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

// readLoop reads the response lines of the worker and hands them to the
// waiting requests.
//
// There is exactly one reader for the whole worker: the responses are
// matched by id, so the response of a request that gave up (its deadline
// expired) is dropped instead of being handed to the next request.
func (e *PythonEngine) readLoop() {
	for {
		line, err := e.stdout.ReadBytes('\n')
		if err != nil {
			e.mu.Lock()
			e.broken = true
			e.mu.Unlock()
			e.failPending(err)
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
		e.mu.Lock()
		ch, waiting := e.pending[s]
		if waiting {
			delete(e.pending, s)
		}
		e.mu.Unlock()
		if !waiting {
			continue // a late response of an abandoned request
		}
		ch <- workerReply{obj: obj}
	}
}

// failPending wakes up the requests that wait for a response of a worker
// that died.
func (e *PythonEngine) failPending(err error) {
	e.mu.Lock()
	pending := e.pending
	e.pending = map[string]chan workerReply{}
	e.mu.Unlock()
	for _, ch := range pending {
		ch <- workerReply{err: err}
	}
}

// roundTrip sends one request and waits for the matching response.
//
// The worker slot is acquired in a context aware way: a request whose
// deadline expires while it waits for the slot stops waiting and reports
// the deadline, instead of blocking until the request in front of it has
// finished its parse.
func (e *PythonEngine) roundTrip(ctx context.Context, req map[string]any) (*Object, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	select {
	case e.sem <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	defer func() { <-e.sem }()

	e.mu.Lock()
	if e.broken || e.cmd == nil {
		e.mu.Unlock()
		return nil, errors.New("python parser worker is not running")
	}
	req["id"] = e.nextID()
	id, _ := req["id"].(string)
	ch := make(chan workerReply, 1)
	e.pending[id] = ch
	if _, err := e.writeLine(req); err != nil {
		delete(e.pending, id)
		e.broken = true
		e.mu.Unlock()
		return nil, err
	}
	e.mu.Unlock()

	select {
	case <-ctx.Done():
		// The request gave up: the worker is still busy, but its late
		// response is dropped by the reader, so the worker stays usable.
		e.mu.Lock()
		delete(e.pending, id)
		e.mu.Unlock()
		return nil, ctx.Err()
	case r := <-ch:
		if r.err != nil {
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
