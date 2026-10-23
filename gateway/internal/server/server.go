// Package server implements the HTTP surface of the gateway.
//
// The routes, the status codes and the response shapes mirror the FastAPI
// implementation of the Python reference
// (python/src/a_source_gateway/api.py).
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/envelope"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/metrics"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
)

// MaxBodyBytes bounds the size of a request body.
const MaxBodyBytes = 4 * 1024 * 1024

// Server serves the gateway routes.
type Server struct {
	cfg      *config.Config
	resolver *Resolver
	metrics  *metrics.Metrics
	logger   *log.Logger
	startTS  time.Time
	mux      *http.ServeMux
}

// New builds the HTTP server.
func New(cfg *config.Config, resolver *Resolver, m *metrics.Metrics, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	s := &Server{
		cfg:      cfg,
		resolver: resolver,
		metrics:  m,
		logger:   logger,
		startTS:  time.Now(),
		mux:      http.NewServeMux(),
	}
	s.routes()
	return s
}

// Handler returns the HTTP handler of the gateway.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("GET /{$}", s.handleRoot)
	s.mux.HandleFunc("GET /healthz", s.handleHealthz)
	s.mux.HandleFunc("GET /livez", s.handleLivez)
	s.mux.HandleFunc("GET /readyz", s.handleReadyz)
	s.mux.HandleFunc("GET /metrics", s.handleMetrics)
	s.mux.HandleFunc("GET /v1/schema", s.handleSchema)
	s.mux.HandleFunc("GET /v1/stats", s.handleStats)
	s.mux.HandleFunc("GET /v1/sources", s.handleListSources)
	s.mux.HandleFunc("GET /v1/sources/{source_id}", s.handleGetSource)
	s.mux.HandleFunc("GET /v1/sources/{source_id}/endpoints/{endpoint_id}", s.handleDescribeEndpoint)
	s.mux.HandleFunc("GET /v1/query", s.handleQueryGET)
	s.mux.HandleFunc("POST /v1/query", s.handleQueryPOST)
	s.mux.HandleFunc("/", s.handleNotFound)
}

// TraceHeader is the response header that carries the trace id.
const TraceHeader = "X-Gateway-Trace-Id"

// traceID returns the trace id of an inbound request.
//
// The value of the X-Gateway-Trace-Id or X-Request-Id header is reused, so
// that the caller can correlate its own logs with the gateway logs and the
// parser worker logs. Without such a header a random id is generated.
func traceID(r *http.Request) string {
	for _, h := range []string{TraceHeader, "X-Request-Id"} {
		if v := strings.TrimSpace(r.Header.Get(h)); v != "" {
			return v
		}
	}
	return envelope.NewTraceID()
}

// ── handlers ─────────────────────────────────────────────────────────

func (s *Server) handleRoot(w http.ResponseWriter, r *http.Request) {
	reg := s.resolver.Registry()
	doc := parser.NewObject()
	doc.Set("service", "A-Class Source Gateway")
	doc.Set("version", "1.0")
	doc.Set("purpose", "不存业务数据;提供 A 类源的元数据索引与 ≤500ms 的实时查询代理")
	doc.Set("budget_ms", 500)
	doc.Set("sources", reg.Count)
	doc.Set("families", reg.Families)
	doc.Set("offline", s.resolver.Offline())
	doc.Set("routes", []any{
		"GET  /v1/schema",
		"GET  /v1/stats",
		"GET  /v1/sources?family=&q=",
		"GET  /v1/sources/{source_id}",
		"GET  /v1/sources/{source_id}/endpoints/{endpoint_id}",
		"POST /v1/query",
		"GET  /v1/query?source=&endpoint=&params=<json>",
	})
	mcp := parser.NewObject()
	mcp.Set("command", "python3 -m a_source_gateway.mcp_server")
	mcp.Set("tools", []any{"list_sources", "get_source_meta", "describe_endpoint",
		"query_source", "get_meta_schema"})
	doc.Set("mcp", mcp)
	writeJSON(w, http.StatusOK, doc)
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	if s.resolver == nil {
		writeDetail(w, r, http.StatusServiceUnavailable, "网关未就绪")
		return
	}
	doc := parser.NewObject()
	doc.Set("ok", true)
	doc.Set("sources", len(s.resolver.Registry().Sources))
	doc.Set("offline", s.resolver.Offline())
	writeJSON(w, http.StatusOK, doc)
}

func (s *Server) handleLivez(w http.ResponseWriter, r *http.Request) {
	doc := parser.NewObject()
	doc.Set("ok", true)
	doc.Set("uptime_s", math.Round(time.Since(s.startTS).Seconds()*10)/10)
	writeJSON(w, http.StatusOK, doc)
}

