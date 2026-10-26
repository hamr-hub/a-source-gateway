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
	"bytes"
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
//
// The wording is the same on both planes (Go engine, Python worker) and in
// the Python reference: the envelope of a call must not depend on which
// engine produced it.
var ErrNotJSON = errors.New("上游返回的不是 JSON(疑似 HTML 错误页)")

// NotJSONError is the concrete form of ErrNotJSON: it carries an excerpt of
// the body, so that the reason is visible at a glance ("<!DOCTYPE html>…").
type NotJSONError struct {
	// Snippet is a single line excerpt of the upstream body.
	Snippet string
	// Cause is the underlying decoding error. It is kept for the logs;
	// it is not rendered into the message, because the raw decoder error
	// ("invalid character '<' …") says less than the excerpt and differs
	// between the engines.
	Cause error
}

// Error implements error.
//
// The message is one template on every plane:
//
//	上游返回的不是 JSON(疑似 HTML 错误页); 正文片段: <片段>
func (e *NotJSONError) Error() string {
	msg := "上游返回的不是 JSON(疑似 HTML 错误页)"
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

// ErrNotShape reports that an upstream body does not have the shape the
// endpoint declares (CSV / TSV / JSON Lines).
//
// It is the sibling of ErrNotJSON: an upstream that answers with an HTML
// error page instead of the declared format is not an empty result. The
// caller has to be able to tell the two apart, so the error is detectable
// with errors.Is.
//
// The wording is the same on both planes (Go engine, Python worker) and in
// the Python reference: the envelope of a call must not depend on which
// engine produced it.
var ErrNotShape = errors.New("上游返回的不是声明的形状")

// shapeMismatchPrefixes holds the wording of the shape mismatch, one entry
// per shape. The three sentences are a contract with the Python plane: they
// are written verbatim there too, and the manager recognises a worker answer
// by them (see parseShapeMismatch).
var shapeMismatchPrefixes = []struct {
	shape  string
	prefix string
}{
	{"csv", "上游返回的不是 CSV(疑似 XML/HTML 错误页)"},
	{"tsv", "上游返回的不是 TSV(疑似 XML/HTML 错误页)"},
	{"jsonl", "上游返回的不是 JSONL(疑似 HTML 错误页)"},
}

// NotShapeError is the concrete form of ErrNotShape: it carries the shape
// the endpoint declared and an excerpt of the body, so that the reason is
// visible at a glance ("<!DOCTYPE html>…").
type NotShapeError struct {
	// Shape is the declared shape of the endpoint ("csv", "tsv", "jsonl").
	Shape string
	// Snippet is a single line excerpt of the upstream body.
	Snippet string
	// Cause is the underlying reason. It is kept for the logs; it is not
	// rendered into the message, because the excerpt says more than a
	// decoder error does.
	Cause error
}

// Error implements error.
//
// The message is one template per shape:
//
//	上游返回的不是 CSV(疑似 XML/HTML 错误页); 正文片段: <片段>
//	上游返回的不是 TSV(疑似 XML/HTML 错误页); 正文片段: <片段>
//	上游返回的不是 JSONL(疑似 HTML 错误页); 正文片段: <片段>
//
// An empty body adds no excerpt, exactly like NotJSONError does.
func (e *NotShapeError) Error() string {
	msg := notShapePrefix(e.Shape)
	if e.Snippet != "" {
		msg += "; 正文片段: " + e.Snippet
	}
	return msg
}

// Unwrap makes errors.Is(err, ErrNotShape) true.
func (e *NotShapeError) Unwrap() error { return ErrNotShape }

// NewNotShapeError builds a NotShapeError out of a body excerpt.
func NewNotShapeError(shape, snippet string, cause error) error {
	return &NotShapeError{Shape: shape, Snippet: snippet, Cause: cause}
}

// notShapePrefix returns the wording of a shape mismatch. An unknown shape
// (there is none today) falls back to a generic sentence, so that the error
// never renders an empty message.
func notShapePrefix(shape string) string {
	for _, p := range shapeMismatchPrefixes {
		if p.shape == shape {
			return p.prefix
		}
	}
	return "上游返回的不是 " + strings.ToUpper(shape) + "(疑似 XML/HTML 错误页)"
}

// parseShapeMismatch splits a shape mismatch sentence back into its shape
// and the excerpt of the body.
//
// The Python worker reports a shape mismatch with its own error type name,
// which the two planes do not have to agree on. The wording, on the other
// hand, is a contract: it is written verbatim on both sides. So the manager
// recognises a worker answer by the wording, and turns it into the very same
// error the Go engine produces.
func parseShapeMismatch(msg string) (shape string, snippet string, ok bool) {
	for _, p := range shapeMismatchPrefixes {
		idx := strings.Index(msg, p.prefix)
		if idx < 0 {
			continue
		}
		rest := msg[idx+len(p.prefix):]
		rest = strings.TrimPrefix(rest, "; 正文片段: ")
		return p.shape, notJSONSnippet([]byte(rest)), true
	}
	return "", "", false
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

// markupProbeBytes is the window in which a closing tag marker ("</") turns
// a body that starts with "<" into a markup document. It is wide enough for
// the head of a real error page and narrow enough to stay clear of a long
// CSV that happens to carry a "</" somewhere further down.
const markupProbeBytes = 512

// utf8BOM is the byte order mark some upstreams put in front of a document.
var utf8BOM = []byte{0xef, 0xbb, 0xbf}

// looksLikeMarkup reports whether the body looks like an XML/HTML document
// instead of the declared CSV/TSV.
//
// An upstream that answers with an error page does not say so in the status
// code: it answers 200 with a HTML page. Handing that page to the CSV reader
// would turn the error page into "data rows", which is worse than no answer
// at all. The test is deliberately narrow, so that a CSV field starting with
// "<" is not mistaken for markup.
func looksLikeMarkup(body []byte) bool {
	head := stripBOMAndSpace(body)
	if len(head) == 0 || head[0] != '<' {
		return false
	}
	if hasPrefixFold(head, "<!doctype") || hasPrefixFold(head, "<html") || hasPrefixFold(head, "<?xml") {
		return true
	}
	probe := head
	if len(probe) > markupProbeBytes {
		probe = probe[:markupProbeBytes]
	}
	return bytes.Contains(probe, []byte("</"))
}

// stripBOMAndSpace drops the byte order mark and the surrounding blanks.
//
// The order is the one of the Python plane (strip_bom_and_space): the blanks
// first, then the BOM, then the blanks again. Both planes have to reach the
// same conclusion for the same body.
func stripBOMAndSpace(body []byte) []byte {
	out := bytes.TrimSpace(body)
	out = bytes.TrimPrefix(out, utf8BOM)
	return bytes.TrimSpace(out)
}

// hasPrefixFold compares a prefix case insensitively (ASCII).
func hasPrefixFold(b []byte, prefix string) bool {
	if len(b) < len(prefix) {
		return false
	}
	return strings.EqualFold(string(b[:len(prefix)]), prefix)
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
// The same holds for the CSV/TSV/JSON Lines shapes: an upstream that answers
// with an HTML error page is reported as ErrNotShape. Handing that page to
// the CSV reader would turn the error page into "data rows", and dropping
// every JSON Lines row would look like "the source has no data". The XML and
// text shapes stay untouched: their contract is "here is the raw body".
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
		return parseDelimited(ctx, shape, body, delim, maxRows)
	case "jsonl":
		return parseJSONL(ctx, shape, body)
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

// parseDelimited parses a CSV/TSV body.
//
// A body that looks like an XML/HTML document is not a CSV/TSV: it is the
// error page of an upstream that answered 200. Reporting it as such keeps
// the error page out of the data.
func parseDelimited(ctx context.Context, shape string, body []byte, delim rune, maxRows int) ([]any, error) {
	if maxRows <= 0 {
		maxRows = JSONLMaxRows
	}
	if looksLikeMarkup(body) {
		return nil, NewNotShapeError(shape, notJSONSnippet(body), nil)
	}
	rows := []any{}
	r := csv.NewReader(bytes.NewReader(body))
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

// parseJSONL parses a JSON Lines body.
//
// A few broken lines are tolerated: they are skipped, the good lines are
// kept. But when **no** line can be parsed while the body does hold lines,
// the body is not JSON Lines at all (an HTML error page for example). That
// is reported, because "no data" and "the upstream sent an error page" are
// two different answers.
func parseJSONL(ctx context.Context, shape string, body []byte) ([]any, error) {
	out := []any{}
	lines := 0
	for _, line := range bytes.Split(body, []byte("\n")) {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		lines++
		v, err := UnmarshalOrderedContext(ctx, line)
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
	if lines > 0 && len(out) == 0 {
		return nil, NewNotShapeError(shape, notJSONSnippet(body), nil)
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
