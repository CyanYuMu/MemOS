package app

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
)

func testConfig(t *testing.T, role config.Role) config.Config {
	t.Helper()
	return config.Config{
		ServiceName: "test",
		Role:        role,
		HTTP: config.HTTPConfig{
			ConsoleAddr:       "127.0.0.1:0",
			MCPAddr:           "127.0.0.1:0",
			ReadHeaderTimeout: time.Second,
			ShutdownTimeout:   time.Second,
			MaxBodyBytes:      1 << 20,
		},
		Upstream: config.UpstreamConfig{
			BaseURL:          "http://127.0.0.1:1",
			Timeout:          time.Second,
			MaxResponseBytes: 1 << 20,
		},
		Store: config.StoreConfig{Path: filepath.Join(t.TempDir(), "test.db")},
		Limits: config.LimitsConfig{
			GraphMaxNodes: 4000,
		},
		Audit:     config.AuditConfig{RetentionDays: 30},
		LocalSync: config.LocalSyncConfig{Enabled: true},
		Auth: config.AuthConfig{
			SessionTTL:         12 * time.Hour,
			SessionMaxLifetime: 30 * 24 * time.Hour,
		},
	}
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(&bytes.Buffer{}, nil))
}

func TestNewRoles(t *testing.T) {
	t.Parallel()

	tests := []struct {
		role config.Role
		want []string
	}{
		{config.RoleConsole, []string{"console"}},
		{config.RoleMCP, []string{"mcp"}},
		{config.RoleAll, []string{"console", "mcp"}},
	}
	for _, tt := range tests {
		t.Run(string(tt.role), func(t *testing.T) {
			t.Parallel()

			a, err := New(Options{Config: testConfig(t, tt.role), Logger: testLogger()})
			if err != nil {
				t.Fatalf("New: %v", err)
			}
			t.Cleanup(func() { _ = a.Close() })
			handlers := a.Handlers()
			if len(handlers) != len(tt.want) {
				t.Fatalf("handlers = %d, want %d", len(handlers), len(tt.want))
			}
			for _, name := range tt.want {
				if _, ok := handlers[name]; !ok {
					t.Fatalf("missing handler %q", name)
				}
			}
		})
	}
}

func TestMetricsEndpoint(t *testing.T) {
	t.Parallel()

	cfg := testConfig(t, config.RoleAll)
	cfg.HTTP.MetricsAddr = "127.0.0.1:0"
	a, err := New(Options{Config: cfg, Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = a.Close() })

	handlers := a.Handlers()
	metricsHandler, ok := handlers["metrics"]
	if !ok {
		t.Fatalf("missing metrics handler: %+v", handlers)
	}
	handlers["console"].ServeHTTP(httptest.NewRecorder(), httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/healthz", nil))

	rec := httptest.NewRecorder()
	metricsHandler.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	for _, want := range []string{`memosd_build_info`, `route="/healthz"`, `role="console"`} {
		if !bytes.Contains(rec.Body.Bytes(), []byte(want)) {
			t.Errorf("missing %q in metrics output", want)
		}
	}
}

// goodContract 是上游契约 v2 的自检响应；/readyz 拿它决定是否放流量。
const goodContract = `{"contract_version":2,"time":{"basis":"utc"},"sources":{"default":"omitted"},"top_k_max":25}`

// fakeUpstream 起一个只实现 /product/contract 的上游，返回可拨号的 base URL。
func fakeUpstream(t *testing.T, contract string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/contract" {
			http.NotFound(w, r)
			return
		}
		_, _ = io.WriteString(w, contract)
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

func TestHealthEndpoints(t *testing.T) {
	t.Parallel()

	cfg := testConfig(t, config.RoleAll)
	cfg.Upstream.BaseURL = fakeUpstream(t, goodContract)
	a, err := New(Options{Config: cfg, Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = a.Close() })
	for name, h := range a.Handlers() {
		for _, path := range []string{"/healthz", "/readyz"} {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, path, nil))
			if rec.Code != http.StatusOK {
				t.Errorf("%s %s = %d", name, path, rec.Code)
			}
			if rec.Header().Get("X-Request-Id") == "" {
				t.Errorf("%s %s missing request id", name, path)
			}
		}
	}
}

