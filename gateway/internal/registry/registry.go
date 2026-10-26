package registry

import (
	"encoding/csv"
	"fmt"
	"log"
	"os"
	"sort"
	"strings"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
)

// CSV column contract (mirrors python/src/a_source_gateway/registry.py).
const (
	CFamily    = "分类"
	CName      = "数据源"
	COrg       = "机构"
	CCountry   = "国别"
	CContent   = "数据内容"
	CEntry     = "官方入口"
	CAuth      = "认证方式"
	CLimits    = "官方限速口径"
	CBulk      = "全量下载"
	CFreq      = "更新频率"
	CScale     = "实测规模"
	CIncrement = "实测增量"
	CProxy     = "代理需求"
	CRisk      = "合规风险"
	CCrawl     = "建议采集方式"
	CLink      = "链接状态"
	CLinkMS    = "探测耗时ms"
	// CVerified 是这条元数据最近一次被真实请求核对过的日期(ISO)。
	// 空着表示"还没核过" —— 留空比填一个自己没跑过的日期诚实。
	CVerified = "最后验证"
)

// DefaultCheckedAt 是 最后验证 列为空时的取值。
//
// 它是一个明确的哨兵而不是某个历史日期: 以前两边都硬编码 "2026-10-22",
// 于是不管多久没核过, 对外都宣称是那天验证的。
const DefaultCheckedAt = "unverified"

// Document level constants of the Python reference.
const (
	// SchemaVersion is the version of the registry and of the envelope.
	// 1.2: the source list projection carries the aliases of a source.
	SchemaVersion      = "1.2"
	GeneratedAt        = "2026-10-22"
	DefaultCacheTTLS   = 900
	DefaultBudgetMS    = 400
	GenericEndpointID  = "entry"
	GenericEndpointNam = "官方入口(通用)"
)

// verifiedAt reads the "last verified" column of an inventory row.
//
// The registry must not claim a verification date it has no evidence for:
// a source nobody re-checked reports the "unverified" sentinel, which is
// what the Python reference does as well.
func verifiedAt(r map[string]string) string {
	if v := strings.TrimSpace(r[CVerified]); v != "" {
		return v
	}
	return DefaultCheckedAt
}