func (s *Server) handleReadyz(w http.ResponseWriter, r *http.Request) {
	reg := s.resolver.Registry()
	ok := reg.Count > 0
	doc := parser.NewObject()
	doc.Set("ok", ok)
	doc.Set("sources", reg.Count)
	doc.Set("offline", s.resolver.Offline())
	status := http.StatusOK
	if !ok {
		status = http.StatusServiceUnavailable
	}
	writeJSON(w, status, doc)
}

func (s *Server) handleMetrics(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(s.metrics.Render()))
}

func (s *Server) handleSchema(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(schemaDoc))
}

func (s *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	reg := s.resolver.Registry()
	endpoints := 0
	for _, src := range reg.Sources {
		endpoints += len(src.Endpoints)
	}
	doc := parser.NewObject()
	doc.Set("sources", reg.Count)
	doc.Set("families", len(reg.Families))
	doc.Set("endpoints", endpoints)
	doc.Set("cache", s.resolver.Cache().Stats())
	prewarm := parser.NewObject()
	prewarm.Set("done", s.resolver.PrewarmDone())
	states := parser.NewObject()
	for k, v := range s.resolver.Prewarmed() {
		states.Set(k, v)
	}
	prewarm.Set("states", states)
	prewarm.Set("pending", s.resolver.PendingWarms())
	prewarm.Set("filled", s.resolver.WarmFills())
	doc.Set("prewarm", prewarm)
	doc.Set("offline", s.resolver.Offline())
	doc.Set("budget_ms", 500)
	// The active parsing engine is always observable.
	doc.Set("parser", s.resolver.ParserName())
	writeJSON(w, http.StatusOK, doc)
}

func (s *Server) handleListSources(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	family := q.Get("family")
	keyword := q.Get("q")
	limit := 200
	if raw := q.Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil {
			writeValidationError(w, "query", "limit", "int_parsing", fmt.Sprintf("Input should be a valid integer, unable to parse string as an integer: %q", raw), raw)
			return
		}
		if n < 1 || n > 500 {
			writeValidationError(w, "query", "limit", "less_than_equal", "Input should be less than or equal to 500", raw)
			return
		}
		limit = n
	}
	items := s.resolver.ListSources(family, keyword)
	if len(items) > limit {
		items = items[:limit]
	}
	meta := parser.NewObject()
	meta.Set("count", len(items))
	meta.Set("family", family)
	meta.Set("q", keyword)
	meta.Set("latency_ms", 0)
	meta.Set("schema_version", "1.0")
	doc := parser.NewObject()
	doc.Set("meta", meta)
	doc.Set("data", items)
	doc.Set("fields", []any{"id", "name", "family", "org", "country", "summary",
		"link_status", "scale", "endpoint_count"})
	writeJSON(w, http.StatusOK, doc)
}

func (s *Server) handleGetSource(w http.ResponseWriter, r *http.Request) {
	sourceID := r.PathValue("source_id")
	src := s.resolver.GetSource(sourceID)
	if src == nil {
		writeDetail(w, r, http.StatusNotFound, fmt.Sprintf("未知源 %s", sourceID))
		return
	}
	meta := parser.NewObject()
	meta.Set("source", sourceID)
	meta.Set("schema_version", "1.0")
	doc := parser.NewObject()
	doc.Set("meta", meta)
	doc.Set("data", src)
	writeJSON(w, http.StatusOK, doc)
}

func (s *Server) handleDescribeEndpoint(w http.ResponseWriter, r *http.Request) {
	sourceID := r.PathValue("source_id")
	endpointID := r.PathValue("endpoint_id")
	data := s.resolver.Describe(sourceID, endpointID)
	if data == nil {
		writeDetail(w, r, http.StatusNotFound, fmt.Sprintf("未知端点 %s/%s", sourceID, endpointID))
		return
	}
	meta := parser.NewObject()
	meta.Set("source", sourceID)
	meta.Set("endpoint", endpointID)
	meta.Set("schema_version", "1.0")
	doc := parser.NewObject()
	doc.Set("meta", meta)
	doc.Set("data", data)
	writeJSON(w, http.StatusOK, doc)
}

