// Command memosd 是 memory-console-be 唯一的可执行入口。
// 同一个二进制按角色启动（console / mcp / all），装配逻辑在 internal/app，这里只负责参数与退出码。
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/app"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/logging"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/version"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run 是可测试的入口：不读全局状态，不直接退出进程，输出走传入的 writer。
func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		usage(stderr)
		return 2
	}
	switch args[0] {
	case "version":
		fmt.Fprintln(stdout, version.String())
		return 0
	case "serve":
		return runServe(args[1:], stderr)
	case "chown":
		return runChown(args[1:], stderr)
	default:
		usage(stderr)
		return 2
	}
}

func usage(w io.Writer) {
	fmt.Fprintln(w, "usage: memory-console <version|serve|chown <dir>>")
}

// runChown 给 hostPath PVC 初始化属主：local-storage 的目录由 kubelet 以 root 创建，
// 容器以 65532 运行，没有这个步骤 SQLite 打不开。
//
//nolint:gosec // 路径来自部署清单的固定挂载点，不是用户输入。
func runChown(args []string, stderr io.Writer) int {
	if len(args) != 1 {
		fmt.Fprintln(stderr, "usage: memory-console chown <dir>")
		return 2
	}
	if err := filepath.WalkDir(args[0], func(path string, _ os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		return os.Lchown(path, 65532, 65532)
	}); err != nil {
		fmt.Fprintf(stderr, "memory-console: chown %s: %v\n", args[0], err)
		return 1
	}
	return 0
}

func runServe(args []string, stderr io.Writer) int {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	fs.SetOutput(stderr)
	roleFlag := fs.String("role", "", "console, mcp or all (overrides MEMOSD_ROLE)")
	consoleAddr := fs.String("console-addr", "", "console listen address (overrides MEMOSD_CONSOLE_ADDR)")
	mcpAddr := fs.String("mcp-addr", "", "mcp listen address (overrides MEMOSD_MCP_ADDR)")
	if err := fs.Parse(args); err != nil {
		return 2
	}

	cfg, err := config.Load()
	if err != nil {
		fmt.Fprintf(stderr, "memosd: invalid configuration: %v\n", err)
		return 2
	}
	if err := cfg.ApplyOverrides(*roleFlag, *consoleAddr, *mcpAddr); err != nil {
		fmt.Fprintf(stderr, "memosd: %v\n", err)
		return 2
	}
	if err := cfg.Validate(); err != nil {
		fmt.Fprintf(stderr, "memosd: invalid configuration: %v\n", err)
		return 2
	}

	level, err := logging.ParseLevel(cfg.Log.Level)
	if err != nil {
		fmt.Fprintf(stderr, "memosd: %v\n", err)
		return 2
	}
	// 指标先于日志器构造：日志落盘失败要能累加到 /metrics，否则磁盘满时审计留痕
	// 会无声中断。app.New 复用同一个 registry。
	m := app.NewMetrics(cfg)
	log, logCloser, err := logging.New(logging.Options{
		Level:      level,
		Format:     cfg.Log.Format,
		File:       cfg.Log.File,
		MaxSizeMB:  cfg.Log.MaxSizeMB,
		MaxBackups: cfg.Log.MaxBackups,
		MaxAgeDays: cfg.Log.MaxAgeDays,
		Compress:   cfg.Log.Compress,
		OnError: func(op string, _ error) {
			m.ObserveLogError(op)
		},
	})
	if err != nil {
		fmt.Fprintf(stderr, "memosd: %v\n", err)
		return 2
	}
	if logCloser != nil {
		defer func() { _ = logCloser.Close() }()
	}
	logging.SetDefault(log)
	log.Info("starting",
		slog.String("config", cfg.Redacted()),
		slog.String("version", version.String()),
	)
	if cfg.Log.File != "" {
		log.Info("log sink ready",
			slog.String("file", cfg.Log.File),
			slog.Int("max_size_mb", cfg.Log.MaxSizeMB),
			slog.Int("max_backups", cfg.Log.MaxBackups),
			slog.Int("max_age_days", cfg.Log.MaxAgeDays),
			slog.Bool("compress", cfg.Log.Compress),
		)
	}

	a, err := app.New(app.Options{Config: cfg, Logger: log, Metrics: m})
	if err != nil {
		fmt.Fprintf(stderr, "memosd: %v\n", err)
		return 2
	}
	defer func() {
		if err := a.Close(); err != nil {
			log.Error("close failed", slog.String("error", err.Error()))
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := a.Run(ctx); err != nil {
		log.Error("run failed", slog.String("error", err.Error()))
		return 1
	}
	return 0
}
