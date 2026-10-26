// Package parser implements the parsing plane of the gateway.
//
// Two engines are available:
//
//   - the Python engine (primary): a `python3 -m a_source_gateway.parser_worker`
//     subprocess speaking newline-delimited JSON over stdin/stdout;
//   - the Go engine (fallback): a pure Go implementation of the same parse and
//     project semantics.
//
// Both engines return the same shape, so the rest of the gateway does not
// care which one is active. The active engine is reported in meta.parser.
package parser

import (
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"
)

// Limits copied from the Python reference (Resolver._parse).
const (
	// JSONLimitBytes caps the raw XML payload that is passed through.
	JSONLimitBytes = 4 * 1024 * 1024
	// TextLimitBytes caps the raw text payload that is passed through.
	TextLimitBytes = 200000
	// JSONLMaxRows caps the number of parsed JSON Lines records.
	JSONLMaxRows = 15000
	// NotJSONSnippetRunes caps the excerpt of the body that is carried by
	// a NotJSONError. It is long enough to recognise an HTML error page
	// and short enough for one log line.
	NotJSONSnippetRunes = 240
)

// ErrNotJSON reports that an upstream body is not JSON.
//
// An upstream that answers with an HTML error page (a gateway error, a
// captcha, a maintenance page) is not an empty result: the caller has to
// be able to tell the two apart. The error is detectable with errors.Is.
var ErrNotJSON = errors.New("上游响应不是 JSON")

// NotJSONError is the concrete form of ErrNotJSON: it carries an excerpt of
// the body, so that the reason is visible at a glance ("<!DOCTYPE html>…").
type NotJSONError struct {
	// Snippet is a single line excerpt of the upstream body.
	Snippet string
	// Cause is the underlying decoding error.
	Cause error
}

// Error implements error.
//
// The excerpt is not repeated when the cause already carries it (the
// Python worker puts the excerpt into its message).
func (e *NotJSONError) Error() string {
	msg := "上游响应不是 JSON"
	if e.Cause != nil && e.Snippet != "" && !strings.Contains(e.Cause.Error(), e.Snippet) {
		msg += ": " + e.Cause.Error()
	} else if e.Cause != nil && e.Snippet == "" {
		msg += ": " + e.Cause.Error()
	}
	if e.Snippet != "" {
		msg += "; 正文片段: " + e.Snippet
	}
	return msg
}

// Unwrap makes errors.Is(err, ErrNotJSON) true.
func (e *NotJSONError) Unwrap() error { return ErrNotJSON }

// NewNotJSONError builds a NotJSONError out of a body excerpt.
func NewNotJSONError(snippet string, cause error) error {
	return &NotJSONError{Snippet: snippet, Cause: cause}
}

// notJSONSnippet turns the head of an upstream body into a single line
// excerpt: newlines are flattened and the length is capped, so that an HTML
// error page stays readable inside one log line.
func notJSONSnippet(body []byte) string {
	raw := string(body)
	if len(raw) > NotJSONSnippetRunes*4 {
		raw = raw[:NotJSONSnippetRunes*4]
	}
	raw = strings.ToValidUTF8(raw, "")
	flat := strings.Map(func(r rune) rune {
		switch r {
		case '\n', '\r', '\t':
			return ' '
		}
		return r
	}, raw)
	flat = strings.TrimSpace(flat)
	rs := []rune(flat)
	if len(rs) > NotJSONSnippetRunes {
		return string(rs[:NotJSONSnippetRunes]) + "…"
	}
	return flat
}

// Spec describes where the items live inside an upstream payload and how
// they have to be trimmed. It maps 1:1 onto the "spec" object of the Python
// worker protocol.
type Spec struct {
	ItemPath   string `json:"item_path"`
	TotalField string `json:"total_field"`
	ItemShape  string `json:"item_shape"`
	// ItemFlatten flattens one level when the located list holds lists
	// (e.g. the SSE bulletin index nests each record in its own array).
	ItemFlatten bool     `json:"item_flatten"`
	Limit       int      `json:"limit"`
	Fields      []string `json:"fields"`
	MaxRows     int      `json:"max_rows"`
	// TraceID correlates the parse with the gateway request. It is not part
	// of the "spec" object of the protocol; the Python engine sends it as an
	// extra top level field of the request.
	TraceID string `json:"-"`
}

// Result is the outcome of a parse.
type Result struct {
	// Items holds the parsed records (empty when the payload is not a list).
	Items []any
	// Data holds the projected payload; for list payloads it equals Items.
	Data any
	// Total is the upstream record count when the endpoint exposes one.
	Total *int
	// Truncated reports whether the upstream payload held more records.
	Truncated bool
	// Fields lists the projected field names (informational).
	Fields []string
	// ElapsedMS is the parse duration.
	ElapsedMS float64
}

// Engine parses an upstream body into records.
type Engine interface {
	// Name is "python" or "go" and is reported in meta.parser.
	Name() string
	// Parse parses body according to shape and projects it per spec.
	Parse(ctx context.Context, shape, mediaType string, body []byte, spec Spec) (*Result, error)
	// Close releases the engine resources.
	Close() error
}

