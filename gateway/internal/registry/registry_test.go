package registry

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
)

func TestClassifyAuthRuleOrder(t *testing.T) {
	cases := []struct {
		in      string
		wantTyp string
		wantOpt bool
	}{
		// The historical bug: a naive "key" in text check would classify
		// these rows as key protected.
		{"无key", "none", false},
		{"无key+必须声明含邮箱的UA", "none", false},
		{"无 key", "none", false},
		{"无需", "none", false},
		{"", "none", false},
		{"无", "none", false},
		{"none", "none", false},
		{"-", "none", false},
		// The optional key has to win over the "无key" rule.
		{"可选key", "api_key", true},
		{"无key(可选)", "api_key", true},
		{"可注册key", "api_key", false},
		{"免费key", "api_key", false},
		{"api key", "api_key", false},
		{"api_key", "none", false},
		{"需邮箱", "email_param", false},
		{"邮箱", "email_param", false},
		{"需token", "token", false},
		{"token", "oauth2", false},
		{"oauth", "oauth2", false},
		{"部分付费", "paid", false},
		{"需付费", "paid", false},
		{"账号+付费", "paid", false},
		{"免费/付费", "mixed", false},
		// The reference table checks "部分付费" before "部分免费部分付费",
		// so this text is classified as paid.
		{"部分免费部分付费", "paid", false},
		{"多数免费", "mixed", false},
		{"部分注册", "mixed", false},
		{"完全没听过的说法", "none", false},
	}
	for _, c := range cases {
		typ, opt := ClassifyAuth(c.in)
		if typ != c.wantTyp || opt != c.wantOpt {
			t.Errorf("ClassifyAuth(%q) = (%q,%v), want (%q,%v)", c.in, typ, opt, c.wantTyp, c.wantOpt)
		}
	}
}

