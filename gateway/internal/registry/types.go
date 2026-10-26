// Package registry builds the source registry at runtime from the two
// declarative data files (data/source_inventory.csv and data/endpoints.json).
//
// The derived rules (slugify, classify_auth, parse_total, parse_unit, the
// proxy policy and the generic fallback endpoint) are implemented so that a
// Go-built registry equals the Python-built one
// (python/src/a_source_gateway/registry.py).
package registry

import (
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
)

// ParamSpec describes one request parameter of an endpoint.
type ParamSpec struct {
	Name     string
	In       string
	Type     string
	Required bool
	Desc     string
	Pattern  *string
	Default  any
	Example  any
	Env      string
}

// ToObject renders the parameter as an ordered object, omitting only the
// keys that are unset (mirrors ParamSpec.to_dict of the Python reference).
//
// The "in" key is rendered last: the reference pops the internal "in_"
// field and re-inserts it under the name "in", which moves it to the end of
// the key order.
func (p ParamSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("name", p.Name)
	o.Set("type", p.Type)
	o.Set("required", p.Required)
	o.Set("desc", p.Desc)
	if p.Pattern != nil {
		o.Set("pattern", *p.Pattern)
	}
	if p.Default != nil {
		o.Set("default", p.Default)
	}
	if p.Example != nil {
		o.Set("example", p.Example)
	}
	if p.Env != "" {
		o.Set("env", p.Env)
	}
	o.Set("in", p.In)
	return o
}

// MarshalJSON renders the parameter.
func (p ParamSpec) MarshalJSON() ([]byte, error) {
	return p.ToObject().MarshalJSON()
}

// FieldSpec describes one field of an upstream response.
type FieldSpec struct {
	Name string
	Type string
	Desc string
	Path *string
}

// ToObject renders the field spec as an ordered object.
func (f FieldSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("name", f.Name)
	o.Set("type", f.Type)
	o.Set("desc", f.Desc)
	if f.Path != nil {
		o.Set("path", *f.Path)
	}
	return o
}

// MarshalJSON renders the field spec.
func (f FieldSpec) MarshalJSON() ([]byte, error) {
	return f.ToObject().MarshalJSON()
}

// AuthSpec describes how a source authenticates.
type AuthSpec struct {
	Type     string
	Header   *string
	Env      *string
	Optional bool
	Note     string
}

// ToObject renders the auth spec as an ordered object.
func (a AuthSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("type", a.Type)
	if a.Header != nil {
		o.Set("header", *a.Header)
	}
	if a.Env != nil {
		o.Set("env", *a.Env)
	}
	o.Set("optional", a.Optional)
	o.Set("note", a.Note)
	return o
}

// MarshalJSON renders the auth spec.
func (a AuthSpec) MarshalJSON() ([]byte, error) {
	return a.ToObject().MarshalJSON()
}

// LimitsSpec carries the official rate limit statement of a source.
type LimitsSpec struct {
	RatePerSec   *float64
	Burst        *int
	DailyCredits *int
	PolicyURL    *string
	Note         string
}

// ToObject renders the limits spec as an ordered object.
func (l LimitsSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	if l.RatePerSec != nil {
		o.Set("rate_per_sec", *l.RatePerSec)
	}
	if l.Burst != nil {
		o.Set("burst", *l.Burst)
	}
	if l.DailyCredits != nil {
		o.Set("daily_credits", *l.DailyCredits)
	}
	if l.PolicyURL != nil {
		o.Set("policy_url", *l.PolicyURL)
	}
	o.Set("note", l.Note)
	return o
}

// MarshalJSON renders the limits spec.
func (l LimitsSpec) MarshalJSON() ([]byte, error) {
	return l.ToObject().MarshalJSON()
}

// ScaleSpec carries the measured size of a source.
type ScaleSpec struct {
	Total      *int
	Unit       string
	MeasuredAt string
	Basis      string
}

// ToObject renders the scale spec as an ordered object.
func (s ScaleSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	if s.Total != nil {
		o.Set("total", *s.Total)
	}
	o.Set("unit", s.Unit)
	o.Set("measured_at", s.MeasuredAt)
	o.Set("basis", s.Basis)
	return o
}

// MarshalJSON renders the scale spec.
func (s ScaleSpec) MarshalJSON() ([]byte, error) {
	return s.ToObject().MarshalJSON()
}

// IncrementSpec carries the incremental update statement of a source.
type IncrementSpec struct {
	Frequency string
	Daily     string
	Window    string
	Strategy  string
}