// GoEngine is the pure Go fallback engine.
type GoEngine struct {
	// MaxRows is the parse guard for tabular payloads.
	MaxRows int
}

// NewGoEngine returns a Go engine using the given parse guard.
func NewGoEngine(maxRows int) *GoEngine {
	if maxRows <= 0 {
		maxRows = JSONLMaxRows
	}
	return &GoEngine{MaxRows: maxRows}
}

// Name implements Engine.
func (g *GoEngine) Name() string { return "go" }

// Close implements Engine.
func (g *GoEngine) Close() error { return nil }

// Parse implements Engine.
func (g *GoEngine) Parse(ctx context.Context, shape, mediaType string, body []byte, spec Spec) (*Result, error) {
	start := time.Now()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	payload, err := g.parseBody(ctx, shape, body, spec.MaxRows)
	if err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	data, total := Extract(payload, spec)
	data, total, truncated := Trim(data, total, spec.Limit, spec.Fields, false)
	res := &Result{Data: data, Total: total, Truncated: truncated, Fields: spec.Fields}
	if list, ok := data.([]any); ok {
		res.Items = list
	}
	res.ElapsedMS = float64(time.Since(start).Microseconds()) / 1000.0
	return res, nil
}

// parseBody turns the raw upstream body into a decoded payload.
//
// It mirrors Resolver._parse of the Python reference: CSV/TSV become lists of
// rows, JSON Lines become a list of objects, XML and text are passed through
// as markers, and everything else is parsed as JSON.
//
// A JSON shape whose body is not JSON is **not** an empty result: it is
// reported as ErrNotJSON, so that the gateway can degrade the call and hand
// out the direct upstream fallback instead of an empty envelope. The XML and
// text shapes keep their pass through behaviour: there the body is not
// supposed to be JSON at all.
//
// The context bounds the work: the decoding of a big document is not free
// and must not push the call past its budget.
func (g *GoEngine) parseBody(ctx context.Context, shape string, body []byte, maxRows int) (any, error) {
	switch shape {
	case "csv", "tsv":
		delim := ','
		if shape == "tsv" {
			delim = '\t'
		}
		return parseDelimited(ctx, string(body), delim, maxRows)
	case "jsonl":
		return parseJSONL(ctx, string(body))
	case "xml":
		return NewObjectWith("_xml", truncateBytes(body, JSONLimitBytes)), nil
	case "text":
		return NewObjectWith("_text", truncateBytes(body, TextLimitBytes)), nil
	}
	v, err := UnmarshalOrderedContext(ctx, body)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		// The upstream answered something that is not JSON (an HTML error
		// page for example). Saying so is the only honest answer: an
		// empty envelope would look like "the source has no data".
		return nil, NewNotJSONError(notJSONSnippet(body), err)
	}
	return v, nil
}

// NewObjectWith builds a single-key ordered object.
func NewObjectWith(key string, val any) *Object {
	o := NewObject()
	o.Set(key, val)
	return o
}

func truncateBytes(b []byte, limit int) string {
	if len(b) > limit {
		b = b[:limit]
	}
	return string(b)
}

func parseDelimited(ctx context.Context, body string, delim rune, maxRows int) ([]any, error) {
	if maxRows <= 0 {
		maxRows = JSONLMaxRows
	}
	rows := []any{}
	r := csv.NewReader(strings.NewReader(body))
	r.Comma = delim
	r.FieldsPerRecord = -1
	r.LazyQuotes = true
	for i := 0; i < maxRows; i++ {
		if i%checkEvery == 0 {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
		}
		rec, err := r.Read()
		if err != nil {
			break
		}
		row := make([]any, len(rec))
		for j, f := range rec {
			row[j] = f
		}
		rows = append(rows, row)
	}
	return rows, nil
}

func parseJSONL(ctx context.Context, body string) ([]any, error) {
	out := []any{}
	for _, line := range strings.Split(body, "\n") {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		v, err := UnmarshalOrderedContext(ctx, []byte(line))
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			continue
		}
		out = append(out, v)
		if len(out) >= JSONLMaxRows {
			break
		}
	}
	return out, nil
}

