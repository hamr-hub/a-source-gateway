package parser

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
)

func TestUnmarshalOrderedKeepsKeyOrderAndNumbers(t *testing.T) {
	raw := []byte(`{"zeta":1,"alpha":1726791,"mid":{"b":2,"a":1.5},"list":[3,2,1]}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatalf("UnmarshalOrdered: %v", err)
	}
	obj, ok := v.(*Object)
	if !ok {
		t.Fatalf("expected an object, got %T", v)
	}
	keys := obj.Keys()
	want := []string{"zeta", "alpha", "mid", "list"}
	for i := range want {
		if keys[i] != want[i] {
			t.Fatalf("keys = %v, want %v", keys, want)
		}
	}
	// The number literal must survive unchanged.
	total, ok := ToInt(mustGet(t, obj, "alpha"))
	if !ok || total != 1726791 {
		t.Errorf("alpha = %v", total)
	}
	out, err := Marshal(obj)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != string(raw) {
		t.Errorf("round trip = %s, want %s", out, raw)
	}
}

func mustGet(t *testing.T, o *Object, key string) any {
	t.Helper()
	v, ok := o.Get(key)
	if !ok {
		t.Fatalf("key %q not found", key)
	}
	return v
}

func TestMarshalMatchesPythonStyle(t *testing.T) {
	cases := []struct {
		in   any
		want string
	}{
		{nil, "null"},
		{true, "true"},
		{json.Number("1726791"), "1726791"},
		{"a\"b", `"a\"b"`},
		{"中文", `"中文"`},
		{[]any{json.Number("1"), "x"}, `[1,"x"]`},
	}
	for _, c := range cases {
		got, err := Marshal(c.in)
		if err != nil {
			t.Fatalf("Marshal(%v): %v", c.in, err)
		}
		if string(got) != c.want {
			t.Errorf("Marshal(%v) = %s, want %s", c.in, got, c.want)
		}
	}
}

func TestExtractItemPathAndTotal(t *testing.T) {
	raw := []byte(`{"message":{"total-results":1726791,"items":[{"DOI":"10.1/0"}]}}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	data, total := Extract(v, Spec{ItemPath: "message.items", TotalField: "message.total-results"})
	if total == nil || *total != 1726791 {
		t.Fatalf("total = %v, want 1726791", total)
	}
	list, ok := data.([]any)
	if !ok || len(list) != 1 {
		t.Fatalf("data = %#v", data)
	}
}

func TestExtractTotalFieldWithListIndex(t *testing.T) {
	raw := []byte(`[{"total":29544},{"other":1}]`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	_, total := Extract(v, Spec{TotalField: "0.total"})
	if total == nil || *total != 29544 {
		t.Fatalf("total = %v, want 29544", total)
	}
}

// An upstream key whose name contains a dot (OData's "@odata.count") is
// reachable: the dotted walk falls back to the whole path as one literal key.
func TestExtractTotalFieldWithDottedLiteralKey(t *testing.T) {
	raw := []byte(`{"@odata.context":"ctx","@odata.count":3102,"value":[{"a":1}]}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	_, total := Extract(v, Spec{TotalField: "@odata.count"})
	if total == nil || *total != 3102 {
		t.Fatalf("total = %v, want 3102", total)
	}
}

// The literal fallback must not shadow a real nested path.
func TestExtractTotalFieldPrefersTheNestedPath(t *testing.T) {
	raw := []byte(`{"meta":{"total":7},"meta.total":9}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	_, total := Extract(v, Spec{TotalField: "meta.total"})
	if total == nil || *total != 7 {
		t.Fatalf("total = %v, want 7 (the nested path wins)", total)
	}
}

func TestExtractMapItemShape(t *testing.T) {
	raw := []byte(`{"0":{"name":"a"},"1":{"name":"b"}}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := Extract(v, Spec{ItemShape: "map"})
	list, ok := data.([]any)
	if !ok || len(list) != 2 {
		t.Fatalf("data = %#v", data)
	}
	first, ok := list[0].(*Object)
	if !ok {
		t.Fatalf("list[0] = %T", list[0])
	}
	if v, _ := first.Get("name"); v != "a" {
		t.Errorf("name = %v, want a", v)
	}
}

func TestTrimLimitFieldsAndTruncation(t *testing.T) {
	raw := []byte(`{"message":{"total-results":100,"items":[{"a":1,"b":2,"c":3},{"a":4,"b":5,"c":6}]}}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	data, total := Extract(v, Spec{ItemPath: "message.items", TotalField: "message.total-results"})

	// The limit truncates and flags the result.
	out, total2, truncated := Trim(data, total, 1, nil, false)
	if !truncated {
		t.Error("truncated must be true")
	}
	if total2 == nil || *total2 != 100 {
		t.Errorf("total = %v, want 100", total2)
	}
	if l, ok := out.([]any); !ok || len(l) != 1 {
		t.Errorf("out = %#v", out)
	}

	// A total larger than the list also flags the result.
	out, _, truncated = Trim(data, total, 0, nil, false)
	_ = out
	if !truncated {
		t.Error("truncated must be true when the total is bigger than the list")
	}

	// The field projection keeps only the requested keys, in the order of
	// the source document.
	projected, _, _ := Trim(data, total, 0, []string{"a"}, false)
	list, ok := projected.([]any)
	if !ok || len(list) != 2 {
		t.Fatalf("projected = %#v", projected)
	}
	row, ok := list[0].(*Object)
	if !ok {
		t.Fatalf("row = %T", list[0])
	}
	if row.Len() != 1 {
		t.Errorf("row = %v, want one key", row.Keys())
	}
	if v, _ := row.Get("a"); v != json.Number("1") {
		t.Errorf("a = %v", v)
	}
}

func TestTrimDefaultsTotalToItemCount(t *testing.T) {
	raw := []byte(`{"items":[{"a":1},{"a":2}]}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	data, total := Extract(v, Spec{ItemPath: "items"})
	if total != nil {
		t.Fatalf("total = %v, want nil", total)
	}
	_, total2, truncated := Trim(data, total, 0, nil, false)
	if total2 == nil || *total2 != 2 {
		t.Errorf("total = %v, want 2", total2)
	}
	if truncated {
		t.Error("truncated must be false")
	}
}

func TestGoEngineParsesJSON(t *testing.T) {
	eng := NewGoEngine(100)
	body := []byte(`{"message":{"total-results":7,"items":[{"DOI":"10.1/0","title":"t"}]}}`)
	res, err := eng.Parse(context.Background(), "object", "application/json", body,
		Spec{ItemPath: "message.items", TotalField: "message.total-results", MaxRows: 100})
	if err != nil {
		t.Fatal(err)
	}
	if res.Total == nil || *res.Total != 7 {
		t.Errorf("total = %v, want 7", res.Total)
	}
	if len(res.Items) != 1 {
		t.Fatalf("items = %#v", res.Items)
	}
	out, err := Marshal(res.Data)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != `[{"DOI":"10.1/0","title":"t"}]` {
		t.Errorf("data = %s", out)
	}
}

// The text shape is not supposed to carry JSON: its body is passed through
// as the "_text" marker, exactly like the reference does.
func TestGoEngineFallsBackToText(t *testing.T) {
	eng := NewGoEngine(100)
	res, err := eng.Parse(context.Background(), "text", "text/plain", []byte("not json"), Spec{})
	if err != nil {
		t.Fatal(err)
	}
	obj, ok := res.Data.(*Object)
	if !ok {
		t.Fatalf("data = %T", res.Data)
	}
	if v, _ := obj.Get("_text"); v != "not json" {
		t.Errorf("_text = %v", v)
	}
}

// The XML shape is not supposed to carry JSON either: its body is passed
// through as the "_xml" marker and it is never an error.
func TestGoEngineXMLShapeKeepsTheXMLFallback(t *testing.T) {
	eng := NewGoEngine(100)
	body := []byte("<root><a>1</a></root>")
	res, err := eng.Parse(context.Background(), "xml", "application/xml", body, Spec{})
	if err != nil {
		t.Fatal(err)
	}
	out, err := Marshal(res.Data)
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf(`{"_xml_bytes":%d,"_truncated":false}`, len(body))
	if string(out) != want {
		t.Errorf("data = %s, want %s", out, want)
	}
}

// A JSON shape whose body is not JSON is **not** an empty result: the caller
// has to be able to tell "the source has no data" apart from "the upstream
// answered an HTML error page". The error is detectable and carries the head
// of the body, so that the reason is visible at a glance.
func TestGoEngineReportsNotJSONForAJSONShape(t *testing.T) {
	eng := NewGoEngine(100)
	body := []byte("<!DOCTYPE html>\n<html><head><title>502 Bad Gateway</title></head></html>")
	_, err := eng.Parse(context.Background(), "object", "application/json", body, Spec{})
	if err == nil {
		t.Fatal("a body that is not JSON must be an error, not an empty result")
	}
	if !errors.Is(err, ErrNotJSON) {
		t.Errorf("error = %v, want it to match ErrNotJSON", err)
	}
	msg := err.Error()
	if !strings.Contains(msg, "<!DOCTYPE html>") {
		t.Errorf("error = %q, want the head of the body", msg)
	}
	if strings.Contains(msg, "\n") {
		t.Errorf("error = %q, the excerpt must be a single line", msg)
	}
}

func TestGoEngineParsesCSV(t *testing.T) {
	eng := NewGoEngine(100)
	res, err := eng.Parse(context.Background(), "csv", "text/csv", []byte("a,b\n1,2\n"), Spec{})
	if err != nil {
		t.Fatal(err)
	}
	rows, ok := res.Data.([]any)
	if !ok || len(rows) != 2 {
		t.Fatalf("rows = %#v", res.Data)
	}
	first, ok := rows[0].([]any)
	if !ok || len(first) != 2 || first[0] != "a" {
		t.Errorf("row = %#v", rows[0])
	}
}

func TestGoEngineRespectsMaxRows(t *testing.T) {
	eng := NewGoEngine(2)
	res, err := eng.Parse(context.Background(), "csv", "text/csv", []byte("a\nb\nc\nd\n"), Spec{MaxRows: 2})
	if err != nil {
		t.Fatal(err)
	}
	rows, _ := res.Data.([]any)
	if len(rows) != 2 {
		t.Errorf("rows = %d, want 2", len(rows))
	}
}

// TestGoEngineXMLProjectionKeepsTheKeyOrder checks the key order of the XML
// projection: it is the insertion order of the Python reference
// ({"_xml_bytes": N, "_truncated": bool}), not the sorted order of a Go map.
// The worker reports a body that is not JSON with the error type "NotJSON":
// it becomes the very same error the Go engine produces, so that the
// envelope of a call looks the same whichever engine is active.
func TestWorkerNotJSONIsMappedOntoErrNotJSON(t *testing.T) {
	reply := NewObject()
	reply.Set("id", "gw-1")
	reply.Set("ok", false)
	reply.Set("error", "\u4e0a\u6e38\u54cd\u5e94\u4e0d\u662f JSON: invalid character '<'; \u6b63\u6587\u7247\u6bb5: <!DOCTYPE html><html>")
	reply.Set("error_type", "NotJSON")

	_, err := resultFromWire(reply)
	if err == nil {
		t.Fatal("the worker failure must be an error")
	}
	if !errors.Is(err, ErrNotJSON) {
		t.Errorf("error = %v, want it to match ErrNotJSON", err)
	}
	if !strings.Contains(err.Error(), "<!DOCTYPE html>") {
		t.Errorf("error = %q, want the excerpt of the body", err)
	}
}

// Any other worker failure stays a plain parse error: only the "NotJSON"
// type is a content problem, not a worker problem.
func TestWorkerParseErrorIsNotMappedOntoErrNotJSON(t *testing.T) {
	reply := NewObject()
	reply.Set("id", "gw-1")
	reply.Set("ok", false)
	reply.Set("error", "boom")
	reply.Set("error_type", "ParseError")

	_, err := resultFromWire(reply)
	if err == nil {
		t.Fatal("the worker failure must be an error")
	}
	if errors.Is(err, ErrNotJSON) {
		t.Errorf("error = %v, want a plain error", err)
	}
}

// The excerpt of a body that is not JSON is a single line.
func TestNotJSONSnippetIsSingleLine(t *testing.T) {
	snippet := notJSONSnippet([]byte("<html>\n\t<body>\n502\n</body>\n</html>"))
	if strings.ContainsAny(snippet, "\n\r\t") {
		t.Errorf("snippet = %q, want a single line", snippet)
	}
	if !strings.Contains(snippet, "502") {
		t.Errorf("snippet = %q, want the head of the body", snippet)
	}
}

func TestGoEngineXMLProjectionKeepsTheKeyOrder(t *testing.T) {
	eng := NewGoEngine(100)
	body := []byte("<root><a>1</a></root>")
	res, err := eng.Parse(context.Background(), "xml", "application/xml", body, Spec{})
	if err != nil {
		t.Fatal(err)
	}
	out, err := Marshal(res.Data)
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf(`{"_xml_bytes":%d,"_truncated":false}`, len(body))
	if string(out) != want {
		t.Errorf("data = %s, want %s", out, want)
	}
}

// TestEncodeBodyMatchesTheOneShotEncoding checks that the chunked encoding
// of the body is byte identical to the one shot encoding: the wire protocol
// must not change.
func TestEncodeBodyMatchesTheOneShotEncoding(t *testing.T) {
	for _, size := range []int{0, 1, 2, 3, 100, 96 * 1024, 96*1024 + 1, 200_000} {
		body := make([]byte, size)
		for i := range body {
			body[i] = byte(i % 251)
		}
		got, err := encodeBody(context.Background(), body)
		if err != nil {
			t.Fatal(err)
		}
		want := base64.StdEncoding.EncodeToString(body)
		if got != want {
			t.Errorf("size %d: the chunked encoding differs from the one shot encoding", size)
		}
	}
}

func TestPyReprMatchesPythonFormat(t *testing.T) {
	cases := []struct {
		in   any
		want string
	}{
		{"x", "'x'"},
		{json.Number("25"), "25"},
		{nil, "None"},
		{true, "True"},
		{false, "False"},
		{[]any{"a", json.Number("1")}, "['a', 1]"},
	}
	for _, c := range cases {
		if got := PyRepr(c.in); got != c.want {
			t.Errorf("PyRepr(%v) = %s, want %s", c.in, got, c.want)
		}
	}
}

func TestMarshalIndentMatchesPythonLayout(t *testing.T) {
	o := NewObject()
	o.Set("a", json.Number("1"))
	list := []any{}
	item := NewObject()
	item.Set("b", "c")
	list = append(list, item)
	o.Set("list", list)
	o.Set("empty", NewObject())
	got, err := MarshalIndent(o, "  ")
	if err != nil {
		t.Fatal(err)
	}
	want := "{\n  \"a\": 1,\n  \"list\": [\n    {\n      \"b\": \"c\"\n    }\n  ],\n  \"empty\": {}\n}"
	if string(got) != want {
		t.Errorf("indent =\n%s\nwant\n%s", got, want)
	}
}

// A top level array payload must still be located by item_path: the SZSE list
// endpoint answers [{"metadata":…,"data":[…]}], so "0.data" is the real path.
func TestExtractTopLevelArrayWithNumericItemPath(t *testing.T) {
	raw := []byte(`[{"metadata":{"recordcount":2906},"data":[{"agdm":"000001"},{"agdm":"000002"}],"error":null}]`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	data, total := Extract(v, Spec{ItemPath: "0.data", TotalField: "0.metadata.recordcount"})
	if total == nil || *total != 2906 {
		t.Fatalf("total = %v, want 2906", total)
	}
	list, ok := data.([]any)
	if !ok || len(list) != 2 {
		t.Fatalf("data = %#v", data)
	}
}

// A top level array with a non numeric item_path keeps the whole array:
// that is the long standing behaviour and must not turn into an empty list.
func TestExtractTopLevelArrayWithObjectItemPathKeepsWholePayload(t *testing.T) {
	raw := []byte(`[{"id":"W1"},{"id":"W2"}]`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := Extract(v, Spec{ItemPath: "items"})
	list, ok := data.([]any)
	if !ok || len(list) != 2 {
		t.Fatalf("data = %#v", data)
	}
}

// item_flatten flattens one level of a list of lists (the SSE bulletin index
// nests every record in its own array).
func TestExtractFlattensNestedList(t *testing.T) {
	raw := []byte(`{"pageHelp":{"total":2,"data":[[{"TITLE":"a"}],[{"TITLE":"b"}]]}}`)
	v, err := UnmarshalOrdered(raw)
	if err != nil {
		t.Fatal(err)
	}
	data, total := Extract(v, Spec{ItemPath: "pageHelp.data", TotalField: "pageHelp.total", ItemFlatten: true})
	if total == nil || *total != 2 {
		t.Fatalf("total = %v, want 2", total)
	}
	list, ok := data.([]any)
	if !ok || len(list) != 2 {
		t.Fatalf("data = %#v", data)
	}
	first, ok := list[0].(*Object)
	if !ok {
		t.Fatalf("first item = %#v", list[0])
	}
	if v, _ := first.Get("TITLE"); v != "a" {
		t.Fatalf("TITLE = %v, want a", v)
	}
}