func (s *Server) handleQueryGET(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	source := q.Get("source")
	endpoint := q.Get("endpoint")
	if !q.Has("source") {
		writeValidationError(w, "query", "source", "missing", "Field required", nil)
		return
	}
	if !q.Has("endpoint") {
		writeValidationError(w, "query", "endpoint", "missing", "Field required", nil)
		return
	}
	rawParams := q.Get("params")
	if rawParams == "" {
		rawParams = "{}"
	}
	params, err := parser.UnmarshalOrdered([]byte(rawParams))
	if err != nil {
		writeDetail(w, r, http.StatusBadRequest, "params 必须是合法 JSON")
		return
	}
	paramObj, ok := params.(*parser.Object)
	if !ok {
		writeDetail(w, r, http.StatusBadRequest, "params 必须是合法 JSON")
		return
	}
	fields := []string{}
	for _, f := range strings.Split(q.Get("fields"), ",") {
		if f != "" {
			fields = append(fields, f)
		}
	}
	limit, ok := s.intQuery(w, r, "limit", 20, 1, 500)
	if !ok {
		return
	}
	timeoutMS, ok := s.intQuery(w, r, "timeout_ms", 500, 50, 500)
	if !ok {
		return
	}
	if s.resolver.ResolveSource(source) == "" {
		writeDetail(w, r, http.StatusNotFound, fmt.Sprintf("未知数据源 %s", source))
		return
	}
	req := QueryRequest{
		Source:    source,
		Endpoint:  endpoint,
		Params:    paramObj,
		Fields:    fields,
		Limit:     limit,
		Since:     q.Get("since"),
		TimeoutMS: timeoutMS,
		TraceID:   traceID(r),
	}
	s.runQuery(w, req)
}

func (s *Server) handleQueryPOST(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, MaxBodyBytes))
	if err != nil {
		writeValidationError(w, "body", "", "json_invalid", "Invalid JSON body", nil)
		return
	}
	v, err := parser.UnmarshalOrdered(raw)
	if err != nil {
		writeValidationError(w, "body", "", "json_invalid", "Invalid JSON body", nil)
		return
	}
	body, ok := v.(*parser.Object)
	if !ok {
		writeValidationError(w, "body", "", "model_attributes_type", "Input should be a valid dictionary or object to extract fields from", nil)
		return
	}
	source, ok := bodyString(body, "source")
	if !ok {
		writeValidationError(w, "body", "source", "missing", "Field required", nil)
		return
	}
	endpoint, ok := bodyString(body, "endpoint")
	if !ok {
		writeValidationError(w, "body", "endpoint", "missing", "Field required", nil)
		return
	}
	params := parser.NewObject()
	if raw, ok := body.Get("params"); ok && raw != nil {
		po, ok := raw.(*parser.Object)
		if !ok {
			writeValidationError(w, "body", "params", "dict_type", "Input should be a valid dictionary", raw)
			return
		}
		params = po
	}
	fields := []string{}
	if raw, ok := body.Get("fields"); ok && raw != nil {
		list, ok := raw.([]any)
		if !ok {
			writeValidationError(w, "body", "fields", "list_type", "Input should be a valid list", raw)
			return
		}
		for _, item := range list {
			s, ok := item.(string)
			if !ok {
				writeValidationError(w, "body", "fields", "string_type", "Input should be a valid string", item)
				return
			}
			fields = append(fields, s)
		}
	}
	limit := 20
	if raw, ok := body.Get("limit"); ok && raw != nil {
		n, ok := parser.ToInt(raw)
		if !ok {
			writeValidationError(w, "body", "limit", "int_type", "Input should be a valid integer", raw)
			return
		}
		limit = n
	}
	if limit < 1 || limit > 500 {
		writeValidationError(w, "body", "limit", "less_than_equal", "Input should be less than or equal to 500", limit)
		return
	}
	timeoutMS := 500
	if raw, ok := body.Get("timeout_ms"); ok && raw != nil {
		n, ok := parser.ToInt(raw)
		if !ok {
			writeValidationError(w, "body", "timeout_ms", "int_type", "Input should be a valid integer", raw)
			return
		}
		timeoutMS = n
	}
	if timeoutMS < 50 || timeoutMS > 500 {
		writeValidationError(w, "body", "timeout_ms", "less_than_equal", "Input should be less than or equal to 500", timeoutMS)
		return
	}
	since := ""
	if raw, ok := body.Get("since"); ok && raw != nil {
		since, _ = raw.(string)
	}
	if s.resolver.ResolveSource(source) == "" {
		writeDetail(w, r, http.StatusNotFound, fmt.Sprintf("未知数据源 %s", source))
		return
	}
	req := QueryRequest{
		Source:    source,
		Endpoint:  endpoint,
		Params:    params,
		Fields:    fields,
		Limit:     limit,
		Since:     since,
		TimeoutMS: timeoutMS,
		TraceID:   traceID(r),
	}
	s.runQuery(w, req)
}

