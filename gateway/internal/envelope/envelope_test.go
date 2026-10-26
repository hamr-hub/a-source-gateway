package envelope

import (
	"testing"
)

func TestEnvelopeJSONShape(t *testing.T) {
	meta := NewMeta()
	meta.Source = "crossref"
	meta.Endpoint = "works"
	meta.Count = 2
	meta.Total = intPtr(10)
	env := &Envelope{Meta: meta, Data: []any{}, Fields: []string{}}
	body, err := Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"meta":{"request_id":"","trace_id":"","schema_version":"1.1","source":"crossref","endpoint":"works",` +
		`"parser":"","upstream":{"url":"","method":"GET","status":null,"latency_ms":0},"latency_ms":0,` +
		`"budget_ms":0,"cached":false,"cache_age_s":0,"degraded":false,"offline":false,"count":2,` +
		`"total":10,"truncated":false,"next_cursor":null,"rate_limit":{},"fallback":{},"errors":[]},` +
		`"data":[],"fields":[]}`
	if string(body) != want {
		t.Errorf("envelope =\n%s\nwant\n%s", body, want)
	}
}

func TestErrorShape(t *testing.T) {
	meta := NewMeta()
	meta.AddError("timeout", "上游超时")
	meta.AddErrorStatus("upstream_error", 502, "bad gateway")
	body, err := Marshal(meta.Errors)
	if err != nil {
		t.Fatal(err)
	}
	want := `[{"type":"timeout","message":"上游超时"},{"type":"upstream_error","status":502,"message":"bad gateway"}]`
	if string(body) != want {
		t.Errorf("errors = %s, want %s", body, want)
	}
}

func TestFallbackShape(t *testing.T) {
	meta := NewMeta()
	meta.SetFallback("https://example.org/x", "GET", "直连上游")
	body, err := Marshal(meta.Fallback)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"upstream_url":"https://example.org/x","method":"GET","hint":"直连上游"}`
	if string(body) != want {
		t.Errorf("fallback = %s, want %s", body, want)
	}
}

func intPtr(n int) *int { return &n }

func TestNewTraceIDIs32HexChars(t *testing.T) {
	id := NewTraceID()
	if len(id) != 32 {
		t.Errorf("trace id = %q, want 32 characters", id)
	}
	for _, r := range id {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')) {
			t.Errorf("trace id = %q, want lower case hex", id)
			break
		}
	}
	if NewTraceID() == NewTraceID() {
		t.Error("two trace ids must differ")
	}
}