// Build assembles the registry from the declarative data files.
func Build(cfg *config.Config) (*Document, error) {
	rows, err := loadInventory(cfg)
	if err != nil {
		return nil, err
	}
	defs, err := loadEndpointDefs(cfg)
	if err != nil {
		return nil, err
	}
	aliases, err := LoadAliases(cfg)
	if err != nil {
		return nil, err
	}

	// 国内清单(规则 3.1):只有已在 endpoints.json 里有端点定义的国内源才进运行时;
	// 纯候选源(还没接入的)留在 CSV 里, 不进注册表。
	cnRows, err := loadCNInventory(cfg)
	if err != nil {
		return nil, err
	}
	seenNames := map[string]bool{}
	for _, r := range rows {
		seenNames[strings.TrimSpace(r[CName])] = true
	}
	for _, r := range cnRows {
		name := strings.TrimSpace(r[CName])
		if name == "" {
			continue
		}
		if _, ok := defs[name]; !ok {
			continue
		}
		if seenNames[name] {
			// 同一个源出现在两份清单 → 字段会漂, 这里以海外清单为准并告警
			log.Printf("[registry] 警告: 源 %s 同时出现在两份清单里, 运行时只采用海外清单那一行", name)
			continue
		}
		seenNames[name] = true
		rows = append(rows, r)
	}

	sources := []*SourceSpec{}
	used := map[string]int{}

	for i, r := range rows {
		name := strings.TrimSpace(r[CName])
		if name == "" {
			continue
		}
		als := aliases[name]
		sid := Slugify(name, i, als)
		if n, ok := used[sid]; ok {
			used[sid] = n + 1
			sid = fmt.Sprintf("%s_%d", sid, n+1)
		} else {
			used[sid] = 1
		}

		scaleText := strings.TrimSpace(r[CScale])
		incText := strings.TrimSpace(r[CIncrement])
		entry := strings.TrimSpace(r[CEntry])

		var eps []*EndpointSpec
		for _, d := range defs[name] {
			ep, err := buildEndpoint(d)
			if err != nil {
				return nil, fmt.Errorf("endpoints.json[%s]: %w", name, err)
			}
			eps = append(eps, ep)
		}
		if len(eps) == 0 {
			eps = []*EndpointSpec{genericEndpoint(entry)}
		}

		authTxt := strings.TrimSpace(r[CAuth])
		authType, authOpt := ClassifyAuth(authTxt)

		blob := strings.Join([]string{authTxt, r[CLimits], r[CContent]}, " ")
		uaRequiresContact := strings.Contains(blob, "邮箱") ||
			strings.Contains(blob, "mailto") ||
			strings.Contains(blob, "UA")

		proxyTxt := strings.TrimSpace(r[CProxy])
		proxyPolicy := "fallback"
		if strings.HasPrefix(proxyTxt, "不走") {
			proxyPolicy = "never"
		}

		risk := strings.TrimSpace(r[CRisk])
		if risk == "" {
			risk = "low"
		}

		tags := []string{}
		if t := runeTruncate(strings.TrimSpace(r[CFamily]), 2); t != "" {
			tags = append(tags, t)
		}
		if t := strings.TrimSpace(r[CCountry]); t != "" {
			tags = append(tags, t)
		}

		src := &SourceSpec{
			ID:            sid,
			Name:          name,
			Family:        strings.TrimSpace(r[CFamily]),
			Org:           strings.TrimSpace(r[COrg]),
			Country:       strings.TrimSpace(r[CCountry]),
			Summary:       strings.TrimSpace(r[CContent]),
			Homepage:      entry,
			LinkStatus:    strings.TrimSpace(r[CLink]),
			LinkCheckedAt: verifiedAt(r),
			Auth: AuthSpec{
				Type:     authType,
				Optional: authOpt,
				Note:     authTxt,
			},
			Limits: LimitsSpec{Note: strings.TrimSpace(r[CLimits])},
			Scale: ScaleSpec{
				Unit:       ParseUnit(scaleText),
				MeasuredAt: GeneratedAt,
				Basis:      scaleText,
			},
			Increment: IncrementSpec{
				Frequency: strings.TrimSpace(r[CFreq]),
				Daily:     incText,
				Strategy:  strings.TrimSpace(r[CCrawl]),
			},
			Crawl: CrawlSpec{
				Mode:  "query",
				Notes: strings.TrimSpace(r[CBulk]),
			},
			ProxyPolicy:       proxyPolicy,
			Risk:              risk,
			Tags:              tags,
			Aliases:           als,
			UARequiresContact: uaRequiresContact,
			Endpoints:         eps,
		}
		if total, ok := ParseTotal(scaleText); ok {
			t := total
			src.Scale.Total = &t
		}
		sources = append(sources, src)
	}

	familySet := map[string]bool{}
	for _, s := range sources {
		familySet[s.Family] = true
	}
	families := make([]string, 0, len(familySet))
	for f := range familySet {
		families = append(families, f)
	}
	sort.Strings(families)

	return &Document{
		SchemaVersion: SchemaVersion,
		GeneratedAt:   GeneratedAt,
		BudgetMS:      cfg.BudgetMS,
		Count:         len(sources),
		Families:      families,
		Sources:       sources,
		// The search index is computed here, once: the request path only
		// looks things up, it never scans and normalises the sources again.
		Search: BuildSearchIndex(sources),
	}, nil
}

