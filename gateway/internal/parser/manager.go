package parser

import (
	"context"
	"errors"
	"log"
	"sync"
)

// Manager selects and supervises the parsing engine.
//
// A_GATEWAY_PARSER selects the mode:
//
//	auto   - use the Python worker when it starts and answers a ping,
//	         otherwise fall back to the Go engine (default)
//	python - prefer the Python worker; fall back to Go if it is unavailable
//	go     - always use the Go engine
//
// The Go engine is always available as a safety net: if the Python worker
// fails mid-flight, the request is served by the Go engine and the gateway
// switches over permanently.
//
// A worker that answers correctly is not a failure: a body that is not JSON
// (an HTML error page of an upstream for example) comes back as the
// ErrNotJSON error and does not switch the engine over.
type Manager struct {
	mode     string
	logger   *log.Logger
	goEngine *GoEngine

	mu     sync.Mutex
	active Engine
}

// NewManager builds the engine manager and starts the selected engine.
func NewManager(ctx context.Context, mode, pythonPath, srcDir string, maxRows int, logger *log.Logger) *Manager {
	if logger == nil {
		logger = log.Default()
	}
	m := &Manager{mode: mode, logger: logger, goEngine: NewGoEngine(maxRows)}
	switch mode {
	case "go":
		m.active = m.goEngine
		return m
	}
	py, err := StartPython(ctx, pythonPath, srcDir)
	if err != nil {
		logger.Printf("warning: python parser worker unavailable (%v); using the Go fallback engine", err)
		m.active = m.goEngine
		return m
	}
	m.active = py
	logger.Printf("info: python parser worker ready (%s -m %s)", pythonPath, workerModule)
	return m
}

// Engine returns the currently active engine.
func (m *Manager) Engine() Engine {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.active
}

// Name returns the name of the active engine ("python" or "go").
func (m *Manager) Name() string {
	e := m.Engine()
	if e == nil {
		return "go"
	}
	return e.Name()
}

// Parse parses a body with the active engine, falling back to the Go engine
// when the Python worker fails.
func (m *Manager) Parse(ctx context.Context, shape, mediaType string, body []byte, spec Spec) (*Result, error) {
	active := m.Engine()
	if active == nil {
		return m.goEngine.Parse(ctx, shape, mediaType, body, spec)
	}
	res, err := active.Parse(ctx, shape, mediaType, body, spec)
	if err == nil {
		return res, nil
	}
	if active.Name() == "go" {
		return nil, err
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) || errors.Is(err, ErrNotJSON) {
		// The request ran out of time, or the upstream answered something
		// that is not JSON (an HTML error page for example): neither is the
		// worker's fault, the worker answered correctly.
		//
		// The Python plane stays active. A Go fallback would not have any
		// time left either when the deadline is gone, and an error page of
		// one upstream must not switch the parsing engine of the whole
		// process.
		return nil, err
	}
	m.logger.Printf("warning: python parser worker failed (%v); falling back to the Go engine", err)
	m.mu.Lock()
	if m.active != nil && m.active.Name() == "python" {
		if c, ok := m.active.(*PythonEngine); ok {
			_ = c.Close()
		}
		m.active = m.goEngine
	}
	m.mu.Unlock()
	return m.goEngine.Parse(ctx, shape, mediaType, body, spec)
}

// Close releases the active engine.
func (m *Manager) Close() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.active == nil {
		return nil
	}
	err := m.active.Close()
	m.active = m.goEngine
	return err
}