// ToObject renders the increment spec as an ordered object.
func (i IncrementSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("frequency", i.Frequency)
	o.Set("daily", i.Daily)
	o.Set("window", i.Window)
	o.Set("strategy", i.Strategy)
	return o
}

// MarshalJSON renders the increment spec.
func (i IncrementSpec) MarshalJSON() ([]byte, error) {
	return i.ToObject().MarshalJSON()
}

// CrawlSpec describes how a source should be crawled.
type CrawlSpec struct {
	Mode       string
	BatchFiles []string
	Notes      string
}

// ToObject renders the crawl spec, omitting empty collections and notes.
func (c CrawlSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("mode", c.Mode)
	if len(c.BatchFiles) > 0 {
		o.Set("batch_files", c.BatchFiles)
	}
	if c.Notes != "" {
		o.Set("notes", c.Notes)
	}
	return o
}

// MarshalJSON renders the crawl spec.
func (c CrawlSpec) MarshalJSON() ([]byte, error) {
	return c.ToObject().MarshalJSON()
}

// ResponseSpec describes the shape of an upstream response.
type ResponseSpec struct {
	MediaType   string
	Shape       string
	Fields      []FieldSpec
	ItemPath    string
	ItemShape   string
	ItemFlatten bool
	TotalField  string
	Notes       string
}

// ToObject renders the response spec as an ordered object.
func (r ResponseSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("media_type", r.MediaType)
	o.Set("shape", r.Shape)
	fields := make([]any, 0, len(r.Fields))
	for _, f := range r.Fields {
		fields = append(fields, f.ToObject())
	}
	o.Set("fields", fields)
	if r.ItemPath != "" {
		o.Set("item_path", r.ItemPath)
	}
	if r.ItemShape != "list" {
		o.Set("item_shape", r.ItemShape)
	}
	if r.ItemFlatten {
		o.Set("item_flatten", true)
	}
	if r.TotalField != "" {
		o.Set("total_field", r.TotalField)
	}
	if r.Notes != "" {
		o.Set("notes", r.Notes)
	}
	return o
}

// MarshalJSON renders the response spec.
func (r ResponseSpec) MarshalJSON() ([]byte, error) {
	return r.ToObject().MarshalJSON()
}

// EndpointSpec describes one callable endpoint of a source.
type EndpointSpec struct {
	ID          string
	Name        string
	Method      string
	URLTemplate string
	Params      []ParamSpec
	Headers     *parser.Object
	PageSize    *int
	Response    ResponseSpec
	CacheTTLS   int
	BudgetMS    int
	Notes       string
}

// ToObject renders the endpoint spec as an ordered object.
func (e EndpointSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("id", e.ID)
	o.Set("name", e.Name)
	o.Set("method", e.Method)
	o.Set("url_template", e.URLTemplate)
	params := make([]any, 0, len(e.Params))
	for _, p := range e.Params {
		params = append(params, p.ToObject())
	}
	o.Set("params", params)
	headers := e.Headers
	if headers == nil {
		headers = parser.NewObject()
	}
	o.Set("headers", headers)
	if e.PageSize != nil {
		o.Set("page_size", *e.PageSize)
	} else {
		o.Set("page_size", nil)
	}
	o.Set("response", e.Response.ToObject())
	o.Set("cache_ttl_s", e.CacheTTLS)
	o.Set("budget_ms", e.BudgetMS)
	o.Set("notes", e.Notes)
	return o
}

// MarshalJSON renders the endpoint spec.
func (e EndpointSpec) MarshalJSON() ([]byte, error) {
	return e.ToObject().MarshalJSON()
}

// SourceSpec describes one data source.
type SourceSpec struct {
	ID                string
	Name              string
	Family            string
	Org               string
	Country           string
	Summary           string
	Homepage          string
	LinkStatus        string
	LinkCheckedAt     string
	Auth              AuthSpec
	Limits            LimitsSpec
	Scale             ScaleSpec
	Increment         IncrementSpec
	Crawl             CrawlSpec
	ProxyPolicy       string
	Risk              string
	Tags              []string
	Aliases           []string
	UARequiresContact bool
	Endpoints         []*EndpointSpec
}

