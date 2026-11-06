package httpx

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"regexp"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/logging"
)

const requestIDHeader = "X-Request-Id"

// 只接受可控字符集，避免把调用方输入原样写进日志与响应头。
var requestIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{8,128}$`)

type (
	requestIDKey struct{}
	callerKey    struct{}
	clientIPKey  struct{}
)

// WithRequestID 把请求 ID 放进 context。
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

// RequestIDFrom 取 context 里的请求 ID。
func RequestIDFrom(ctx context.Context) string {
	id, _ := ctx.Value(requestIDKey{}).(string)
	return id
}

// WithClientIP 把 TCP 对端地址放进 context。
func WithClientIP(ctx context.Context, ip string) context.Context {
	return context.WithValue(ctx, clientIPKey{}, ip)
}

// ClientIPFrom 取 context 里的 TCP 对端地址；没有时返回空串。
// MCP 这类拿不到 *http.Request 的 handler 从这里取来源 IP。
func ClientIPFrom(ctx context.Context) string {
	ip, _ := ctx.Value(clientIPKey{}).(string)
	return ip
}

// WithCaller 把已鉴权的调用方放进 context。
func WithCaller(ctx context.Context, c domain.Caller) context.Context {
	return context.WithValue(ctx, callerKey{}, c)
}

// CallerFrom 取 context 里的调用方。
func CallerFrom(ctx context.Context) (domain.Caller, bool) {
	c, ok := ctx.Value(callerKey{}).(domain.Caller)
	return c, ok
}

// RequestID 中间件：接受合法的 X-Request-Id，否则生成一个，并写回响应头。
func RequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get(requestIDHeader)
		if !requestIDPattern.MatchString(id) {
			id = newRequestID()
		}
		w.Header().Set(requestIDHeader, id)
		// 同时写入 platform/logging 的键：日志处理器只认它，两个键必须一起写，
		// 否则 AccessLog 与业务日志都拿不到 request_id。
		ctx := logging.WithRequestID(WithRequestID(r.Context(), id), id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func newRequestID() string {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return "unknown-request"
	}
	return hex.EncodeToString(buf)
}
