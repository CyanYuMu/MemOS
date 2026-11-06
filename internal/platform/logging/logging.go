package logging

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/natefinch/lumberjack.v2"
)

// Options 是日志器的构造参数，来自 config.LogConfig。
type Options struct {
	Level  slog.Level
	Format string    // "json"（默认）或 "text"
	Writer io.Writer // 为空时写 stdout
	// File 非空时同时写这个文件并按大小轮转；stdout 始终保留，kubectl logs 不受影响。
	File       string
	MaxSizeMB  int
	MaxBackups int
	MaxAgeDays int
	Compress   bool
	// OnError 在文件写/刷失败时回调（op = "write" | "flush"），用于累加指标。
	// 为空时失败仍会写 stderr，不会被静默吞掉。
	OnError func(op string, err error)
}

// LevelDebug、LevelInfo 等只是 slog 级别的可读别名，避免调用方反复 import slog。
const (
	LevelDebug = slog.LevelDebug
	LevelInfo  = slog.LevelInfo
	LevelWarn  = slog.LevelWarn
	LevelError = slog.LevelError
)

// ParseLevel 把配置里的级别字符串转成 slog.Level。
func ParseLevel(s string) (slog.Level, error) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return slog.LevelDebug, nil
	case "info", "":
		return slog.LevelInfo, nil
	case "warn":
		return slog.LevelWarn, nil
	case "error":
		return slog.LevelError, nil
	default:
		return slog.LevelInfo, fmt.Errorf("unknown log level %q", s)
	}
}

// New 构造日志器。未知格式退回 JSON；File 非空时同时落盘并轮转。
// 返回的 Closer 在进程退出前关闭，nil 表示只写 Writer。
func New(opts Options) (*slog.Logger, io.Closer, error) {
	w := opts.Writer
	if w == nil {
		w = os.Stdout
	}
	var closer io.Closer
	var flush func() error
	if opts.File != "" {
		dir := filepath.Dir(opts.File)
		if err := os.MkdirAll(dir, 0o750); err != nil {
			return nil, nil, fmt.Errorf("create log dir %s: %w", dir, err)
		}
		// lumberjack 懒打开文件：这里主动打开一次，让权限、只读挂载、磁盘满在启动期
		// 就暴露成启动失败，而不是等到第一条日志才发现（slog 会吞掉 handler 的写错误）。
		//nolint:gosec // 路径来自部署清单的固定挂载点，不是用户输入。
		f, err := os.OpenFile(opts.File, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
		if err != nil {
			return nil, nil, fmt.Errorf("open log file %s: %w", opts.File, err)
		}
		_ = f.Close()
		lj := &lumberjack.Logger{
			Filename:   opts.File,
			MaxSize:    opts.MaxSizeMB,
			MaxBackups: opts.MaxBackups,
			MaxAge:     opts.MaxAgeDays,
			Compress:   opts.Compress,
			LocalTime:  true,
		}
		// 落盘是同步 IO（PVC 上单次 0.1–2ms），再叠加 slog 的全局 handler 锁会成为
		// 所有请求的公共串行点。写内存缓冲、后台定期 Flush；Close 时写尽。
		aw := newAsyncWriter(io.MultiWriter(w, lj), opts.OnError)
		w = aw
		closer = closerGroup{aw, lj}
		flush = aw.flush
	}
	handlerOpts := &slog.HandlerOptions{Level: opts.Level}
	var base slog.Handler
	if opts.Format == "text" {
		base = slog.NewTextHandler(w, handlerOpts)
	} else {
		base = slog.NewJSONHandler(w, handlerOpts)
	}
	if flush != nil {
		// Warn 及以上立即刷盘：进程被 OOM Kill 时，最关键的记录不至于留在缓冲里。
		base = &flushHandler{next: base, flush: flush}
	}
	return slog.New(&contextHandler{next: base}), closer, nil
}

// SetDefault 设置进程默认日志器。
func SetDefault(l *slog.Logger) { slog.SetDefault(l) }

type requestIDKey struct{}

// WithRequestID 把请求 ID 放入 context，后续日志自动携带。
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

// RequestIDFrom 取 context 里的请求 ID，没有返回空串。
func RequestIDFrom(ctx context.Context) string {
	id, _ := ctx.Value(requestIDKey{}).(string)
	return id
}

type loggerKey struct{}

// WithContext 把日志器放入 context，供没有显式持有 logger 的代码取用。
func WithContext(ctx context.Context, l *slog.Logger) context.Context {
	return context.WithValue(ctx, loggerKey{}, l)
}

// FromContext 取 context 里的日志器，没有时返回 slog.Default()。
func FromContext(ctx context.Context) *slog.Logger {
	if l, ok := ctx.Value(loggerKey{}).(*slog.Logger); ok && l != nil {
		return l
	}
	return slog.Default()
}

// Redact 掩码一个凭据：长度 ≤ 12 时全掩码，否则保留前 4 与后 4。空串原样返回。
func Redact(s string) string {
	if s == "" {
		return ""
	}
	const keep = 4
	if len(s) <= keep*3 {
		return strings.Repeat("*", len(s))
	}
	return s[:keep] + "…" + s[len(s)-keep:]
}

// contextHandler 只负责把请求 ID 注入每条日志，不改写业务字段。
// 日志按运营要求保留原始值：不脱敏、不截断；凭据不得由业务代码主动记录。
type contextHandler struct {
	next slog.Handler
}

func (h *contextHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.next.Enabled(ctx, level)
}

func (h *contextHandler) Handle(ctx context.Context, r slog.Record) error {
	id := RequestIDFrom(ctx)
	if id == "" || recordHasKey(r, "request_id") {
		return h.next.Handle(ctx, r)
	}
	out := slog.NewRecord(r.Time, r.Level, r.Message, r.PC)
	r.Attrs(func(a slog.Attr) bool {
		out.AddAttrs(a)
		return true
	})
	out.AddAttrs(slog.String("request_id", id))
	return h.next.Handle(ctx, out)
}

func (h *contextHandler) WithAttrs(as []slog.Attr) slog.Handler {
	return &contextHandler{next: h.next.WithAttrs(as)}
}

func (h *contextHandler) WithGroup(name string) slog.Handler {
	return &contextHandler{next: h.next.WithGroup(name)}
}

func recordHasKey(r slog.Record, key string) bool {
	found := false
	r.Attrs(func(a slog.Attr) bool {
		if a.Key == key {
			found = true
			return false
		}
		return true
	})
	return found
}

// flushHandler 在 Warn 及以上级别写完立即刷盘，缩小崩溃时关键日志的丢失窗口。
// 刷新失败由 asyncWriter 统一上报（stderr + OnError），这里不重复处理。
type flushHandler struct {
	next  slog.Handler
	flush func() error
}

func (h *flushHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.next.Enabled(ctx, level)
}

func (h *flushHandler) Handle(ctx context.Context, r slog.Record) error {
	err := h.next.Handle(ctx, r)
	if r.Level >= slog.LevelWarn {
		_ = h.flush()
	}
	return err
}

func (h *flushHandler) WithAttrs(as []slog.Attr) slog.Handler {
	return &flushHandler{next: h.next.WithAttrs(as), flush: h.flush}
}

func (h *flushHandler) WithGroup(name string) slog.Handler {
	return &flushHandler{next: h.next.WithGroup(name), flush: h.flush}
}
