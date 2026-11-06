package httpx

import (
	"net/http"
	"time"
)

// ServerOptions 是入站服务器的时间参数。
type ServerOptions struct {
	ReadHeaderTimeout time.Duration
	ReadTimeout       time.Duration // 只覆盖请求体读取；不影响 SSE 响应
	IdleTimeout       time.Duration
	ShutdownTimeout   time.Duration
}

const defaultIdleTimeout = 120 * time.Second

// NewServer 构造显式超时的 http.Server。
//
// 不设 WriteTimeout：/mcp 的 SSE 长连接会被它截断。用 IdleTimeout 兜底。
func NewServer(addr string, h http.Handler, opts ServerOptions) *http.Server {
	idle := opts.IdleTimeout
	if idle <= 0 {
		idle = defaultIdleTimeout
	}
	return &http.Server{
		Addr:              addr,
		Handler:           h,
		ReadHeaderTimeout: opts.ReadHeaderTimeout,
		ReadTimeout:       opts.ReadTimeout,
		IdleTimeout:       idle,
	}
}
