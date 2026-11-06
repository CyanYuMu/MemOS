package app

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// 未配置 WebDir 时（只提供 API 的部署），非 API 的未知路径也要回 JSON 信封，
// 不能落到 Go 默认的 text/plain 404；/healthz 这类具体路由不受兜底影响。
func TestConsoleHandlerWithoutWebReturnsJSON404(t *testing.T) {
	t.Parallel()

	a := &App{
		cfg:   config.Config{HTTP: config.HTTPConfig{MaxBodyBytes: 1 << 20}},
		log:   slog.New(slog.DiscardHandler),
		ready: httpx.NewReadiness(),
	}
	h := a.consoleHandler()

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/no-such-page", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("404 body 不是 JSON: %v (%s)", err, rec.Body.String())
	}
	if body["code"] != float64(40001) || body["message"] != "接口不存在" {
		t.Fatalf("404 body = %v", body)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("/healthz status=%d body=%s", rec.Code, rec.Body.String())
	}
}