// TestReadinessRequiresKnownContract 确认上游契约不匹配时 /readyz 失败：
// 用旧镜像的上游只会让请求按旧口径静默算错，不如不放流量。
func TestReadinessRequiresKnownContract(t *testing.T) {
	t.Parallel()

	cfg := testConfig(t, config.RoleAll)
	cfg.Upstream.BaseURL = fakeUpstream(t, `{"contract_version":1,"time":{"basis":"utc"},"sources":{"default":"omitted"},"top_k_max":25}`)
	a, err := New(Options{Config: cfg, Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = a.Close() })

	rec := httptest.NewRecorder()
	a.Handlers()["console"].ServeHTTP(rec,
		httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/readyz", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rec.Code)
	}
	if !bytes.Contains(rec.Body.Bytes(), []byte(`"upstream":"failed"`)) {
		t.Fatalf("body = %s", rec.Body.String())
	}
}

// TestBootstrapRotatesStaticTokens 验证真实 SQLite 上重启轮换：
// 配置里换掉的 bootstrap Token 立即失效，新 Token 生效，移除配置后全部失效。
func TestBootstrapRotatesStaticTokens(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "rotate.db")
	open := func() *store.Store {
		t.Helper()
		db, err := store.Open(ctx, path)
		if err != nil {
			t.Fatalf("open store: %v", err)
		}
		return db
	}
	bootstrap := func(db *store.Store, tenantToken, adminToken, userID string) *identity.Service {
		t.Helper()
		var static []identity.StaticTenant
		if tenantToken != "" {
			static = append(static, identity.StaticTenant{Token: tenantToken, UserID: userID, CubeID: "cube-a"})
		}
		ids := identity.New(db, static, adminToken, testLogger())
		if err := ids.Bootstrap(ctx); err != nil {
			t.Fatalf("bootstrap: %v", err)
		}
		return ids
	}
	authTenant := func(ids *identity.Service, token string) error {
		t.Helper()
		_, err := ids.Authenticate(ctx, token)
		return err
	}
	authAdmin := func(ids *identity.Service, token string) error {
		t.Helper()
		_, err := ids.AuthenticateAdmin(ctx, token)
		return err
	}

	db := open()
	ids := bootstrap(db, "tenant-token-old", "admin-token-old!", "alice")
	if err := authTenant(ids, "tenant-token-old"); err != nil {
		t.Fatalf("旧租户 token 应有效: %v", err)
	}
	if err := authAdmin(ids, "admin-token-old!"); err != nil {
		t.Fatalf("旧管理 token 应有效: %v", err)
	}
	if err := db.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	// 模拟带新配置重启：旧 bootstrap 凭据必须在启动时被吊销。
	db = open()
	t.Cleanup(func() { _ = db.Close() })
	ids = bootstrap(db, "tenant-token-new", "admin-token-new!", "alice")
	if err := authTenant(ids, "tenant-token-old"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("旧租户 token 仍可用: %v", err)
	}
	if err := authAdmin(ids, "admin-token-old!"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("旧管理 token 仍可用: %v", err)
	}
	if err := authTenant(ids, "tenant-token-new"); err != nil {
		t.Fatalf("新租户 token 应有效: %v", err)
	}
	if err := authAdmin(ids, "admin-token-new!"); err != nil {
		t.Fatalf("新管理 token 应有效: %v", err)
	}

	// 从配置里彻底移除：对应 bootstrap 凭据也要失效。
	ids = bootstrap(db, "", "", "alice")
	if err := authTenant(ids, "tenant-token-new"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("移除配置后租户 token 仍可用: %v", err)
	}
	if err := authAdmin(ids, "admin-token-new!"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("移除配置后管理 token 仍可用: %v", err)
	}

	// 重新把旧 Token 写回配置也不能恢复：吊销状态跨重启保留，必须轮换新 Token。
	ids = bootstrap(db, "tenant-token-old", "admin-token-old!", "alice")
	if err := authTenant(ids, "tenant-token-old"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("恢复旧配置不应复活已吊销 token: %v", err)
	}
	if err := authAdmin(ids, "admin-token-old!"); !errors.Is(err, domain.ErrUnauthorized) {
		t.Fatalf("恢复旧配置不应复活已吊销管理 token: %v", err)
	}

	// 换新 Token 并改绑用户：必须同步覆盖，不能继续用旧身份。
	ids = bootstrap(db, "tenant-token-third", "admin-token-third!", "bob")
	caller, err := ids.Authenticate(ctx, "tenant-token-third")
	if err != nil {
		t.Fatalf("改绑后认证失败: %v", err)
	}
	if caller.UserID != "bob" {
		t.Fatalf("改绑未生效，user = %q", caller.UserID)
	}
}

