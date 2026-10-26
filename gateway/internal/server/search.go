package server

import (
	"fmt"
	"io"
	"math"
	"net/http"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/registry"
)

// defaultSearchFanout bounds how many sources a keyword search fans out to.
// A broad keyword matches dozens of sources; querying all of them would turn
// one request into a burst of upstream traffic, which is exactly what the
// per source rate limits are there to avoid.
const defaultSearchFanout = 12

// maxSearchFanout is the hard ceiling a caller can ask for.
const maxSearchFanout = 50

// maxSearchLimit is the hard ceiling of the per-query limit of a search.
const maxSearchLimit = 100

// handleSearchPOST runs one query against many sources in a single request.
//
// Request body, two forms ("queries" wins when both are present):
//
//	{"queries": [{"source","endpoint","params","limit","fields"}], "limit": N}
//	{"q": "keyword", "limit": N, "max_sources": M}
//
// The keyword form fans out over the sources that "q" selects — the same
// search index GET /v1/sources?q= uses — and calls each source's first
// non-generic endpoint with its default parameters.
//
// Every source gets its own budget and they run concurrently, so the wall
// clock is bounded by the slowest source rather than the sum of all of them.
// A source that cannot be resolved still gets an entry in "results": the
// caller can tell "no hit" from "broken" without failing the whole request.
//
// The parameter order of every entry is preserved exactly as the caller wrote
// it. The cache key contains the rendered URL, so reordering the keys would
// make /v1/search miss the entry /v1/query already cached.
func (s *Server) handleSearchPOST(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, MaxBodyBytes))
	if err != nil {
		writeValidationErrorAt(w, []any{"body"}, "json_invalid", "Invalid JSON body", nil)
		return
	}
	v, err := parser.UnmarshalOrdered(raw)
	if err != nil {
		writeValidationErrorAt(w, []any{"body"}, "json_invalid", "Invalid JSON body", nil)
		return
	}
	body, ok := v.(*parser.Object)
	if !ok {
		writeValidationErrorAt(w, []any{"body"}, "model_attributes_type",
			"Input should be a valid dictionary or object to extract fields from", nil)
		return
	}

	limit, verr := searchInt(body, "limit", 5)
	if verr != nil {
		writeSearchValidationError(w, verr)
		return
	}
	if limit < 1 {
		limit = 1
	}
	if limit > maxSearchLimit {
		limit = maxSearchLimit
	}
	maxSrc, verr := searchInt(body, "max_sources", defaultSearchFanout)
	if verr != nil {
		writeSearchValidationError(w, verr)
		return
	}
	if maxSrc < 1 {
		maxSrc = 1
	}
	if maxSrc > maxSearchFanout {
		maxSrc = maxSearchFanout
	}

	reqs, names, verr := s.searchRequests(body, limit, maxSrc)
	if verr != nil {
		writeSearchValidationError(w, verr)
		return
	}

	t0 := time.Now()
	envs := s.resolver.Search(reqs, nil)

	results := make([]any, 0, len(envs))
	okCount, degCount, failCount, hitCount := 0, 0, 0, 0
	for _, env := range envs {
		m := env.Meta
		item := parser.NewObject()
		item.Set("source", m.Source)
		item.Set("endpoint", m.Endpoint)
		item.Set("degraded", m.Degraded)
		item.Set("cached", m.Cached)
		item.Set("latency_ms", m.LatencyMS)
		item.Set("count", m.Count)
		item.Set("total", m.Total)
		item.Set("data", env.Data)
		item.Set("fields", env.Fields)
		if m.Fallback != nil && m.Fallback.Len() > 0 {
			// 空对象不算"有直连指引": envelope.NewMeta() 把 Fallback 初始化成
			// 空对象, 直接判非 nil 会让每条结果都带一个空的 fallback:{}。
			item.Set("fallback", m.Fallback)
		}
		errs := make([]any, 0, len(m.Errors))
		for _, e := range m.Errors {
			eo := parser.NewObject()
			eo.Set("type", e.Type)
			eo.Set("message", e.Message)
			errs = append(errs, eo)
		}
		item.Set("errors", errs)
		results = append(results, item)
		switch {
		case m.Degraded:
			degCount++
		case len(m.Errors) > 0:
			// 没降级但有错误: 未知源 / 未知端点这类硬失败。
			failCount++
		default:
			okCount++
		}
		if m.Count > 0 {
			hitCount++
		}
	}

	elapsed := time.Since(t0)
	totalMS := int(math.Round(float64(elapsed.Microseconds()) / 1000))
	if totalMS < 1 && elapsed > 0 {
		totalMS = 1
	}
	doc := parser.NewObject()
	meta := parser.NewObject()
	// requested = ok + degraded + failed: 三类互斥, 调用方一眼能看出
	// "多少条真拿到了 / 多少条降级了 / 多少条直接失败了"。
	meta.Set("requested", len(reqs))
	meta.Set("ok", okCount)
	meta.Set("degraded", degCount)
	meta.Set("failed", failCount)
	meta.Set("with_data", hitCount)
	meta.Set("budget_ms", s.cfg.BudgetMS)
	meta.Set("latency_ms", totalMS)
	meta.Set("trace_id", traceID(r))
	meta.Set("schema_version", registry.SchemaVersion)
	doc.Set("meta", meta)
	doc.Set("queries", names)
	doc.Set("results", results)
	writeJSON(w, http.StatusOK, doc)
}

