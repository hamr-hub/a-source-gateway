// Package config loads the gateway runtime configuration from environment
// variables.
//
// The variable names and defaults mirror the Python reference implementation
// (python/src/a_source_gateway/config.py) exactly, so both runtimes can be
// deployed side by side with the same environment.
//
// Only the standard library is used; no configuration file format is involved.
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Parser engine selection modes for A_GATEWAY_PARSER.
const (
	ParserAuto   = "auto"
	ParserPython = "python"
	ParserGo     = "go"
)

// Version is the release version of the gateway.
//
// It is a var, not a const, so the build can inject the real value:
//
//	go build -ldflags "-X github.com/hamr-hub/a-source-gateway/gateway/internal/config.Version=1.5.1"
//
// The Makefile and release workflow both read the version from pyproject.toml
// (the single source of truth) and inject it, so the Go binary and the Python
// package can never report different versions.
var Version = "1.5.1"

// Config is the fully resolved runtime configuration.
type Config struct {
	// Service.
	Host    string
	Port    int
	Workers int

	// Budgets.
	BudgetMS         int
	UpstreamBudgetMS int
	ReserveMS        int
	MaxBodyBytes     int
	MaxParseRows     int
	ConnectPool      int

	// Cache.
	CacheEntries    int
	CacheBytes      int
	CacheTTLSeconds int
	WarmBudgetMS    int
	Prewarm         bool

	// Identity / network.
	Contact   string
	UserAgent string
	Offline   bool
	VerifyTLS bool
	// HTTP2 enables HTTP/2 for upstream calls (ALPN negotiation).
	//
	// The upstream transport sets a custom DialContext, which makes the Go
	// standard library conservatively disable HTTP/2. Forcing it back on
	// lets the gateway multiplex repeated calls to one host over a single
	// connection instead of paying a TCP + TLS handshake per call.
	// A_GATEWAY_HTTP2=0 turns it off for upstreams with a broken h2 stack.
	HTTP2 bool

	// Data files.
	DataDir string

	// Parsing plane.
	ParserMode   string
	ParserPython string
}

// Load reads the configuration from the process environment.
//
// Semantics are copied from the Python reference (including its quirks):
//   - values are whitespace-trimmed;
//   - an empty integer variable falls back to the default, while an empty
//     boolean variable means false (A_GATEWAY_PREWARM="" disables prewarm);
//   - A_GATEWAY_VERIFY disables TLS verification only for the literal value
//     "none" (any other value keeps verification on).
func Load() *Config {
	c := &Config{
		Host:             envStr("A_GATEWAY_HOST", "127.0.0.1"),
		Port:             envInt("A_GATEWAY_PORT", 8848),
		Workers:          envInt("A_GATEWAY_WORKERS", 1),
		BudgetMS:         envInt("A_GATEWAY_BUDGET_MS", 500),
		UpstreamBudgetMS: envInt("A_GATEWAY_UPSTREAM_BUDGET_MS", 400),
		ReserveMS:        envInt("A_GATEWAY_RESERVE_MS", 100),
		MaxBodyBytes:     envInt("A_GATEWAY_MAX_BODY_BYTES", 1024*1024),
		MaxParseRows:     envInt("A_GATEWAY_MAX_PARSE_ROWS", 15000),
		ConnectPool:      envInt("A_GATEWAY_POOL_SIZE", 32),
		CacheEntries:     envInt("A_GATEWAY_CACHE_ENTRIES", 512),
		CacheBytes:       envInt("A_GATEWAY_CACHE_BYTES", 32*1024*1024),
		CacheTTLSeconds:  envInt("A_GATEWAY_CACHE_TTL_S", 900),
		WarmBudgetMS:     envInt("A_GATEWAY_WARM_BUDGET_MS", 30000),
		Prewarm:          envBool("A_GATEWAY_PREWARM", true),
		Contact:          envStr("A_GATEWAY_CONTACT", ""),
		Offline:          envBool("A_GATEWAY_OFFLINE", false),
		DataDir:          envStr("A_GATEWAY_DATA_DIR", ""),
		ParserMode:       strings.ToLower(envStr("A_GATEWAY_PARSER", ParserAuto)),
		ParserPython:     envStr("A_GATEWAY_PARSER_PYTHON", "python3"),
		HTTP2:            envBool("A_GATEWAY_HTTP2", true),
	}
	c.VerifyTLS = strings.ToLower(envStr("A_GATEWAY_VERIFY", "true")) != "none"

	if ua := envStr("A_GATEWAY_UA", ""); ua != "" {
		c.UserAgent = ua
	} else if c.Contact != "" {
		c.UserAgent = "a-source-gateway/1.0 (+" + c.Contact + ")"
	} else {
		c.UserAgent = "a-source-gateway/1.0 (+https://github.com/hamr-hub/a-source-gateway)"
	}

	switch c.ParserMode {
	case ParserAuto, ParserPython, ParserGo:
	default:
		c.ParserMode = ParserAuto
	}
	if c.ParserPython == "" {
		c.ParserPython = "python3"
	}
	return c
}

