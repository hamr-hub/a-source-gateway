package metrics

import (
	"strings"
	"testing"
)

func TestRenderMetricNames(t *testing.T) {
	m := New()
	m.Observe(Observation{Source: "crossref", LatencyMS: 10})
	m.Observe(Observation{Source: "crossref", Cached: true, LatencyMS: 20})
	m.Observe(Observation{Source: "sec_edgar", Degraded: true, LatencyMS: 30, ErrorTypes: []string{"timeout"}})
	m.WarmFill()
	m.WarmError("timeout: boom")

	out := m.Render()
	for _, name := range []string{
		"asg_queries_total", "asg_cache_hits_total", "asg_degraded_total", "asg_timeouts_total",
		"asg_errors_total", "asg_warm_fills_total", "asg_warm_errors_total", "asg_latency_avg_ms",
		"asg_latency_max_ms", "asg_cache_hit_ratio", "asg_source_queries", "asg_source_degraded",
		"asg_source_latency_avg_ms", "asg_warm_last_error",
	} {
		if !strings.Contains(out, name) {
			t.Errorf("metric %q is missing:\n%s", name, out)
		}
	}
	if !strings.HasSuffix(out, "\n") {
		t.Error("the exposition must end with a newline")
	}
	for _, line := range []string{
		"# HELP asg_queries_total 总查询数",
		"# TYPE asg_queries_total counter",
		"# TYPE asg_latency_avg_ms gauge",
		"asg_queries_total 3",
		"asg_cache_hits_total 1",
		"asg_degraded_total 1",
		"asg_timeouts_total 1",
		"asg_errors_total 1",
		"asg_warm_fills_total 1",
		"asg_warm_errors_total 1",
		"asg_latency_avg_ms 20.00",
		"asg_latency_max_ms 30",
		"asg_cache_hit_ratio 0.3333",
		`asg_source_queries{source="crossref"} 2`,
		`asg_source_degraded{source="sec_edgar"} 1`,
		`asg_source_latency_avg_ms{source="crossref"} 15.00`,
	} {
		if !strings.Contains(out, line) {
			t.Errorf("line %q is missing:\n%s", line, out)
		}
	}
}

func TestStaleErrorsAreNotCountedAsErrors(t *testing.T) {
	m := New()
	m.Observe(Observation{Source: "s", Degraded: true, Cached: true, LatencyMS: 5, ErrorTypes: []string{"stale"}})
	out := m.Render()
	if !strings.Contains(out, "asg_errors_total 0") {
		t.Errorf("a stale response must not count as an error:\n%s", out)
	}
	if !strings.Contains(out, "asg_degraded_total 1") {
		t.Errorf("a stale response is degraded:\n%s", out)
	}
}

func TestLatencyHistogramBuckets(t *testing.T) {
	m := New()
	// 4 次调用: 80ms, 450ms, 600ms, 1500ms
	for _, ms := range []int{80, 450, 600, 1500} {
		m.Observe(Observation{Source: "s", LatencyMS: ms})
	}
	out := m.Render()
	// 累计桶: <=100 有 1, <=500 有 2, <=750 有 3, <=2000 有 4
	for _, line := range []string{
		`asg_query_latency_ms_bucket{le="100"} 1`,
		`asg_query_latency_ms_bucket{le="500"} 2`,
		`asg_query_latency_ms_bucket{le="750"} 3`,
		`asg_query_latency_ms_bucket{le="2000"} 4`,
		`asg_query_latency_ms_bucket{le="+Inf"} 4`,
		`asg_query_latency_ms_sum 2630`,
		`asg_query_latency_ms_count 4`,
	} {
		if !strings.Contains(out, line) {
			t.Errorf("histogram line %q is missing:\n%s", line, out)
		}
	}
}

func TestPerSourceLinesAreSorted(t *testing.T) {
	m := New()
	m.Observe(Observation{Source: "zeta", LatencyMS: 1})
	m.Observe(Observation{Source: "alpha", LatencyMS: 1})
	out := m.Render()
	alpha := strings.Index(out, `asg_source_queries{source="alpha"}`)
	zeta := strings.Index(out, `asg_source_queries{source="zeta"}`)
	if alpha < 0 || zeta < 0 || alpha > zeta {
		t.Errorf("per source lines are not sorted:\n%s", out)
	}
}
