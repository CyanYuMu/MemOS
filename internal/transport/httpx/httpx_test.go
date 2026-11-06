package httpx

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
}

func TestRequestID(t *testing.T) {
	t.Parallel()

	handler := RequestID(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		if RequestIDFrom(r.Context()) == "" {
			t.Error("request id missing from context")
		}
	}))

	rec := httptest.NewRecorder()
	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/", nil)
	req.Header.Set("X-Request-Id", "client-req-123")
	handler.ServeHTTP(rec, req)
	if got := rec.Header().Get("X-Request-Id"); got != "client-req-123" {
		t.Fatalf("request id = %q", got)
	}

	rec = httptest.NewRecorder()
	req = httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/", nil)
	req.Header.Set("X-Request-Id", "bad id with spaces")
	handler.ServeHTTP(rec, req)
	if got := rec.Header().Get("X-Request-Id"); got == "bad id with spaces" || got == "" {
		t.Fatalf("invalid id not replaced: %q", got)
	}
}

func TestClientIP(t *testing.T) {
	t.Parallel()

	handler := ClientIP(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		if got := ClientIPFrom(r.Context()); got != "203.0.113.7" {
			t.Errorf("client ip = %q", got)
		}
	}))

	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/", nil)
	req.RemoteAddr = "203.0.113.7:51234"
	// X-Forwarded-For 不参与：可信来源只有 TCP 对端。
	req.Header.Set("X-Forwarded-For", "198.51.100.1")
	handler.ServeHTTP(httptest.NewRecorder(), req)
}

func TestRecover(t *testing.T) {
	t.Parallel()

	h := Chain(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		panic("boom")
	}), Recover(discardLogger()))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/", nil))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d", rec.Code)
	}
	var env Envelope
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("body: %v", err)
	}
	if env.Code != CodeInternal || env.Data != nil {
		t.Fatalf("envelope = %+v", env)
	}
}

func TestDecodeJSON(t *testing.T) {
	t.Parallel()

	type payload struct {
		Name string `json:"name"`
	}

	tests := []struct {
		name    string
		body    string
		wantErr error
		want    string
	}{
		{name: "ok", body: `{"name":"记忆"}`, want: "记忆"},
		{name: "empty", body: "", want: ""},
		{name: "null", body: "null", want: ""},
		{name: "invalid", body: "{", wantErr: ErrBadJSON},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/", strings.NewReader(tt.body))
			var p payload
			err := DecodeJSON(req, &p)
			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("err = %v, want %v", err, tt.wantErr)
			}
			if p.Name != tt.want {
				t.Fatalf("name = %q, want %q", p.Name, tt.want)
			}
		})
	}
}

func TestMaxBody(t *testing.T) {
	t.Parallel()

	h := MaxBody(8)(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		var v map[string]any
		if err := DecodeJSON(r, &v); !errors.Is(err, ErrBodyTooLarge) {
			t.Errorf("err = %v, want ErrBodyTooLarge", err)
		}
	}))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/", strings.NewReader(`{"k":"0123456789"}`)))
}

func TestRequireJSON(t *testing.T) {
	t.Parallel()

	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "text/plain")
	if err := RequireJSON(req); !errors.Is(err, ErrNotJSON) {
		t.Fatalf("err = %v", err)
	}

	req = httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/", strings.NewReader(`{}`))
	req.Header.Set("Content-Type", "application/json")
	if err := RequireJSON(req); err != nil {
		t.Fatalf("err = %v", err)
	}

	req = httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/", nil)
	req.Header.Set("Content-Type", "text/plain")
	if err := RequireJSON(req); err != nil {
		t.Fatalf("bodyless request should pass: %v", err)
	}
}

func TestCallerContext(t *testing.T) {
	t.Parallel()

	ctx := WithCaller(context.Background(), domain.Caller{TenantID: "cube", UserID: "alice"})
	got, ok := CallerFrom(ctx)
	if !ok || got.TenantID != "cube" || got.UserID != "alice" {
		t.Fatalf("caller = %+v, %v", got, ok)
	}
}

func TestReadiness(t *testing.T) {
	t.Parallel()

	r := NewReadiness()
	r.Add("store", func(context.Context) error { return nil })
	h := r.Handler()

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("ready status = %d", rec.Code)
	}

	r.Add("upstream", func(context.Context) error { return errors.New("down") })
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("not ready status = %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "down") {
		t.Fatalf("readiness leaks error detail: %s", rec.Body.String())
	}
}

func TestCORSWhitelist(t *testing.T) {
	t.Parallel()

	h := CORS([]string{"http://localhost:5274"})(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		OK(w, nil)
	}))

	rec := httptest.NewRecorder()
	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/", nil)
	req.Header.Set("Origin", "http://localhost:5274")
	h.ServeHTTP(rec, req)
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "http://localhost:5274" {
		t.Fatalf("allow-origin = %q", got)
	}

	rec = httptest.NewRecorder()
	req = httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/", nil)
	req.Header.Set("Origin", "http://evil.example")
	h.ServeHTTP(rec, req)
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Fatalf("unexpected allow-origin for evil origin: %q", got)
	}

	rec = httptest.NewRecorder()
	req = httptest.NewRequestWithContext(context.Background(), http.MethodOptions, "/", nil)
	req.Header.Set("Origin", "http://localhost:5274")
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("preflight status = %d", rec.Code)
	}
}

func TestNotFoundJSON(t *testing.T) {
	t.Parallel()

	rec := httptest.NewRecorder()
	NotFoundJSON(rec)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); !strings.HasPrefix(got, "application/json") {
		t.Fatalf("content-type = %q", got)
	}
	var env Envelope
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if env.Code != CodeNotFound || env.Data != nil || env.Message != "接口不存在" {
		t.Fatalf("envelope = %+v", env)
	}
}

func TestSetRetryAfter(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name   string
		window time.Duration
		want   string
	}{
		{"zero", 0, ""},
		{"negative", -time.Second, ""},
		{"sub-second rounds up", 300 * time.Millisecond, "1"},
		{"ceil", 1500 * time.Millisecond, "2"},
		{"window", 10 * time.Minute, "600"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rec := httptest.NewRecorder()
			SetRetryAfter(rec, tc.window)
			if got := rec.Header().Get("Retry-After"); got != tc.want {
				t.Fatalf("Retry-After = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestMethodNotAllowedJSON(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	mux.HandleFunc("POST /only-post", func(w http.ResponseWriter, _ *http.Request) {
		OK(w, map[string]any{"ok": true})
	})
	h := MethodNotAllowedJSON(mux)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/only-post", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405", rec.Code)
	}
	if allow := rec.Header().Get("Allow"); allow != http.MethodPost {
		t.Fatalf("Allow = %q, want POST", allow)
	}
	var env Envelope
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode: %v (body=%q)", err, rec.Body.String())
	}
	if env.Code != CodeMethodNotAllowed || env.Data != nil || env.Message != "方法不允许" {
		t.Fatalf("envelope = %+v", env)
	}

	// 正常响应不能被包装器改写。
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/only-post", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("POST status = %d", rec.Code)
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil || env.Code != CodeOK {
		t.Fatalf("POST envelope = %+v err=%v", env, err)
	}
}