// BuildSearchIndex computes the search index of a list of sources.
//
// The index holds two things:
//
//   - the haystack of every source (the lower cased searchable text), used
//     by the keyword search of the source list;
//   - the exact hit tables (the canonical ids, the aliases and the names),
//     used to resolve a caller supplied key onto a source id.
//
// The index is the only place where the metadata of the sources is
// lower cased and normalised, so the request path stays allocation free.
func BuildSearchIndex(sources []*SourceSpec) *SearchIndex {
	idx := &SearchIndex{
		entries:    make([]SearchEntry, 0, len(sources)),
		AliasIndex: map[string]string{},
		ExactIDs:   map[string]string{},
	}
	for _, s := range sources {
		if s == nil {
			continue
		}
		parts := make([]string, 0, 6+len(s.Aliases)+len(s.Endpoints))
		parts = append(parts, s.ID, s.Name, s.Org, s.Country, s.Family, s.Summary)
		parts = append(parts, s.Aliases...)
		for _, e := range s.Endpoints {
			if e != nil {
				parts = append(parts, e.ID)
			}
		}
		idx.entries = append(idx.entries, SearchEntry{
			Source:   s,
			Haystack: strings.ToLower(strings.Join(parts, "\n")),
			NormID:   NormalizeKey(s.ID),
			NormName: NormalizeKey(s.Name),
		})
		// The first source that claims an id keeps it, like the linear
		// scan of the reference implementation.
		if _, ok := idx.ExactIDs[s.ID]; !ok {
			idx.ExactIDs[s.ID] = s.ID
		}
	}
	// The exact hit table is filled by priority: an alias wins over a
	// canonical id, a canonical id wins over a name. Within one priority
	// the first source of the registry wins, exactly like the linear scan
	// of the reference implementation.
	for _, s := range sources {
		if s == nil {
			continue
		}
		for _, a := range s.Aliases {
			putToken(idx.AliasIndex, NormalizeKey(a), s.ID)
		}
	}
	for _, s := range sources {
		if s == nil {
			continue
		}
		putToken(idx.AliasIndex, NormalizeKey(s.ID), s.ID)
	}
	for _, s := range sources {
		if s == nil {
			continue
		}
		putToken(idx.AliasIndex, NormalizeKey(s.Name), s.ID)
	}
	return idx
}

// putToken records a normalised token, keeping the first source that claims it.
func putToken(index map[string]string, token, sourceID string) {
	if token == "" {
		return
	}
	if _, ok := index[token]; ok {
		return
	}
	index[token] = sourceID
}

