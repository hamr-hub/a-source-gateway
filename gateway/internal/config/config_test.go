package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadDefaults(t *testing.T) {
	for _, k := range []string{
		"A_GATEWAY_HOST", "A_GATEWAY_PORT", "A_GATEWAY_BUDGET_MS", "A_GATEWAY_UPSTREAM_BUDGET_MS",
		"A_GATEWAY_RESERVE_MS", "A_GATEWAY_MAX_BODY_BYTES", "A_GATEWAY_MAX_PARSE_ROWS",
		"A_GATEWAY_POOL_SIZE", "A_GATEWAY_CACHE_ENTRIES", "A_GATEWAY_CACHE_BYTES",
		"A_GATEWAY_CACHE_TTL_S", "A_GATEWAY_WARM_BUDGET_MS", "A_GATEWAY_PREWARM",
		"A_GATEWAY_CONTACT", "A_GATEWAY_UA", "A_GATEWAY_OFFLINE", "A_GATEWAY_VERIFY",
		"A_GATEWAY_DATA_DIR", "A_GATEWAY_PARSER", "A_GATEWAY_PARSER_PYTHON",
		"A_GATEWAY_HTTP2",
	} {
		t.Setenv(k, "")
		os.Unsetenv(k)
	}
	c := Load()
	if c.Host != "127.0.0.1" || c.Port != 8848 {
		t.Errorf("addr = %s:%d", c.Host, c.Port)
	}
	if c.BudgetMS != 500 || c.UpstreamBudgetMS != 400 || c.ReserveMS != 100 {
		t.Errorf("budgets = %d/%d/%d", c.BudgetMS, c.UpstreamBudgetMS, c.ReserveMS)
	}
	if c.MaxBodyBytes != 1024*1024 || c.MaxParseRows != 15000 {
		t.Errorf("body/rows = %d/%d", c.MaxBodyBytes, c.MaxParseRows)
	}
	if c.ConnectPool != 32 || c.CacheEntries != 512 || c.CacheBytes != 32*1024*1024 {
		t.Errorf("pool/cache = %d/%d/%d", c.ConnectPool, c.CacheEntries, c.CacheBytes)
	}
	if c.CacheTTLSeconds != 900 || c.WarmBudgetMS != 30000 {
		t.Errorf("ttl/warm = %d/%d", c.CacheTTLSeconds, c.WarmBudgetMS)
	}
	if !c.Prewarm {
		t.Error("prewarm must default to true")
	}
	if c.Offline {
		t.Error("offline must default to false")
	}
	if !c.VerifyTLS {
		t.Error("TLS verification must default to true")
	}
	if !c.HTTP2 {
		t.Error("HTTP/2 must default to true")
	}
	if c.ParserMode != ParserAuto || c.ParserPython != "python3" {
		t.Errorf("parser = %q/%q", c.ParserMode, c.ParserPython)
	}
	want := "a-source-gateway/1.0 (+https://github.com/hamr-hub/a-source-gateway)"
	if c.UserAgent != want {
		t.Errorf("ua = %q, want %q", c.UserAgent, want)
	}
}

func TestLoadOverrides(t *testing.T) {
	t.Setenv("A_GATEWAY_HOST", "0.0.0.0")
	t.Setenv("A_GATEWAY_PORT", "9000")
	t.Setenv("A_GATEWAY_BUDGET_MS", "750")
	t.Setenv("A_GATEWAY_CONTACT", "ops@example.com")
	t.Setenv("A_GATEWAY_OFFLINE", "1")
	t.Setenv("A_GATEWAY_VERIFY", "none")
	t.Setenv("A_GATEWAY_PARSER", "go")
	c := Load()
	if c.Host != "0.0.0.0" || c.Port != 9000 || c.BudgetMS != 750 {
		t.Errorf("cfg = %+v", c)
	}
	if !c.Offline {
		t.Error("offline must be true")
	}
	if c.VerifyTLS {
		t.Error("A_GATEWAY_VERIFY=none must disable the verification")
	}
	if c.ParserMode != ParserGo {
		t.Errorf("parser = %q", c.ParserMode)
	}
	want := "a-source-gateway/1.0 (+ops@example.com)"
	if c.UserAgent != want {
		t.Errorf("ua = %q, want %q", c.UserAgent, want)
	}
}

// A_GATEWAY_HTTP2=0 turns the protocol off for upstreams with a broken h2 stack.
func TestHTTP2CanBeDisabled(t *testing.T) {
	t.Setenv("A_GATEWAY_HTTP2", "0")
	c := Load()
	if c.HTTP2 {
		t.Error("A_GATEWAY_HTTP2=0 must disable HTTP/2")
	}
	t.Setenv("A_GATEWAY_HTTP2", "1")
	c = Load()
	if !c.HTTP2 {
		t.Error("A_GATEWAY_HTTP2=1 must enable HTTP/2")
	}
}

func TestUserAgentOverride(t *testing.T) {
	t.Setenv("A_GATEWAY_UA", "custom-agent/2.0")
	c := Load()
	if c.UserAgent != "custom-agent/2.0" {
		t.Errorf("ua = %q", c.UserAgent)
	}
}

func TestFindData(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "endpoints.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	c := Load()
	c.DataDir = dir
	p, err := c.FindData("endpoints.json")
	if err != nil {
		t.Fatalf("FindData: %v", err)
	}
	if p != filepath.Join(dir, "endpoints.json") {
		t.Errorf("path = %q", p)
	}
	if _, err := c.FindData("does-not-exist.json"); err == nil {
		t.Error("expected an error for a missing data file")
	}
}

func TestFindPythonSrcDir(t *testing.T) {
	dir := t.TempDir()
	pkg := filepath.Join(dir, "a_source_gateway")
	if err := os.MkdirAll(pkg, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(pkg, "__init__.py"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	c := Load()
	c.DataDir = dir
	// The search starts at the working directory, so the temporary package
	// is only found when it is below the working directory. The function
	// must at least not fail when nothing is found.
	if got := c.FindPythonSrcDir(); got != "" {
		if _, err := os.Stat(filepath.Join(got, "a_source_gateway")); err != nil {
			t.Errorf("FindPythonSrcDir returned %q without a package", got)
		}
	}
}