// Headers returns the default request headers sent to every upstream.
func (c *Config) Headers() map[string]string {
	return map[string]string{
		"User-Agent":      c.UserAgent,
		"Accept-Encoding": "identity",
	}
}

// Addr is the listen address of the HTTP server.
func (c *Config) Addr() string {
	return fmt.Sprintf("%s:%d", c.Host, c.Port)
}

// dataCandidates lists the directories that may hold the declarative data
// files. The Python reference looks next to the installed package; the Go
// binary walks up from the working directory and from the executable
// directory instead, which covers "repo/gateway" -> "repo/data".
func (c *Config) dataCandidates() []string {
	var out []string
	if c.DataDir != "" {
		if abs, err := filepath.Abs(c.DataDir); err == nil {
			out = append(out, abs)
		} else {
			out = append(out, c.DataDir)
		}
	}
	out = append(out, walkUpDataDirs(".")...)
	if exe, err := os.Executable(); err == nil {
		out = append(out, walkUpDataDirs(filepath.Dir(exe))...)
	}
	return out
}

func walkUpDataDirs(start string) []string {
	var out []string
	dir, err := filepath.Abs(start)
	if err != nil {
		return out
	}
	for i := 0; i < 8; i++ {
		out = append(out, filepath.Join(dir, "data"))
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return out
}

// FindData locates a declarative data file (source_inventory.csv, endpoints.json,
// aliases.json) and returns its path.
func (c *Config) FindData(filename string) (string, error) {
	cands := c.dataCandidates()
	for _, d := range cands {
		p := filepath.Join(d, filename)
		if st, err := os.Stat(p); err == nil && !st.IsDir() {
			return p, nil
		}
	}
	return "", fmt.Errorf("找不到数据文件 %s; 已尝试: %s", filename, strings.Join(cands, ", "))
}

// FindPythonSrcDir locates the directory that must be on PYTHONPATH so that
// "python3 -m a_source_gateway.parser_worker" resolves.
//
// The repository layout is <repo>/python/src/a_source_gateway/, so the parent
// of the package directory is what we are after. An empty string means "no
// candidate found" and the worker is started with the inherited environment.
func (c *Config) FindPythonSrcDir() string {
	var starts []string
	starts = append(starts, ".")
	if exe, err := os.Executable(); err == nil {
		starts = append(starts, filepath.Dir(exe))
	}
	for _, start := range starts {
		dir, err := filepath.Abs(start)
		if err != nil {
			continue
		}
		for i := 0; i < 8; i++ {
			for _, cand := range []string{
				filepath.Join(dir, "a_source_gateway"),
				filepath.Join(dir, "src", "a_source_gateway"),
				filepath.Join(dir, "python", "src", "a_source_gateway"),
			} {
				if st, err := os.Stat(cand); err == nil && st.IsDir() {
					return filepath.Dir(cand)
				}
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return ""
}

// envStr mirrors the Python helper of the same name: an unset variable yields
// the default, a variable set to the empty string yields the empty string.
func envStr(name, def string) string {
	v, ok := os.LookupEnv(name)
	if !ok {
		return def
	}
	return strings.TrimSpace(v)
}

func envInt(name string, def int) int {
	s := envStr(name, strconv.Itoa(def))
	if s == "" {
		return def
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return n
}

func envBool(name string, def bool) bool {
	fallback := "0"
	if def {
		fallback = "1"
	}
	switch strings.ToLower(envStr(name, fallback)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}