// ToObject renders the source spec as an ordered object.
func (s SourceSpec) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("id", s.ID)
	o.Set("name", s.Name)
	o.Set("family", s.Family)
	o.Set("org", s.Org)
	o.Set("country", s.Country)
	o.Set("summary", s.Summary)
	o.Set("homepage", s.Homepage)
	o.Set("link_status", s.LinkStatus)
	o.Set("link_checked_at", s.LinkCheckedAt)
	o.Set("auth", s.Auth.ToObject())
	o.Set("limits", s.Limits.ToObject())
	o.Set("scale", s.Scale.ToObject())
	o.Set("increment", s.Increment.ToObject())
	o.Set("crawl", s.Crawl.ToObject())
	o.Set("proxy_policy", s.ProxyPolicy)
	o.Set("risk", s.Risk)
	tags := s.Tags
	if tags == nil {
		tags = []string{}
	}
	o.Set("tags", tags)
	aliases := s.Aliases
	if aliases == nil {
		aliases = []string{}
	}
	o.Set("aliases", aliases)
	o.Set("ua_requires_contact", s.UARequiresContact)
	eps := make([]any, 0, len(s.Endpoints))
	for _, e := range s.Endpoints {
		eps = append(eps, e.ToObject())
	}
	o.Set("endpoints", eps)
	return o
}

// MarshalJSON renders the source spec.
func (s SourceSpec) MarshalJSON() ([]byte, error) {
	return s.ToObject().MarshalJSON()
}

// Endpoint returns the endpoint with the given id, or nil.
func (s *SourceSpec) Endpoint(id string) *EndpointSpec {
	for _, e := range s.Endpoints {
		if e.ID == id {
			return e
		}
	}
	return nil
}

// SearchEntry is one source inside the registry search index.
//
// The entry carries the texts the search runs against, precomputed once
// when the registry is built: a request never lower cases or normalises
// the metadata of the sources again.
type SearchEntry struct {
	// Source is the source this entry belongs to.
	Source *SourceSpec
	// Haystack is the lower cased searchable text of the source: the
	// canonical id, every alias, the name, the org, the country, the
	// family, the summary and every endpoint id, joined with "\n".
	Haystack string
	// NormID is the normalised id (lower case ASCII alphanumerics only).
	NormID string
	// NormName is the normalised name (lower case ASCII alphanumerics only).
	NormName string
}

// SearchIndex is the search index of the registry.
//
// It is built once, at the end of the registry build. The request path
// only looks things up: it never scans the sources and never allocates a
// normalised copy of the metadata.
type SearchIndex struct {
	entries []SearchEntry
	// AliasIndex maps a normalised token onto a source id. It holds the
	// aliases, the canonical ids and the names. The aliases win over the
	// ids and the ids win over the names, which is the resolution order
	// of the reference implementation.
	AliasIndex map[string]string
	// ExactIDs maps a source id (verbatim) onto itself.
	ExactIDs map[string]string
}

// Entries returns the index entries in registry order.
func (i *SearchIndex) Entries() []SearchEntry {
	if i == nil {
		return nil
	}
	return i.entries
}

// LookupExact returns the source id of an exact id hit.
func (i *SearchIndex) LookupExact(id string) (string, bool) {
	if i == nil {
		return "", false
	}
	v, ok := i.ExactIDs[id]
	return v, ok
}

// LookupToken returns the source id of a normalised token hit (an alias, a
// canonical id or a name).
func (i *SearchIndex) LookupToken(token string) (string, bool) {
	if i == nil {
		return "", false
	}
	v, ok := i.AliasIndex[token]
	return v, ok
}

// Document is the registry document (the same structure as the Python
// build-registry output).
type Document struct {
	SchemaVersion string
	GeneratedAt   string
	BudgetMS      int
	Count         int
	Families      []string
	Sources       []*SourceSpec
	// Search is the search index of the sources. It is built together
	// with the document and is not part of the exported JSON.
	Search *SearchIndex
}

// ToObject renders the registry document as an ordered object.
func (d *Document) ToObject() *parser.Object {
	o := parser.NewObject()
	o.Set("schema_version", d.SchemaVersion)
	o.Set("generated_at", d.GeneratedAt)
	o.Set("budget_ms", d.BudgetMS)
	o.Set("count", d.Count)
	families := d.Families
	if families == nil {
		families = []string{}
	}
	o.Set("families", families)
	sources := make([]any, 0, len(d.Sources))
	for _, s := range d.Sources {
		sources = append(sources, s.ToObject())
	}
	o.Set("sources", sources)
	return o
}

// MarshalJSON renders the registry document.
func (d *Document) MarshalJSON() ([]byte, error) {
	return d.ToObject().MarshalJSON()
}

// Source looks a source up by id.
func (d *Document) Source(id string) *SourceSpec {
	for _, s := range d.Sources {
		if s.ID == id {
			return s
		}
	}
	return nil
}