// runQuery executes a query and writes the envelope.
func (s *Server) runQuery(w http.ResponseWriter, req QueryRequest) {
	t0 := time.Now()
	env := s.resolver.Query(req, nil)
	s.writeEnvelope(w, env, t0, req.TraceID)
}

// writeEnvelope writes the envelope and applies the client visible budget
// bookkeeping of the reference implementation.
//
// The reported latency is the client visible wall clock; it is never zero,
// also on the early error paths.
func (s *Server) writeEnvelope(w http.ResponseWriter, env *envelope.Envelope, t0 time.Time, traceID string) {
	elapsed := time.Since(t0)
	totalMS := int(math.Round(float64(elapsed.Microseconds()) / 1000))
	if totalMS < 1 && elapsed > 0 {
		totalMS = 1
	}
	env.Meta.LatencyMS = totalMS
	if totalMS > env.Meta.BudgetMS {
		env.Meta.AddError("budget_exceeded", fmt.Sprintf("客户端可见耗时 %dms 超过预算 %dms", totalMS, env.Meta.BudgetMS))
		env.Meta.Degraded = true
		if list, ok := env.Data.([]any); ok && len(list) > 5 {
			env.Data = list[:5]
			env.Meta.Truncated = true
		}
	}
	if env.Meta.TraceID == "" {
		env.Meta.TraceID = envelope.NewTraceID()
	}
	if env.Meta.Parser == "" {
		env.Meta.Parser = s.resolver.ParserName()
	}
	body, err := envelope.Marshal(env)
	if err != nil {
		s.logger.Printf("error: cannot encode envelope: %v", err)
		doc := parser.NewObject()
		doc.Set("detail", "网关内部错误")
		body, _ := json.Marshal(doc)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set(TraceHeader, traceID)
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write(body)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Gateway-Latency-Ms", strconv.Itoa(totalMS))
	w.Header().Set(TraceHeader, env.Meta.TraceID)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

func (s *Server) intQuery(w http.ResponseWriter, r *http.Request, name string, def, min, max int) (int, bool) {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return def, true
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		writeValidationError(w, "query", name, "int_parsing",
			fmt.Sprintf("Input should be a valid integer, unable to parse string as an integer: %q", raw), raw)
		return 0, false
	}
	if n < min || n > max {
		writeValidationError(w, "query", name, "less_than_equal",
			fmt.Sprintf("Input should be less than or equal to %d", max), n)
		return 0, false
	}
	return n, true
}

func (s *Server) handleNotFound(w http.ResponseWriter, r *http.Request) {
	writeDetail(w, r, http.StatusNotFound, "Not Found")
}

// ── helpers ──────────────────────────────────────────────────────────

func bodyString(o *parser.Object, key string) (string, bool) {
	v, ok := o.Get(key)
	if !ok || v == nil {
		return "", false
	}
	s, ok := v.(string)
	return s, ok
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	body, err := envelope.Marshal(v)
	if err != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"detail":"网关内部错误"}`))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// writeDetail renders a FastAPI style error body and echoes the trace id,
// so that a client error can be correlated with the gateway logs as well.
func writeDetail(w http.ResponseWriter, r *http.Request, status int, detail string) {
	doc := parser.NewObject()
	doc.Set("detail", detail)
	body, _ := json.Marshal(doc)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set(TraceHeader, traceID(r))
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// writeValidationError renders a FastAPI style validation error.
func writeValidationError(w http.ResponseWriter, loc string, field, typ, msg string, input any) {
	item := parser.NewObject()
	item.Set("type", typ)
	locList := []any{}
	if loc != "" {
		locList = append(locList, loc)
	}
	if field != "" {
		locList = append(locList, field)
	}
	item.Set("loc", locList)
	item.Set("msg", msg)
	item.Set("input", input)
	doc := parser.NewObject()
	doc.Set("detail", []any{item})
	body, _ := envelope.Marshal(doc)
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnprocessableEntity)
	_, _ = w.Write(body)
}

// RecoverMiddleware turns a panic of a handler into a degraded envelope.
func RecoverMiddleware(logger *log.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				logger.Printf("error: panic in %s %s: %v", r.Method, r.URL.Path, rec)
				meta := envelope.NewMeta()
				meta.BudgetMS = 500
				meta.Degraded = true
				meta.TraceID = traceID(r)
				meta.Parser = "go"
				meta.AddError("internal_error", fmt.Sprintf("网关内部错误: %v", rec))
				env := &envelope.Envelope{Meta: meta, Data: []any{}, Fields: []string{}}
				body, _ := envelope.Marshal(env)
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set(TraceHeader, meta.TraceID)
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write(body)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// Shutdown releases the resources of the server.
func (s *Server) Shutdown(ctx context.Context) error {
	return nil
}
