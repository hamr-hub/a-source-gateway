// Package envelope implements the response envelope of the gateway.
//
// The shape mirrors ResponseEnvelope.to_dict() of the Python reference
// (python/src/a_source_gateway/schema.py): the same keys, in the same
// order, with the same null/empty semantics.
//
// The Go gateway adds two keys that the Python reference does not have:
// meta.trace_id (the correlation id of the request, taken from the inbound
// request or generated) and meta.parser (the name of the parsing engine
// that produced the data, "python" or "go"). Both are reported on every
// path, also on the early error returns.
package envelope

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
)

// SchemaVersion is the envelope schema version.
//
// 1.2: the source list projection carries the aliases of a source.
const SchemaVersion = "1.2"

// Upstream describes the upstream call of a query.
type Upstream struct {
	URL       string `json:"url"`
	Method    string `json:"method"`
	Status    *int   `json:"status"`
	LatencyMS int    `json:"latency_ms"`
}

// Error is one entry of meta.errors.
type Error struct {
	Type    string `json:"type"`
	Status  *int   `json:"status,omitempty"`
	Message string `json:"message"`
}

// Meta describes the request that produced the envelope.
type Meta struct {
	RequestID string `json:"request_id"`
	// TraceID correlates the request across the gateway logs, the upstream
	// call and the parser worker. It is taken from the inbound request
	// (X-Gateway-Trace-Id or X-Request-Id) or generated.
	TraceID       string `json:"trace_id"`
	SchemaVersion string `json:"schema_version"`
	Source        string `json:"source"`
	Endpoint      string `json:"endpoint"`
	// Parser names the engine that produced the data ("python" or "go").
	Parser     string         `json:"parser"`
	Upstream   Upstream       `json:"upstream"`
	LatencyMS  int            `json:"latency_ms"`
	BudgetMS   int            `json:"budget_ms"`
	Cached     bool           `json:"cached"`
	CacheAgeS  int            `json:"cache_age_s"`
	Degraded   bool           `json:"degraded"`
	Offline    bool           `json:"offline"`
	Count      int            `json:"count"`
	Total      *int           `json:"total"`
	Truncated  bool           `json:"truncated"`
	NextCursor *string        `json:"next_cursor"`
	RateLimit  *parser.Object `json:"rate_limit"`
	Fallback   *parser.Object `json:"fallback"`
	Errors     []Error        `json:"errors"`
}

// Envelope is the response envelope: meta describes the request, data holds
// the result, fields lists the projected field names.
type Envelope struct {
	Meta   Meta     `json:"meta"`
	Data   any      `json:"data"`
	Fields []string `json:"fields"`
}

// NewMeta returns a meta block with the same defaults as the Python
// reference (empty rate limit, empty fallback, empty error list).
func NewMeta() Meta {
	return Meta{
		SchemaVersion: SchemaVersion,
		Upstream:      Upstream{Method: "GET"},
		RateLimit:     parser.NewObject(),
		Fallback:      parser.NewObject(),
		Errors:        []Error{},
	}
}

// AddError appends an error to the meta block.
func (m *Meta) AddError(typ, message string) {
	m.Errors = append(m.Errors, Error{Type: typ, Message: message})
}

// AddErrorStatus appends an error that carries the upstream status code.
func (m *Meta) AddErrorStatus(typ string, status int, message string) {
	s := status
	m.Errors = append(m.Errors, Error{Type: typ, Status: &s, Message: message})
}

// SetFallback fills the direct-upstream fallback block.
func (m *Meta) SetFallback(upstreamURL, method, hint string) {
	fb := parser.NewObject()
	fb.Set("upstream_url", upstreamURL)
	fb.Set("method", method)
	fb.Set("hint", hint)
	m.Fallback = fb
}

// Marshal encodes a value as the compact JSON the gateway serves.
func Marshal(v any) ([]byte, error) {
	return parser.Marshal(v)
}

// NewTraceID returns a random 32 character hex trace id.
func NewTraceID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// The random source of the operating system is not expected to
		// fail; a time based fallback keeps the envelope complete.
		now := time.Now().UnixNano()
		return fmt.Sprintf("%032x", now)
	}
	return hex.EncodeToString(b[:])
}