// NormalizeKey strips everything that is not a lower case ASCII letter or
// digit.
//
// It mirrors the normalisation of the Python reference
// (re.sub(r"[^a-z0-9]+", "", key.lower())); both planes must keep the same
// semantics, otherwise a key resolves on one plane and not on the other.
func NormalizeKey(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range strings.ToLower(s) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// buildEndpoint converts one endpoint definition into an EndpointSpec.
func buildEndpoint(d *parser.Object) (*EndpointSpec, error) {
	id, _ := d.Get("id")
	idStr, ok := id.(string)
	if !ok || idStr == "" {
		return nil, fmt.Errorf("endpoint definition without id")
	}
	ep := &EndpointSpec{
		ID:          idStr,
		Name:        objString(d, "name", idStr),
		Method:      objString(d, "method", "GET"),
		URLTemplate: objString(d, "url_template", ""),
		CacheTTLS:   objIntOr(d, "cache_ttl_s", DefaultCacheTTLS),
		BudgetMS:    objIntOr(d, "budget_ms", DefaultBudgetMS),
		Notes:       objString(d, "notes", ""),
	}
	if v := objIntOrPtr(d, "budget_ms"); v == nil || *v == 0 {
		ep.BudgetMS = DefaultBudgetMS
	}
	if v := objIntOrPtr(d, "page_size"); v != nil {
		ep.PageSize = v
	}
	if h, ok := d.Get("headers"); ok {
		if ho, ok := h.(*parser.Object); ok {
			ep.Headers = ho
		}
	}
	if params, ok := d.Get("params"); ok {
		if list, ok := params.([]any); ok {
			for _, item := range list {
				po, ok := item.(*parser.Object)
				if !ok {
					continue
				}
				p := ParamSpec{
					Name:     objString(po, "name", ""),
					In:       objString(po, "in", "query"),
					Type:     objString(po, "type", "string"),
					Required: objBool(po, "required"),
					Desc:     objString(po, "desc", ""),
					Env:      objString(po, "env", ""),
				}
				if pat, ok := po.Get("pattern"); ok {
					if s, ok := pat.(string); ok {
						p.Pattern = &s
					}
				}
				if v, ok := po.Get("default"); ok && v != nil {
					p.Default = v
				}
				if v, ok := po.Get("example"); ok && v != nil {
					p.Example = v
				}
				ep.Params = append(ep.Params, p)
			}
		}
	}
	if resp, ok := d.Get("response"); ok {
		if ro, ok := resp.(*parser.Object); ok {
			ep.Response = ResponseSpec{
				MediaType:   objString(ro, "media_type", "application/json"),
				Shape:       objString(ro, "shape", "object"),
				ItemPath:    objString(ro, "item_path", ""),
				ItemShape:   objString(ro, "item_shape", "list"),
				ItemFlatten: objBool(ro, "item_flatten"),
				TotalField:  objString(ro, "total_field", ""),
				Notes:       objString(ro, "notes", ""),
			}
			if fields, ok := ro.Get("fields"); ok {
				if list, ok := fields.([]any); ok {
					for _, item := range list {
						fo, ok := item.(*parser.Object)
						if !ok {
							continue
						}
						f := FieldSpec{
							Name: objString(fo, "name", ""),
							Type: objString(fo, "type", "string"),
							Desc: objString(fo, "desc", ""),
						}
						if path, ok := fo.Get("path"); ok {
							if s, ok := path.(string); ok {
								f.Path = &s
							}
						}
						ep.Response.Fields = append(ep.Response.Fields, f)
					}
				}
			}
		}
	}
	return ep, nil
}

// genericEndpoint builds the fallback endpoint of a source without a
// dedicated endpoint definition.
func genericEndpoint(entry string) *EndpointSpec {
	shape := "object"
	switch {
	case strings.HasSuffix(entry, ".xml"):
		shape = "xml"
	case strings.HasSuffix(entry, ".csv"):
		shape = "csv"
	case strings.HasSuffix(entry, ".tsv"):
		shape = "tsv"
	}
	mediaType := "*/*"
	switch shape {
	case "xml":
		mediaType = "application/xml"
	case "csv":
		mediaType = "text/csv"
	case "tsv":
		mediaType = "text/tab-separated-values"
	}
	return &EndpointSpec{
		ID:          GenericEndpointID,
		Name:        GenericEndpointNam,
		Method:      "GET",
		URLTemplate: entry,
		Headers:     parser.NewObject(),
		CacheTTLS:   DefaultCacheTTLS,
		BudgetMS:    DefaultBudgetMS,
		Response: ResponseSpec{
			MediaType: mediaType,
			Shape:     shape,
			Fields:    []FieldSpec{},
			ItemShape: "list",
			Notes:     "通用透传端点(官方入口直取);结构随官方文件而定, 建议先取该源的专用端点",
		},
	}
}

// loadInventory reads data/source_inventory.csv.
func loadInventory(cfg *config.Config) ([]map[string]string, error) {
	path, err := cfg.FindData("source_inventory.csv")
	if err != nil {
		return nil, err
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	r := csv.NewReader(f)
	r.FieldsPerRecord = -1
	r.LazyQuotes = true
	records, err := r.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	if len(records) == 0 {
		return nil, fmt.Errorf("%s is empty", path)
	}
	header := records[0]
	rows := make([]map[string]string, 0, len(records)-1)
	for _, rec := range records[1:] {
		row := make(map[string]string, len(header))
		for i, col := range header {
			if i < len(rec) {
				row[col] = rec[i]
			} else {
				row[col] = ""
			}
		}
		rows = append(rows, row)
	}
	return rows, nil
}

// loadCNInventory reads data/source_inventory_cn.csv (国内清单, 规则 3.1).
// 文件缺失不是错误 —— 注册表必须永远能构建, 但要告警:
// 缺这个文件意味着已接入的国内源会在运行时消失(发布/安装流程漏装数据文件的典型症状)。
func loadCNInventory(cfg *config.Config) ([]map[string]string, error) {
	path, err := cfg.FindData("source_inventory_cn.csv")
	if err != nil {
		log.Printf("[registry] 警告: 找不到国内清单 source_inventory_cn.csv; " +
			"已接入的国内源不会出现在运行时(检查发布/安装流程是否漏装该文件)")
		return nil, nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	r := csv.NewReader(f)
	r.FieldsPerRecord = -1
	r.LazyQuotes = true
	records, err := r.ReadAll()
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	if len(records) == 0 {
		return nil, nil
	}
	header := records[0]
	rows := make([]map[string]string, 0, len(records)-1)
	for _, rec := range records[1:] {
		row := make(map[string]string, len(header))
		for i, col := range header {
			if i < len(rec) {
				row[col] = rec[i]
			} else {
				row[col] = ""
			}
		}
		rows = append(rows, row)
	}
	return rows, nil
}

// loadEndpointDefs reads data/endpoints.json.
func loadEndpointDefs(cfg *config.Config) (map[string][]*parser.Object, error) {
	path, err := cfg.FindData("endpoints.json")
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	v, err := parser.UnmarshalOrdered(raw)
	if err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	root, ok := v.(*parser.Object)
	if !ok {
		return nil, fmt.Errorf("%s: expected a JSON object", path)
	}
	sourcesVal, _ := root.Get("sources")
	sourcesObj, ok := sourcesVal.(*parser.Object)
	if !ok {
		return map[string][]*parser.Object{}, nil
	}
	out := map[string][]*parser.Object{}
	for _, key := range sourcesObj.Keys() {
		val, _ := sourcesObj.Get(key)
		list, ok := val.([]any)
		if !ok {
			continue
		}
		eps := []*parser.Object{}
		for _, item := range list {
			if o, ok := item.(*parser.Object); ok {
				eps = append(eps, o)
			}
		}
		out[key] = eps
	}
	return out, nil
}

// LoadAliases reads data/aliases.json.
//
// The file is optional: when it is missing the sources simply have no
// aliases and a warning is logged. The alias table is never hard coded.
func LoadAliases(cfg *config.Config) (map[string][]string, error) {
	path, err := cfg.FindData("aliases.json")
	if err != nil {
		return map[string][]string{}, nil
	}
	return loadAliasesFile(path)
}

// loadAliasesFile reads one alias file.
func loadAliasesFile(path string) (map[string][]string, error) {
	out := map[string][]string{}
	raw, err := os.ReadFile(path)
	if err != nil {
		return out, err
	}
	v, err := parser.UnmarshalOrdered(raw)
	if err != nil {
		return out, fmt.Errorf("parse %s: %w", path, err)
	}
	root, ok := v.(*parser.Object)
	if !ok {
		return out, fmt.Errorf("%s: expected a JSON object", path)
	}
	aliasesVal, _ := root.Get("aliases")
	aliasesObj, ok := aliasesVal.(*parser.Object)
	if !ok {
		return out, nil
	}
	for _, name := range aliasesObj.Keys() {
		val, _ := aliasesObj.Get(name)
		list, ok := val.([]any)
		if !ok {
			continue
		}
		items := []string{}
		for _, item := range list {
			if s, ok := item.(string); ok {
				items = append(items, s)
			}
		}
		out[name] = items
	}
	return out, nil
}

// AliasesPath returns the path of the alias file, or an empty string when it
// does not exist.
func AliasesPath(cfg *config.Config) string {
	path, err := cfg.FindData("aliases.json")
	if err != nil {
		return ""
	}
	return path
}

func objString(o *parser.Object, key, def string) string {
	v, ok := o.Get(key)
	if !ok || v == nil {
		return def
	}
	switch t := v.(type) {
	case string:
		return t
	case bool:
		if t {
			return "true"
		}
		return "false"
	default:
		return parser.PyRepr(v)
	}
}

func objBool(o *parser.Object, key string) bool {
	v, ok := o.Get(key)
	if !ok || v == nil {
		return false
	}
	b, _ := v.(bool)
	return b
}

func objIntOr(o *parser.Object, key string, def int) int {
	if v := objIntOrPtr(o, key); v != nil {
		return *v
	}
	return def
}

func objIntOrPtr(o *parser.Object, key string) *int {
	v, ok := o.Get(key)
	if !ok || v == nil {
		return nil
	}
	if n, ok := parser.ToInt(v); ok {
		return &n
	}
	return nil
}

// runeTruncate truncates a string by runes (Python slices by code points).
func runeTruncate(s string, n int) string {
	rs := []rune(s)
	if len(rs) <= n {
		return s
	}
	return string(rs[:n])
}