// Extract locates the item list and the total inside a decoded payload.
//
// It mirrors the first half of Resolver._project. Both item_path and
// total_field accept numeric list indices ("0.total"): a top level array
// payload is located like an object payload, so a declared item_path is
// never silently ignored.
func Extract(payload any, spec Spec) (any, *int) {
	var total *int
	if spec.TotalField != "" {
		if node, ok := lookupTotalPath(payload, spec.TotalField); ok && IsNumber(node) {
			n, _ := ToInt(node)
			total = &n
		}
	}

	data := payload
	switch {
	case spec.ItemShape == "map":
		if obj, ok := payload.(*Object); ok {
			data = obj.Values()
		}
	case spec.ItemPath != "":
		if node, ok := lookupTotalPath(payload, spec.ItemPath); ok && node != nil {
			data = node
		} else if arr, isArr := payload.([]any); isArr && !numericHead(spec.ItemPath) {
			// 顶层是数组且路径首段不是数字: 保持整个数组(与 Python 引擎一致)
			data = arr
		} else {
			data = []any{}
		}
		if spec.ItemFlatten {
			data = flattenOneLevel(data)
		}
	}

	// The XML passthrough marker replaces the payload with a size summary,
	// but only when the payload did not yield a list of items.
	if _, isList := data.([]any); !isList {
		if obj, ok := payload.(*Object); ok {
			if raw, ok := obj.Get("_xml"); ok {
				s, _ := raw.(string)
				// The key order is the one of the Python reference
				// ({"_xml_bytes": N, "_truncated": bool}), so that the
				// same request produces the same bytes whichever engine
				// is active.
				summary := NewObject()
				summary.Set("_xml_bytes", len([]byte(s)))
				summary.Set("_truncated", utf8.RuneCountInString(s) >= JSONLimitBytes)
				data = summary
			}
		}
	}
	return data, total
}

// Trim applies the limit/fields projection to an extracted item list.
//
// It mirrors the second half of Resolver._project: a limit truncates the
// list and flags the result as truncated, a total larger than the list also
// flags it, and fields keeps only the requested keys of every record.
func Trim(data any, total *int, limit int, fields []string, truncatedIn bool) (any, *int, bool) {
	truncated := truncatedIn
	list, ok := data.([]any)
	if !ok {
		return data, total, truncated
	}
	if total == nil {
		n := len(list)
		total = &n
	}
	if limit > 0 && len(list) > limit {
		list = list[:limit]
		truncated = true
	} else if total != nil && *total > len(list) {
		truncated = true
	}
	if len(fields) > 0 && len(list) > 0 {
		if _, isObj := list[0].(*Object); isObj {
			keep := make(map[string]bool, len(fields))
			for _, f := range fields {
				keep[f] = true
			}
			out := make([]any, len(list))
			for i, row := range list {
				obj, ok := row.(*Object)
				if !ok {
					out[i] = row
					continue
				}
				proj := NewObject()
				for _, k := range obj.Keys() {
					if keep[k] {
						v, _ := obj.Get(k)
						proj.Set(k, v)
					}
				}
				out[i] = proj
			}
			list = out
		}
	}
	return list, total, truncated
}

// lookupLiteralKey looks the whole path up as one literal key.
//
// Upstream keys do not always follow the dotted convention: OData uses
// "@odata.count" and "@odata.nextLink", whose names contain a dot. Splitting
// such a path on "." can never reach the value.
func lookupLiteralKey(node any, path string) (any, bool) {
	obj, ok := node.(*Object)
	if !ok {
		return nil, false
	}
	v, found := obj.Get(path)
	if !found {
		return nil, false
	}
	return v, true
}

func lookupTotalPath(node any, path string) (any, bool) {
	root := node
	for _, part := range strings.Split(path, ".") {
		switch t := node.(type) {
		case *Object:
			v, ok := t.Get(part)
			if !ok {
				return lookupLiteralKey(root, path)
			}
			node = v
		case []any:
			idx, err := parseDigits(part)
			if err != nil {
				return lookupLiteralKey(root, path)
			}
			if idx >= len(t) {
				return lookupLiteralKey(root, path)
			}
			node = t[idx]
		default:
			return lookupLiteralKey(root, path)
		}
	}
	return node, true
}

// numericHead reports whether the first segment of a dotted path is an index.
func numericHead(path string) bool {
	head := path
	if i := strings.IndexByte(path, '.'); i >= 0 {
		head = path[:i]
	}
	_, err := parseDigits(head)
	return err == nil
}

// flattenOneLevel flattens a list of lists into a single list. It is a
// no-op for anything else, so a wrong guess never corrupts the payload.
func flattenOneLevel(data any) any {
	list, ok := data.([]any)
	if !ok || len(list) == 0 {
		return data
	}
	if _, isList := list[0].([]any); !isList {
		return data
	}
	flat := make([]any, 0, len(list))
	for _, sub := range list {
		if sl, ok := sub.([]any); ok {
			flat = append(flat, sl...)
		} else {
			flat = append(flat, sub)
		}
	}
	return flat
}

func parseDigits(s string) (int, error) {
	if s == "" {
		return 0, fmt.Errorf("empty index")
	}
	n := 0
	for _, r := range s {
		if r < '0' || r > '9' {
			return 0, fmt.Errorf("not a digit: %q", s)
		}
		n = n*10 + int(r-'0')
	}
	return n, nil
}

// PyRepr exposes the Python-compatible repr used for cache keys.
func PyRepr(v any) string { return pyRepr(v) }

// NumberOf converts a JSON number to its float value.
func NumberOf(v any) (float64, bool) {
	switch t := v.(type) {
	case json.Number:
		f, err := t.Float64()
		return f, err == nil
	case int:
		return float64(t), true
	case int64:
		return float64(t), true
	case float64:
		return t, true
	}
	return 0, false
}