// searchRequest is one query inside a POST /v1/search body.
type searchRequest struct {
	source   string
	endpoint string
	params   *parser.Object
	limit    int
	fields   []string
}

// searchValidationError describes a rejected request body.
//
// The shape mirrors the FastAPI validation error of the Python reference
// plane (type / loc / msg / input), so a client sees the same body from
// either plane.
type searchValidationError struct {
	loc   []any
	typ   string
	msg   string
	input any
}

// writeSearchValidationError writes a validation error in the FastAPI shape.
func writeSearchValidationError(w http.ResponseWriter, e *searchValidationError) {
	writeValidationErrorAt(w, e.loc, e.typ, e.msg, e.input)
}

// searchInt reads an optional integer field of the search body.
func searchInt(body *parser.Object, key string, def int) (int, *searchValidationError) {
	raw, ok := body.Get(key)
	if !ok || raw == nil {
		return def, nil
	}
	n, ok := parser.ToInt(raw)
	if !ok {
		return 0, &searchValidationError{
			loc:   []any{"body", key},
			typ:   "int_type",
			msg:   "Input should be a valid integer",
			input: raw,
		}
	}
	return n, nil
}

// searchRequests builds the query list of a search request.
//
// Two forms are accepted and "queries" wins when both are present:
// an explicit list of queries, or a keyword that selects the sources.
func (s *Server) searchRequests(body *parser.Object, limit, maxSrc int) ([]QueryRequest, []string, *searchValidationError) {
	entries, verr := parseSearchEntries(body)
	if verr != nil {
		return nil, nil, verr
	}

	reqs := make([]QueryRequest, 0, len(entries))
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		ep := e.endpoint
		if ep == "" {
			ep = registry.GenericEndpointID
		}
		l := e.limit
		if l <= 0 {
			l = limit
		}
		if l > maxSearchLimit {
			l = maxSearchLimit
		}
		reqs = append(reqs, QueryRequest{
			Source:   e.source,
			Endpoint: ep,
			Params:   e.params,
			Fields:   e.fields,
			Limit:    l,
		})
		names = append(names, e.source+"/"+ep)
	}
	if len(reqs) > 0 {
		return reqs, names, nil
	}

	// No explicit query list: fan out over the sources the keyword selects.
	q, ok := body.Get("q")
	if !ok || q == nil {
		return nil, nil, &searchValidationError{
			loc: []any{"body", "queries"},
			typ: "missing",
			msg: "Field required: provide \"queries\" or \"q\"",
		}
	}
	keyword, ok := q.(string)
	if !ok {
		return nil, nil, &searchValidationError{
			loc:   []any{"body", "q"},
			typ:   "string_type",
			msg:   "Input should be a valid string",
			input: q,
		}
	}
	if keyword == "" {
		return nil, nil, &searchValidationError{
			loc: []any{"body", "q"},
			typ: "missing",
			msg: "Field required: provide \"queries\" or \"q\"",
		}
	}

	// 先把"没有真实端点"的源滤掉, 再按 max_sources 截断 —— 否则一个只有通用
	// 端点(entry)的源会白占一个名额, 让调用方少查一个真正可用的源。
	listed := s.resolver.ListSources("", keyword)
	for _, entry := range listed {
		if len(reqs) >= maxSrc {
			break
		}
		eo, ok := entry.(*parser.Object)
		if !ok {
			continue
		}
		rawID, ok := eo.Get("id")
		if !ok {
			continue
		}
		id, _ := rawID.(string)
		if id == "" {
			continue
		}
		src := s.resolver.Registry().Source(id)
		if src == nil {
			continue
		}
		ep := firstRealEndpoint(src)
		if ep == nil {
			continue
		}
		reqs = append(reqs, QueryRequest{
			Source:   id,
			Endpoint: ep.ID,
			Params:   prewarmParams(ep, s.cfg.Contact),
			Fields:   []string{},
			Limit:    limit,
		})
		names = append(names, id+"/"+ep.ID)
	}
	return reqs, names, nil
}