func TestParseTotal(t *testing.T) {
	cases := []struct {
		in   string
		want int
		ok   bool
	}{
		{"2025Q1索引340131份；2026-10-21单日3283份/1995个主体/111种表单", 340131, true},
		{"50万份", 500000, true},
		{"1.5亿条", 150000000, true},
		{"约 3300 份/日", 3300, true},
		{"3千条", 3000, true},
		{"9,011,130,327", 9011130327, true},
		{"1,234,567", 1234567, true},
		{"实测约 5000 条", 5000, true},
		{"", 0, false},
		{"没有数字", 0, false},
		// Below the threshold of 100 the unit match is dropped and the
		// fallback kicks in.
		{"12份", 0, false},
	}
	for _, c := range cases {
		got, ok := ParseTotal(c.in)
		if ok != c.ok || (ok && got != c.want) {
			t.Errorf("ParseTotal(%q) = (%d,%v), want (%d,%v)", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestParseUnit(t *testing.T) {
	cases := []struct{ in, want string }{
		{"340131份", "filings"},
		// "表" of "表单" is matched before "种", exactly like the reference.
		{"111种表单", "tables"},
		{"1995个主体", "records"},
		{"29,544个指标", "records"},
		{"500条记录", "records"},
		{"120张表", "tables"},
		{"12取值", "values"},
		{"", ""},
	}
	for _, c := range cases {
		if got := ParseUnit(c.in); got != c.want {
			t.Errorf("ParseUnit(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestSlugify(t *testing.T) {
	cases := []struct {
		name    string
		idx     int
		aliases []string
		want    string
	}{
		{"SEC EDGAR", 0, []string{"sec", "edgar", "sec_edgar"}, "sec_edgar"},
		{"OpenAlex", 1, []string{"openalex", "oa"}, "openalex"},
		{"GLEIF LEI", 2, nil, "gleif_lei"},
		// Too short: the first matching alias wins.
		{"UN 安理会综合名单", 30, []string{"unsc", "un_sanctions"}, "unsc"},
		{"UN 安理会综合名单", 30, []string{"un_sanctions", "unsc"}, "un_sanctions"},
		// Too short and no usable alias: the zero padded index.
		{"中国证监会 公开信息", 11, nil, "src_011"},
		{"中国证监会 公开信息", 11, []string{"证监会"}, "src_011"},
		{"中国证监会 公开信息", 11, []string{"csrc"}, "csrc"},
	}
	for _, c := range cases {
		if got := Slugify(c.name, c.idx, c.aliases); got != c.want {
			t.Errorf("Slugify(%q,%d,%v) = %q, want %q", c.name, c.idx, c.aliases, got, c.want)
		}
	}
}

func TestFoldSlugKeepsASCIIRuns(t *testing.T) {
	if got := Slugify("SEC EDGAR", 0, nil); got != "sec_edgar" {
		t.Errorf("got %q", got)
	}
	if got := Slugify("BIS Entity List / DPL / UVL", 0, nil); got != "bis_entity_list_dpl_uvl" {
		t.Errorf("got %q", got)
	}
	// NFKD turns "é" into "e" plus a combining mark, the mark becomes a
	// separator: "cafe_registry".
	if got := Slugify("Café Registry", 0, nil); got != "cafe_registry" {
		t.Errorf("got %q", got)
	}
}

// testConfig points the configuration at the repository data directory.
func testConfig(t *testing.T) *config.Config {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	// internal/registry -> gateway -> repo root
	root := filepath.Dir(filepath.Dir(wd))
	cfg := config.Load()
	cfg.DataDir = filepath.Join(root, "data")
	return cfg
}

func TestBuildRegistryFromDataFiles(t *testing.T) {
	cfg := testConfig(t)
	doc, err := Build(cfg)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	// The count has to follow the inventory files, which are edited by other
	// agents; the test must not hardcode it.
	rows, err := loadInventory(cfg)
	if err != nil {
		t.Fatalf("loadInventory: %v", err)
	}
	defs, err := loadEndpointDefs(cfg)
	if err != nil {
		t.Fatalf("loadEndpointDefs: %v", err)
	}
	// 国内清单里的行只有在 endpoints.json 里有端点定义时才进运行时(规则 3.1)。
	cnRows, err := loadCNInventory(cfg)
	if err != nil {
		t.Fatalf("loadCNInventory: %v", err)
	}
	want := 0
	for _, r := range rows {
		if strings.TrimSpace(r[CName]) != "" {
			want++
		}
	}
	for _, r := range cnRows {
		name := strings.TrimSpace(r[CName])
		if name == "" {
			continue
		}
		if _, ok := defs[name]; ok {
			want++
		}
	}
	if doc.Count != want {
		t.Errorf("count = %d, want %d", doc.Count, want)
	}
	if doc.SchemaVersion != "1.0" {
		t.Errorf("schema_version = %q", doc.SchemaVersion)
	}
	if doc.BudgetMS != 500 {
		t.Errorf("budget_ms = %d, want 500", doc.BudgetMS)
	}
	byID := map[string]*SourceSpec{}
	for _, s := range doc.Sources {
		if _, dup := byID[s.ID]; dup {
			t.Errorf("duplicate source id %q", s.ID)
		}
		byID[s.ID] = s
	}
	for _, want := range []string{"sec_edgar", "crossref", "openalex", "gleif_lei", "ofac_sdn"} {
		if _, ok := byID[want]; !ok {
			t.Errorf("missing source %q", want)
		}
	}
	// The alias file supplies the ids of the sources whose name has no
	// usable ASCII slug: such a source must not fall back to src_NNN.
	for _, src := range doc.Sources {
		hasASCII := false
		for _, r := range src.Name {
			if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
				hasASCII = true
				break
			}
		}
		if hasASCII {
			continue
		}
		if strings.HasPrefix(src.ID, "src_") {
			t.Errorf("%q has no ASCII slug and no alias: the id is %q", src.Name, src.ID)
		}
	}
	// A source without an endpoint definition gets the generic endpoint.
	generic := 0
	for _, s := range doc.Sources {
		for _, e := range s.Endpoints {
			if e.ID == GenericEndpointID {
				generic++
				if e.CacheTTLS != DefaultCacheTTLS || e.BudgetMS != DefaultBudgetMS {
					t.Errorf("generic endpoint of %q: ttl=%d budget=%d", s.ID, e.CacheTTLS, e.BudgetMS)
				}
			}
		}
	}
	if generic == 0 {
		t.Error("expected at least one generic endpoint")
	}
}

func TestProxyPolicyAndContactDetection(t *testing.T) {
	cfg := testConfig(t)
	doc, err := Build(cfg)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	never, fallback := 0, 0
	for _, s := range doc.Sources {
		switch s.ProxyPolicy {
		case "never":
			never++
		case "fallback":
			fallback++
		default:
			t.Errorf("%q has proxy policy %q", s.ID, s.ProxyPolicy)
		}
	}
	if never == 0 || fallback == 0 {
		t.Errorf("proxy policies: never=%d fallback=%d", never, fallback)
	}
	// SEC EDGAR demands a contact address in the User-Agent.
	sec := doc.Source("sec_edgar")
	if sec == nil {
		t.Fatal("sec_edgar not found")
	}
	if !sec.UARequiresContact {
		t.Error("sec_edgar must require a contact address")
	}
	if sec.Auth.Type != "none" {
		t.Errorf("sec_edgar auth type = %q, want none", sec.Auth.Type)
	}
}

func TestLoadAliasesMissingFileIsGraceful(t *testing.T) {
	if _, err := loadAliasesFile(filepath.Join(t.TempDir(), "aliases.json")); err == nil {
		t.Error("a missing alias file must report an error")
	}
	// The registry itself keeps working without the alias file.
	cfg := testConfig(t)
	aliases, err := LoadAliases(cfg)
	if err != nil {
		t.Fatalf("LoadAliases: %v", err)
	}
	if len(aliases) == 0 {
		t.Error("expected the aliases of the repository data directory")
	}
}
