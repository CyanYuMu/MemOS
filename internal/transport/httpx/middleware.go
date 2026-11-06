package httpx

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"slices"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/metrics"
)

// Chain 按注册顺序组合中间件：先注册的在外层。
func Chain(h http.Handler, mws ...func(http.Handler) http.Handler) http.Handler {
	for _, mw := range slices.Backward(mws) {
		h = mw(h)
	}
	return h
}

// Recover 捕获 handler 的 panic，记日志并返回统一错误信封。
func Recover(log *slog.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func(ctx context.Context) {
				if rec := recover(); rec != nil {
					log.ErrorContext(ctx, "panic recovered",
						slog.Any("panic", rec),
						slog.String("method", r.Method),
						slog.String("path", redactPath(r.URL.Path)),
					)
					Fail(w, http.StatusInternalServerError, CodeInternal, "内部错误")
				}
			}(r.Context())
			next.ServeHTTP(w, r)
		})
	}
}

// Observer 记录入站请求指标；由 internal/platform/metrics 实现。nil 表示不记录。
type Observer interface {
	ObserveHTTP(in metrics.HTTPObservation)
}

// AccessLogOptions 是访问日志与请求指标的参数。
type AccessLogOptions struct {
	Role    string
	Metrics Observer
}

// AccessLog 记录方法、路径、状态、耗时与身份，不记录 query、header 与 body。
func AccessLog(log *slog.Logger, opts AccessLogOptions) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			started := time.Now()
			rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
			next.ServeHTTP(rec, r)

			attrs := []slog.Attr{
				slog.String("method", r.Method),
				slog.String("path", redactPath(r.URL.Path)),
				slog.Int("status", rec.status),
				slog.Int64("duration_ms", time.Since(started).Milliseconds()),
				slog.String("remote_ip", remoteIP(r)),
			}
			if id := RequestIDFrom(r.Context()); id != "" {
				attrs = append(attrs, slog.String("request_id", id))
			}
			if c, ok := CallerFrom(r.Context()); ok {
				attrs = append(attrs, slog.String("tenant_id", c.TenantID))
			}
			log.LogAttrs(r.Context(), slog.LevelInfo, "http request", attrs...)

			method, route := routeLabel(r)
			if opts.Metrics != nil {
				opts.Metrics.ObserveHTTP(metrics.HTTPObservation{
					Role:     opts.Role,
					Method:   method,
					Route:    route,
					Status:   rec.status,
					Duration: time.Since(started),
				})
			}
		})
	}
}

// redactPath 把邀请 token 从日志与 panic 输出里抹掉：/__invite/<token> → /__invite/***。
func redactPath(path string) string {
	const prefix = "/__invite/"
	if strings.HasPrefix(path, prefix) && len(path) > len(prefix) {
		return prefix + "***"
	}
	return path
}

// routeLabel 优先用 ServeMux 匹配到的路由模板（低基数），
// 未命中路由（404、静态资源回退等）统一归到 other，避免原始路径撑爆标签。
func routeLabel(r *http.Request) (method, route string) {
	pattern := r.Pattern
	if pattern == "" {
		return r.Method, "other"
	}
	if method, route, ok := strings.Cut(pattern, " "); ok {
		return method, route
	}
	return r.Method, pattern
}

// ClientIP 把 TCP 对端地址放进 context：MCP 的 handler 只有 ctx 没有 *http.Request，
// 审计要在这里取来源 IP。与访问日志同一口径，不解析 X-Forwarded-For。
func ClientIP(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r.WithContext(WithClientIP(r.Context(), remoteIP(r))))
	})
}

// remoteIP 只取 TCP 对端地址，不信任 X-Forwarded-For（可能被调用方伪造）。
func remoteIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// MaxBody 限制请求体大小。超限时 DecodeJSON 返回 ErrBodyTooLarge。
func MaxBody(limit int64) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Body != nil {
				r.Body = http.MaxBytesReader(w, r.Body, limit)
			}
			next.ServeHTTP(w, r)
		})
	}
}

// CORS 只对白名单来源回显 CORS 头，绝不通配。预检请求直接 204。
func CORS(origins []string) func(http.Handler) http.Handler {
	allowed := make(map[string]struct{}, len(origins))
	for _, o := range origins {
		if o != "" && o != "*" {
			allowed[o] = struct{}{}
		}
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			if _, ok := allowed[origin]; ok && origin != "" {
				h := w.Header()
				h.Set("Access-Control-Allow-Origin", origin)
				h.Add("Vary", "Origin")
				h.Set("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
				h.Set("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Request-Id")
				h.Set("Access-Control-Max-Age", "600")
			}
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// statusRecorder 记录响应状态码，并保留 Flusher 能力（/mcp 的 SSE 需要）。
type statusRecorder struct {
	http.ResponseWriter
	status      int
	wroteHeader bool
}

func (r *statusRecorder) WriteHeader(status int) {
	if r.wroteHeader {
		return
	}
	r.wroteHeader = true
	r.status = status
	r.ResponseWriter.WriteHeader(status)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if !r.wroteHeader {
		r.WriteHeader(http.StatusOK)
	}
	return r.ResponseWriter.Write(b)
}

func (r *statusRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Unwrap 让 http.ResponseController 能取到原始 writer。
func (r *statusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

// IsBodyTooLarge 判断解码错误是否由体积上限导致。
func IsBodyTooLarge(err error) bool {
	var tooLarge *http.MaxBytesError
	return errors.As(err, &tooLarge)
}