// parseSearchEntries reads the "queries" array of a search body.
//
// The parameters of every entry keep the order the caller wrote them in:
// the rendered URL goes into the cache key, so a reordered request would
// miss the cache entry the same query already filled.
func parseSearchEntries(body *parser.Object) ([]searchRequest, *searchValidationError) {
	raw, ok := body.Get("queries")
	if !ok || raw == nil {
		return nil, nil
	}
	list, ok := raw.([]any)
	if !ok {
		return nil, &searchValidationError{
			loc:   []any{"body", "queries"},
			typ:   "list_type",
			msg:   "Input should be a valid list",
			input: raw,
		}
	}
	out := make([]searchRequest, 0, len(list))
	for i, item := range list {
		qo, ok := item.(*parser.Object)
		if !ok {
			return nil, &searchValidationError{
				loc:   []any{"body", fmt.Sprintf("queries.%d", i)},
				typ:   "dict_type",
				msg:   "Input should be a valid dictionary",
				input: item,
			}
		}
		entry := searchRequest{params: parser.NewObject(), fields: []string{}}
		rawSource, hasSource := qo.Get("source")
		if !hasSource || rawSource == nil {
			return nil, &searchValidationError{
				loc: []any{"body", fmt.Sprintf("queries.%d.source", i)},
				typ: "missing",
				msg: "Field required",
			}
		}
		source, ok := rawSource.(string)
		if !ok {
			return nil, &searchValidationError{
				loc:   []any{"body", fmt.Sprintf("queries.%d.source", i)},
				typ:   "string_type",
				msg:   "Input should be a valid string",
				input: rawSource,
			}
		}
		if source == "" {
			// 空串等价于没给: 一个空的源 id 拿不到任何数据, 与其让它在结果里
			// 变成一条 not_found, 不如在入口就报"缺参数"。
			return nil, &searchValidationError{
				loc: []any{"body", fmt.Sprintf("queries.%d.source", i)},
				typ: "missing",
				msg: "Field required",
			}
		}
		entry.source = source
		if rawEp, ok := qo.Get("endpoint"); ok && rawEp != nil {
			ep, ok := rawEp.(string)
			if !ok {
				return nil, &searchValidationError{
					loc:   []any{"body", fmt.Sprintf("queries.%d.endpoint", i)},
					typ:   "string_type",
					msg:   "Input should be a valid string",
					input: rawEp,
				}
			}
			entry.endpoint = ep
		}
		if rawParams, ok := qo.Get("params"); ok && rawParams != nil {
			po, ok := rawParams.(*parser.Object)
			if !ok {
				return nil, &searchValidationError{
					loc:   []any{"body", fmt.Sprintf("queries.%d.params", i)},
					typ:   "dict_type",
					msg:   "Input should be a valid dictionary",
					input: rawParams,
				}
			}
			entry.params = po
		}
		if rawFields, ok := qo.Get("fields"); ok && rawFields != nil {
			fl, ok := rawFields.([]any)
			if !ok {
				return nil, &searchValidationError{
					loc:   []any{"body", fmt.Sprintf("queries.%d.fields", i)},
					typ:   "list_type",
					msg:   "Input should be a valid list",
					input: rawFields,
				}
			}
			for j, f := range fl {
				s, ok := f.(string)
				if !ok {
					return nil, &searchValidationError{
						loc:   []any{"body", fmt.Sprintf("queries.%d.fields.%d", i, j)},
						typ:   "string_type",
						msg:   "Input should be a valid string",
						input: f,
					}
				}
				entry.fields = append(entry.fields, s)
			}
		}
		if rawLimit, ok := qo.Get("limit"); ok && rawLimit != nil {
			n, ok := parser.ToInt(rawLimit)
			if !ok {
				return nil, &searchValidationError{
					loc:   []any{"body", fmt.Sprintf("queries.%d.limit", i)},
					typ:   "int_type",
					msg:   "Input should be a valid integer",
					input: rawLimit,
				}
			}
			entry.limit = n
		}
		out = append(out, entry)
	}
	return out, nil
}

// firstRealEndpoint returns the first endpoint of a source that is not the
// generic fallback one.
func firstRealEndpoint(src *registry.SourceSpec) *registry.EndpointSpec {
	for _, e := range src.Endpoints {
		if e.ID != registry.GenericEndpointID {
			return e
		}
	}
	return nil
}
