// Command gateway is the Go runtime of the A-Class Source Gateway.
//
// Usage:
//
//	gateway serve                    # start the HTTP service
//	gateway registry-dump -o out.json # export the registry (parity with the Python build-registry)
//	gateway openapi -o openapi.json   # export the OpenAPI document
//	gateway version
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/hamr-hub/a-source-gateway/gateway/internal/cache"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/config"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/metrics"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/parser"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/ratelimit"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/registry"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/server"
	"github.com/hamr-hub/a-source-gateway/gateway/internal/upstream"
)

func main() {
	logger := log.New(os.Stderr, "", log.LstdFlags|log.Lmsgprefix)
	cmd := "serve"
	args := os.Args[1:]
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd = args[0]
		args = args[1:]
	}
	var err error
	switch cmd {
	case "serve":
		err = serve(args, logger)
	case "registry-dump":
		err = registryDump(args, logger)
	case "openapi":
		err = openapiDump(args, logger)
	case "version":
		fmt.Println("a-source-gateway (go) 1.0")
	case "help", "-h", "--help":
		usage()
	default:
		usage()
		err = fmt.Errorf("未知命令 %q", cmd)
	}
	if err != nil {
		logger.Printf("error: %v", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Println(`a-source-gateway (go)

用法:
  gateway serve                      启动 HTTP 网关
  gateway registry-dump -o file     导出注册表(与 Python build-registry 同结构)
  gateway openapi -o file           导出 OpenAPI 文档
  gateway version                    打印版本`)
}

func serve(args []string, logger *log.Logger) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "", "监听地址(默认取 A_GATEWAY_HOST/A_GATEWAY_PORT)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg := config.Load()
	if *addr != "" {
		parts := strings.Split(*addr, ":")
		if len(parts) == 2 {
			cfg.Host = parts[0]
			fmt.Sscanf(parts[1], "%d", &cfg.Port)
		}
	}

	reg, err := registry.Build(cfg)
	if err != nil {
		return err
	}
	if path := registry.AliasesPath(cfg); path == "" {
		logger.Printf("warning: data/aliases.json not found; sources have no aliases")
	} else {
		logger.Printf("info: aliases loaded from %s", path)
	}
	logger.Printf("info: registry built: %d sources / %d families", reg.Count, len(reg.Families))

	respCache := cache.New(cfg.CacheEntries, cfg.CacheBytes)
	client := upstream.New(upstream.Options{
		PoolSize:       cfg.ConnectPool,
		VerifyTLS:      cfg.VerifyTLS,
		UserAgent:      cfg.UserAgent,
		DefaultHeaders: cfg.Headers(),
	})
	if !cfg.VerifyTLS {
		logger.Printf("warning: TLS verification is DISABLED (A_GATEWAY_VERIFY=none); never use this in production")
	}
	parserMgr := parser.NewManager(context.Background(), cfg.ParserMode, cfg.ParserPython,
		cfg.FindPythonSrcDir(), cfg.MaxParseRows, logger)
	met := metrics.New()
	limiter := ratelimit.New(cfg.ConnectPool)
	resolver := server.NewResolver(cfg, reg, respCache, client, parserMgr, met, limiter, logger)
	srv := server.New(cfg, resolver, met, logger)

	httpSrv := &http.Server{
		Addr:              cfg.Addr(),
		Handler:           server.RecoverMiddleware(logger, srv.Handler()),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		ErrorLog:          logger,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if cfg.Prewarm {
		logger.Printf("info: prewarm started in the background")
		resolver.Prewarm()
	}

	errCh := make(chan error, 1)
	go func() {
		logger.Printf("info: gateway listening on %s (budget %dms, upstream %dms, parser %s, offline %v)",
			cfg.Addr(), cfg.BudgetMS, cfg.UpstreamBudgetMS, parserMgr.Name(), cfg.Offline)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		logger.Printf("info: shutting down")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shutdownCtx)
	client.Close()
	_ = parserMgr.Close()
	return nil
}

func registryDump(args []string, logger *log.Logger) error {
	fs := flag.NewFlagSet("registry-dump", flag.ContinueOnError)
	out := fs.String("o", "registry.json", "输出文件")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg := config.Load()
	reg, err := registry.Build(cfg)
	if err != nil {
		return err
	}
	body, err := parser.MarshalIndent(reg.ToObject(), "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(*out, body, 0o644); err != nil {
		return err
	}
	endpoints := 0
	for _, s := range reg.Sources {
		endpoints += len(s.Endpoints)
	}
	st, _ := os.Stat(*out)
	size := 0.0
	if st != nil {
		size = float64(st.Size()) / 1024
	}
	// The summary goes to stdout, like the Python build-registry command.
	fmt.Printf("已写出 %s (%.1f KiB) | %d 源 / %d 端点\n", *out, size, reg.Count, endpoints)
	return nil
}

func openapiDump(args []string, logger *log.Logger) error {
	fs := flag.NewFlagSet("openapi", flag.ContinueOnError)
	out := fs.String("o", "openapi.json", "输出文件")
	if err := fs.Parse(args); err != nil {
		return err
	}
	body, err := parser.MarshalIndent(server.OpenAPIDocument(), "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(*out, body, 0o644); err != nil {
		return err
	}
	fmt.Printf("已写出 %s\n", *out)
	return nil
}