func TestRunReturnsListenError(t *testing.T) {
	t.Parallel()

	ln, err := (&net.ListenConfig{}).Listen(context.Background(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = ln.Close() }()

	cfg := testConfig(t, config.RoleConsole)
	cfg.HTTP.ConsoleAddr = ln.Addr().String()
	a, err := New(Options{Config: cfg, Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = a.Close() })
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() { done <- a.Run(ctx) }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("端口被占用时 Run 必须返回错误")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run 未返回监听错误")
	}
}

func TestRunStopsOnContext(t *testing.T) {
	t.Parallel()

	a, err := New(Options{Config: testConfig(t, config.RoleAll), Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = a.Close() })
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	done := make(chan error, 1)
	go func() { done <- a.Run(ctx) }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not stop")
	}
}

// TestUnknownRouteReturnsJSON404 钉住四个角色 mux 与 MCP 外层 mux 的兜底行为。
func TestUnknownRouteReturnsJSON404(t *testing.T) {
	t.Parallel()

	a, err := New(Options{Config: testConfig(t, config.RoleAll), Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(func() { _ = a.Close() })

	handlers := a.Handlers()
	cases := []struct {
		name   string
		role   string
		method string
		path   string
	}{
		{"dashboard", "console", http.MethodPost, "/api/dashboard/no-such-route"},
		{"admin", "console", http.MethodGet, "/admin/no-such-route"},
		{"auth", "console", http.MethodGet, "/api/auth/no-such-route"},
		{"mcp outer mux", "mcp", http.MethodGet, "/no-such-route"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rec := httptest.NewRecorder()
			handlers[tc.role].ServeHTTP(rec,
				httptest.NewRequestWithContext(context.Background(), tc.method, tc.path, nil))
			if rec.Code != http.StatusNotFound {
				t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
			}
			if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
				t.Fatalf("content-type = %q", ct)
			}
			var env struct {
				Code    int    `json:"code"`
				Data    any    `json:"data"`
				Message string `json:"message"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if env.Code != 40001 || env.Data != nil || env.Message != "接口不存在" {
				t.Fatalf("envelope = %+v", env)
			}
		})
	}

	// 健康检查是更具体的 pattern，不能被 "/" 兜底拦截。
	rec := httptest.NewRecorder()
	handlers["console"].ServeHTTP(rec,
		httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz status = %d", rec.Code)
	}
}

func TestPprofHandler(t *testing.T) {
	t.Parallel()

	rec := httptest.NewRecorder()
	pprofHandler().ServeHTTP(rec, httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/debug/pprof/", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("pprof index = %d", rec.Code)
	}
	if !bytes.Contains(rec.Body.Bytes(), []byte("goroutine")) {
		t.Fatalf("pprof index body = %s", rec.Body.String())
	}
}
