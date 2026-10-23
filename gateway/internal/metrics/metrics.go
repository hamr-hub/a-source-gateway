// Package metrics implements the zero dependency Prometheus exposition of
// the gateway.
//
// The metric names, the HELP/TYPE lines and the formatting are identical to
// the Python reference (python/src/a_source_gateway/api.py), so an existing
// dashboard keeps working after the migration to Go.
package metrics

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// SourceStat holds the per source counters.
type SourceStat struct {
	Queries    int
	Degraded   int
	LatencySum float64
}

// Metrics is the process wide counter set.
type Metrics struct {
	mu sync.Mutex

	queries     int64
	cacheHits   int64
	degraded    int64
	timeouts    int64
	errors      int64
	warmFills   int64
	warmErrors  int64
	latencySum  float64
	latencyMax  float64
	warmLastErr string

	bySource map[string]*SourceStat
}

// New returns an empty metric set.
func New() *Metrics {
	return &Metrics{bySource: map[string]*SourceStat{}}
}

// Observation is one counted request.
type Observation struct {
	Source    string
	Degraded  bool
	Cached    bool
	LatencyMS int
	// ErrorTypes are the types of the errors of the response; "stale" is
	// not counted as an error.
	ErrorTypes []string
}

// Observe records one request.
func (m *Metrics) Observe(o Observation) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.queries++
	if o.Cached {
		m.cacheHits++
	}
	if o.Degraded {
		m.degraded++
	}
	for _, t := range o.ErrorTypes {
		if t == "timeout" {
			m.timeouts++
		}
		if t != "stale" {
			m.errors++
		}
	}
	m.latencySum += float64(o.LatencyMS)
	if float64(o.LatencyMS) > m.latencyMax {
		m.latencyMax = float64(o.LatencyMS)
	}
	st := m.bySource[o.Source]
	if st == nil {
		st = &SourceStat{}
		m.bySource[o.Source] = st
	}
	st.Queries++
	st.LatencySum += float64(o.LatencyMS)
	if o.Degraded {
		st.Degraded++
	}
}

// WarmFill records a successful background refresh.
func (m *Metrics) WarmFill() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.warmFills++
}

// WarmError records a failed background refresh. The reason stays visible
// in the last error so that a permanently failing warm-up can be diagnosed.
func (m *Metrics) WarmError(reason string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.warmErrors++
	if len(reason) > 200 {
		reason = reason[:200]
	}
	m.warmLastErr = reason
}

// WarmFills returns the number of successful background refreshes.
func (m *Metrics) WarmFills() int64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.warmFills
}

// WarmErrors returns the number of failed background refreshes.
func (m *Metrics) WarmErrors() int64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.warmErrors
}

// WarmLastError returns the reason of the last failed background refresh.
func (m *Metrics) WarmLastError() string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.warmLastErr
}

// Render returns the Prometheus text exposition of the current counters.
func (m *Metrics) Render() string {
	m.mu.Lock()
	defer m.mu.Unlock()

	q := m.queries
	if q < 1 {
		q = 1
	}
	var b strings.Builder
	lines := []string{
		"# HELP asg_queries_total 总查询数",
		"# TYPE asg_queries_total counter",
		fmt.Sprintf("asg_queries_total %d", m.queries),
		"# HELP asg_cache_hits_total 缓存命中数",
		"# TYPE asg_cache_hits_total counter",
		fmt.Sprintf("asg_cache_hits_total %d", m.cacheHits),
		"# HELP asg_degraded_total 降级返回数",
		"# TYPE asg_degraded_total counter",
		fmt.Sprintf("asg_degraded_total %d", m.degraded),
		"# HELP asg_timeouts_total 上游超时数",
		"# TYPE asg_timeouts_total counter",
		fmt.Sprintf("asg_timeouts_total %d", m.timeouts),
		"# HELP asg_errors_total 错误数",
		"# TYPE asg_errors_total counter",
		fmt.Sprintf("asg_errors_total %d", m.errors),
		"# HELP asg_warm_fills_total 后台补数成功数",
		"# TYPE asg_warm_fills_total counter",
		fmt.Sprintf("asg_warm_fills_total %d", m.warmFills),
		"# HELP asg_warm_errors_total 后台补数失败数",
		"# TYPE asg_warm_errors_total counter",
		fmt.Sprintf("asg_warm_errors_total %d", m.warmErrors),
		"# HELP asg_latency_avg_ms 平均耗时",
		"# TYPE asg_latency_avg_ms gauge",
		fmt.Sprintf("asg_latency_avg_ms %.2f", m.latencySum/float64(q)),
		"# HELP asg_latency_max_ms 最大耗时",
		"# TYPE asg_latency_max_ms gauge",
		fmt.Sprintf("asg_latency_max_ms %.0f", m.latencyMax),
		"# HELP asg_cache_hit_ratio 缓存命中率",
		"# TYPE asg_cache_hit_ratio gauge",
		fmt.Sprintf("asg_cache_hit_ratio %.4f", float64(m.cacheHits)/float64(q)),
	}
	for _, l := range lines {
		b.WriteString(l)
		b.WriteByte('\n')
	}

	keys := make([]string, 0, len(m.bySource))
	for k := range m.bySource {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, src := range keys {
		v := m.bySource[src]
		avg := v.LatencySum / float64(maxInt(1, v.Queries))
		b.WriteString(fmt.Sprintf("asg_source_queries{source=\"%s\"} %d\n", src, v.Queries))
		b.WriteString(fmt.Sprintf("asg_source_degraded{source=\"%s\"} %d\n", src, v.Degraded))
		b.WriteString(fmt.Sprintf("asg_source_latency_avg_ms{source=\"%s\"} %.2f\n", src, avg))
	}

	// Extra metric (not part of the Python exposition): the reason of the
	// last failed background refresh has to stay observable.
	b.WriteString("# HELP asg_warm_last_error 最近一次后台补数失败原因\n")
	b.WriteString("# TYPE asg_warm_last_error gauge\n")
	b.WriteString(fmt.Sprintf("asg_warm_last_error %s\n", strconv.Quote(m.warmLastErr)))
	return b.String()
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
