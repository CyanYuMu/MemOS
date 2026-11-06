package app

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
)

const (
	e2eAdminToken = "memos_e2e_admin_token_0001"
	e2eSeedUser   = "root"
	e2eSeedPass   = "seed-pass-e2e-1"
)

// e2eEnv 是真实 SQLite + 真实 HTTP 的控制台实例，不 mock 存储与身份。
type e2eEnv struct {
	t   *testing.T
	app *App
	srv *httptest.Server
}

func newE2E(t *testing.T) *e2eEnv {
	t.Helper()
	return newE2EConfig(t, nil)
}

// newE2EWithAuth 用真实 SQLite 与真实 HTTP 构造控制台，允许定制 Auth 配置。
func newE2EWithAuth(t *testing.T, auth config.AuthConfig) *e2eEnv {
	t.Helper()
	return newE2EConfig(t, func(cfg *config.Config) {
		cfg.Auth = auth
	})
}

// newE2EConfig 用真实 SQLite 与真实 HTTP 构造控制台；默认开启口令登录，
// mutate 可覆盖上游地址、认证等配置。
func newE2EConfig(t *testing.T, mutate func(*config.Config)) *e2eEnv {
	t.Helper()
	cfg := testConfig(t, config.RoleConsole)
	cfg.Identity = config.IdentityConfig{
		AdminToken: e2eAdminToken,
		TenantTokens: []config.TenantToken{
			{Token: "memos_static_a_0000000001", UserID: "user-a", CubeID: "cube-a"},
			{Token: "memos_static_b_0000000001", UserID: "user-b", CubeID: "cube-b"},
		},
	}
	cfg.Auth = config.AuthConfig{
		SeedUser:           e2eSeedUser,
		SeedPassword:       e2eSeedPass,
		SessionTTL:         12 * time.Hour,
		SessionMaxLifetime: 30 * 24 * time.Hour,
		AllowPassword:      true,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	a, err := New(Options{Config: cfg, Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	srv := httptest.NewServer(a.Handlers()["console"])
	t.Cleanup(func() {
		srv.Close()
		_ = a.Close()
	})
	return &e2eEnv{t: t, app: a, srv: srv}
}

// e2eClient 是一个带独立 cookie jar 的浏览器会话；不自动跟随重定向。
type e2eClient struct {
	t       *testing.T
	base    string
	bearer  string
	headers map[string]string
	http    *http.Client
}

func (e *e2eEnv) client() *e2eClient {
	jar, err := cookiejar.New(nil)
	if err != nil {
		e.t.Fatalf("cookie jar: %v", err)
	}
	return &e2eClient{
		t:    e.t,
		base: e.srv.URL,
		http: &http.Client{
			Jar:           jar,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

func (e *e2eEnv) adminClient() *e2eClient {
	c := e.client()
	c.bearer = e2eAdminToken
	return c
}

// result 是一次调用的响应：状态码与解码后的信封。
type result struct {
	status int
	header http.Header
	code   int
	data   any
	msg    string
	raw    string
}

func (c *e2eClient) do(method, path, contentType, body string) result {
	c.t.Helper()
	var reader io.Reader
	if body != "" {
		reader = bytes.NewBufferString(body)
	}
	req, err := http.NewRequestWithContext(context.Background(), method, c.base+path, reader)
	if err != nil {
		c.t.Fatalf("new request: %v", err)
	}
	if body != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if c.bearer != "" {
		req.Header.Set("Authorization", "Bearer "+c.bearer)
	}
	for k, v := range c.headers {
		req.Header.Set(k, v)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		c.t.Fatalf("%s %s: %v", method, path, err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		c.t.Fatalf("read body: %v", err)
	}
	out := result{status: resp.StatusCode, header: resp.Header, raw: string(raw)}
	var env struct {
		Code    int    `json:"code"`
		Data    any    `json:"data"`
		Message string `json:"message"`
	}
	if json.Unmarshal(raw, &env) == nil {
		out.code, out.data, out.msg = env.Code, env.Data, env.Message
	}
	return out
}

func (c *e2eClient) get(path string) result { return c.do(http.MethodGet, path, "", "") }

func (c *e2eClient) post(path, body string) result {
	return c.do(http.MethodPost, path, "application/json", body)
}

// login 走 JSON 登录；成功后 cookie 保存在 jar 里。
func (c *e2eClient) login(username, password string) result {
	c.t.Helper()
	body, err := json.Marshal(map[string]string{"username": username, "password": password})
	if err != nil {
		c.t.Fatalf("marshal: %v", err)
	}
	return c.post("/__login", string(body))
}

func (r result) obj() map[string]any {
	m, _ := r.data.(map[string]any)
	return m
}

func (r result) str(key string) string {
	s, _ := r.obj()[key].(string)
	return s
}

func (r result) records() []map[string]any {
	items, _ := r.obj()["records"].([]any)
	out := make([]map[string]any, 0, len(items))
	for _, item := range items {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func (r result) list() []map[string]any {
	items, _ := r.data.([]any)
	out := make([]map[string]any, 0, len(items))
	for _, item := range items {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func expect(t *testing.T, label string, got result, status int) {
	t.Helper()
	if got.status != status {
		t.Fatalf("%s: status=%d want %d body=%s", label, got.status, status, got.raw)
	}
}
