package admin

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/jobs"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/tenant"
)

const adminToken = "memos_admin_test"

func TestAdminRequiresAdminToken(t *testing.T) {
	srv := newTestServer(t)
	cases := []struct {
		name  string
		token string
		want  int
	}{
		{"missing", "", http.StatusUnauthorized},
		{"tenant token", "memos_tenant_test", http.StatusUnauthorized},
	}
	for _, tc := range cases {
		req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/admin/tenants", nil)
		if tc.token != "" {
			req.Header.Set("Authorization", "Bearer "+tc.token)
		}
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, req)
		if rec.Code != tc.want {
			t.Fatalf("%s: status=%d body=%s", tc.name, rec.Code, rec.Body.String())
		}
	}
}

func TestTenantAndTokenLifecycle(t *testing.T) {
	srv := newTestServer(t)

	create := postJSON(t, srv, "/admin/tenants", `{"id":"cube-a","user_id":"alice","name":"A"}`)
	if create.Code != http.StatusOK {
		t.Fatalf("create tenant: %d %s", create.Code, create.Body.String())
	}

	list := doJSON(t, srv, http.MethodGet, "/admin/tenants", "")
	if !bytes.Contains(list.Body.Bytes(), []byte(`"id":"cube-a"`)) {
		t.Fatalf("tenant list: %s", list.Body.String())
	}

	issued := postJSON(t, srv, "/admin/tenants/cube-a/tokens", `{"label":"ci"}`)
	if issued.Code != http.StatusOK {
		t.Fatalf("issue token: %d %s", issued.Code, issued.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(issued.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode issue: %v", err)
	}
	data, _ := body["data"].(map[string]any)
	raw, _ := data["token"].(string)
	if raw == "" {
		t.Fatalf("签发响应缺少明文: %s", issued.Body.String())
	}
	tokenID, _ := data["id"].(string)

	tokens := doJSON(t, srv, http.MethodGet, "/admin/tenants/cube-a/tokens", "")
	if bytes.Contains(tokens.Body.Bytes(), []byte(raw)) || bytes.Contains(tokens.Body.Bytes(), []byte("hash")) {
		t.Fatalf("列表泄漏明文或哈希: %s", tokens.Body.String())
	}

	revoke := doJSON(t, srv, http.MethodPost, "/admin/tokens/"+tokenID+"/revoke", "")
	if revoke.Code != http.StatusOK {
		t.Fatalf("revoke: %d %s", revoke.Code, revoke.Body.String())
	}
}

func TestUpsertTenantInvalidatesAuthCache(t *testing.T) {
	called := 0
	srv, _ := newTestServerStore(t, func() { called++ })

	rec := postJSON(t, srv, "/admin/tenants", `{"id":"cube-b","user_id":"bob"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if called != 1 {
		t.Fatalf("租户启停后必须清空鉴权缓存，调用次数=%d", called)
	}
}

func TestInvalidTenantID(t *testing.T) {
	srv := newTestServer(t)
	rec := postJSON(t, srv, "/admin/tenants", `{"id":"bad id","user_id":"alice"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

// 回归：对已归档项目 upsert 不能悄悄把它恢复成可用。
func TestUpsertTenantKeepsArchivedState(t *testing.T) {
	srv, store := newTestServerStore(t, nil)
	archivedAt := time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC)
	store.tenants["cube-gone"] = domain.Tenant{
		ID: "cube-gone", UserID: "gone", Disabled: true, ArchivedAt: &archivedAt, CreatedAt: time.Now(),
	}

	rec := postJSON(t, srv, "/admin/tenants", `{"id":"cube-gone","user_id":"gone","name":"renamed"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	got := store.tenants["cube-gone"]
	if !got.Archived() || !got.Disabled || got.Name != "renamed" {
		t.Fatalf("归档状态被 upsert 改动: %+v", got)
	}
}

// 新建项目不能占用个人项目的保留前缀；已有的个人项目仍可被平台管理员 upsert（由 e2e 覆盖）。
func TestUpsertTenantRejectsReservedPrefixOnCreate(t *testing.T) {
	srv := newTestServer(t)
	rec := postJSON(t, srv, "/admin/tenants", `{"id":"u-anything","user_id":"anything"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestUpsertTenantRejectsDuplicateUserID(t *testing.T) {
	srv := newTestServer(t) // 已有 cube-a -> alice

	rec := postJSON(t, srv, "/admin/tenants", `{"id":"cube-b","user_id":"alice"}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("重复 user_id 应返回 409，got %d %s", rec.Code, rec.Body.String())
	}
	if !bytes.Contains(rec.Body.Bytes(), []byte("该上游 user_id 已被其他项目占用")) {
		t.Fatalf("冲突消息不可辨认: %s", rec.Body.String())
	}
}

func TestListJobs(t *testing.T) {
	srv, fs := newTestServerStore(t, nil)
	fs.jobs = []domain.Job{
		{
			ID: "job-fail", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusFailed,
			TenantID: "cube-a", UserID: "alice", Attempts: 3, LastError: "upstream unavailable",
			Payload: []byte(`{"text":"记忆正文"}`), CreatedAt: time.Now(),
		},
		{
			ID: "job-ok", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusSucceeded,
			TenantID: "cube-b", UserID: "bob", Attempts: 1, CreatedAt: time.Now(),
		},
	}

	rec := doJSON(t, srv, http.MethodGet, "/admin/jobs?status=failed", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !bytes.Contains([]byte(body), []byte(`"id":"job-fail"`)) {
		t.Fatalf("应返回死信: %s", body)
	}
	if bytes.Contains([]byte(body), []byte("job-ok")) || bytes.Contains([]byte(body), []byte("记忆正文")) {
		t.Fatalf("不应返回非目标状态或 payload: %s", body)
	}

	rec = doJSON(t, srv, http.MethodGet, "/admin/jobs?status=bogus", "")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非法 status 应返回 400，got %d %s", rec.Code, rec.Body.String())
	}

	// 租户 Token 不能访问运维查询。
	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/admin/jobs", nil)
	req.Header.Set("Authorization", "Bearer memos_tenant_test")
	rec = httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("租户 Token 应被拒绝，got %d", rec.Code)
	}
}

// 死信重放：只接受 failed，重放后 attempts 归零、立即可被 worker 认领。
func TestRetryFailedJob(t *testing.T) {
	srv, fs := newTestServerStore(t, nil)
	fs.jobs = []domain.Job{
		{
			// 上游明确拒绝：请求没被接受，重放不会写出第二条。
			ID: "job-fail", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusFailed,
			TenantID: "cube-a", UserID: "alice", Attempts: 3, LastError: "upstream rejected",
			CreatedAt: time.Now(),
		},
		{
			ID: "job-ok", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusSucceeded,
			TenantID: "cube-b", UserID: "bob", Attempts: 1, CreatedAt: time.Now(),
		},
	}

	rec := postJSON(t, srv, "/admin/jobs/job-fail/retry", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !bytes.Contains([]byte(body), []byte(`"status":"pending"`)) ||
		!bytes.Contains([]byte(body), []byte(`"attempts":0`)) {
		t.Fatalf("重放后应为 pending 且 attempts 归零: %s", body)
	}
	if fs.jobs[0].LastError != "" {
		t.Fatalf("重放应清掉 last_error: %q", fs.jobs[0].LastError)
	}

	// 已经不是 failed 了：再点一次是冲突，不是幂等成功（避免重复写）。
	rec = postJSON(t, srv, "/admin/jobs/job-fail/retry", "")
	if rec.Code != http.StatusConflict {
		t.Fatalf("非 failed 任务重放应 409，got %d %s", rec.Code, rec.Body.String())
	}
	// 已成功的任务同样不能重放。
	rec = postJSON(t, srv, "/admin/jobs/job-ok/retry", "")
	if rec.Code != http.StatusConflict {
		t.Fatalf("succeeded 任务重放应 409，got %d %s", rec.Code, rec.Body.String())
	}
	rec = postJSON(t, srv, "/admin/jobs/missing/retry", "")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("不存在的任务应 404，got %d %s", rec.Code, rec.Body.String())
	}

	// 上传可能已经写成功（upstream unavailable）：不 force 就拒绝，force 才重放。
	fs.jobs = append(fs.jobs, domain.Job{
		ID: "job-timeout", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusFailed,
		TenantID: "cube-a", UserID: "alice", Attempts: 3, LastError: "upstream unavailable",
		CreatedAt: time.Now(),
	})
	rec = postJSON(t, srv, "/admin/jobs/job-timeout/retry", "")
	if rec.Code != http.StatusConflict {
		t.Fatalf("可能已写入的死信不 force 应 409，got %d %s", rec.Code, rec.Body.String())
	}
	rec = postJSON(t, srv, "/admin/jobs/job-timeout/retry", `{"force":true}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("force 重放应 200，got %d %s", rec.Code, rec.Body.String())
	}

	// 租户 Token 不能做运维动作。
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/admin/jobs/job-ok/retry", nil)
	req.Header.Set("Authorization", "Bearer memos_tenant_test")
	rec = httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("租户 Token 应被拒绝，got %d", rec.Code)
	}
}

func newTestServer(t *testing.T) *Server {
	t.Helper()
	srv, _ := newTestServerStore(t, nil)
	return srv
}

// newTestServerStore 同时返回底层假仓储，invalidate 观察项目写入后的鉴权缓存失效。
func newTestServerStore(t *testing.T, invalidate func()) (*Server, *fakeStore) {
	t.Helper()
	store := newFakeStore()
	store.tenants["cube-a"] = domain.Tenant{ID: "cube-a", UserID: "alice", CreatedAt: time.Now()}
	raw := "memos_tenant_test"
	hash := domain.HashToken(raw)
	store.tokens[hash] = domain.Token{
		ID: "tenant-token", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: hash, CreatedAt: time.Now(),
	}
	ids := identity.New(store, nil, adminToken, slog.Default())
	if err := ids.Bootstrap(context.Background()); err != nil {
		t.Fatalf("bootstrap identity: %v", err)
	}
	srv := New(Deps{
		Tenants:  tenant.New(store, nil, invalidate),
		Jobs:     jobs.New(store),
		Identity: ids,
		Accounts: adminOnly{},
		Logger:   slog.Default(),
	})
	return srv, store
}

// adminOnly 只认管理 Token；账号与成员管理由 internal/app 的黑盒 e2e 覆盖。
type adminOnly struct{ Accounts }

func (adminOnly) Authenticate(_ context.Context, c domain.Credentials) (domain.Principal, error) {
	if c.Bearer != adminToken {
		return domain.Principal{}, domain.Wrap(domain.ErrUnauthorized, "authenticate", "登录已过期，请重新登录", nil)
	}
	return domain.Principal{Kind: domain.PrincipalAdminToken, Username: domain.AdminTokenUsername, PlatformAdmin: true}, nil
}

func postJSON(t *testing.T, srv *Server, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	return doJSON(t, srv, http.MethodPost, path, body)
}

func doJSON(t *testing.T, srv *Server, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	var reader *bytes.Buffer
	if body == "" {
		reader = bytes.NewBuffer(nil)
	} else {
		reader = bytes.NewBufferString(body)
	}
	req := httptest.NewRequestWithContext(context.Background(), method, path, reader)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("Authorization", "Bearer "+adminToken)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	return rec
}

type fakeStore struct {
	mu      sync.Mutex
	tenants map[string]domain.Tenant
	tokens  map[[32]byte]domain.Token
	jobs    []domain.Job
}

func newFakeStore() *fakeStore {
	return &fakeStore{tenants: map[string]domain.Tenant{}, tokens: map[[32]byte]domain.Token{}}
}

func (f *fakeStore) GetTenant(_ context.Context, id string) (domain.Tenant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	tenant, ok := f.tenants[id]
	if !ok {
		return domain.Tenant{}, domain.Wrap(domain.ErrNotFound, "get_tenant", "租户不存在", nil)
	}
	return tenant, nil
}

func (f *fakeStore) UpsertTenant(_ context.Context, t domain.Tenant) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	// 与真实 store 的部分唯一索引保持一致：user_id 映射上游 user_name，不能重复。
	for id := range f.tenants {
		if id != t.ID && f.tenants[id].UserID == t.UserID {
			return domain.Wrap(domain.ErrConflict, "upsert_tenant", "该上游 user_id 已被其他项目占用", nil)
		}
	}
	if existing, ok := f.tenants[t.ID]; ok {
		t.CreatedAt = existing.CreatedAt
	} else if t.CreatedAt.IsZero() {
		t.CreatedAt = time.Now()
	}
	if t.Archived() {
		t.Disabled = true
	}
	f.tenants[t.ID] = t
	return nil
}

// CreateTenantWithOwner、ArchiveTenant、RestoreTenant 只为满足 tenant.Store；admin 路由不走它们。
func (f *fakeStore) CreateTenantWithOwner(ctx context.Context, t domain.Tenant, _ string, _ domain.Role) error {
	return f.UpsertTenant(ctx, t)
}

func (f *fakeStore) ArchiveTenant(context.Context, string, time.Time) error { return nil }

func (f *fakeStore) RestoreTenant(context.Context, string) error { return nil }

func (f *fakeStore) EnsureTenant(_ context.Context, t domain.Tenant) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if existing, ok := f.tenants[t.ID]; ok {
		t.CreatedAt = existing.CreatedAt
		t.Disabled = existing.Disabled
	} else if t.CreatedAt.IsZero() {
		t.CreatedAt = time.Now()
	}
	f.tenants[t.ID] = t
	return nil
}

func (f *fakeStore) ListTenants(context.Context) ([]domain.Tenant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]domain.Tenant, 0, len(f.tenants))
	for id := range f.tenants {
		out = append(out, f.tenants[id])
	}
	return out, nil
}

func (f *fakeStore) ListJobs(_ context.Context, filter domain.JobFilter) ([]domain.Job, int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]domain.Job, 0, len(f.jobs))
	for i := range f.jobs {
		job := f.jobs[i]
		if filter.Status != "" && job.Status != filter.Status {
			continue
		}
		if filter.Kind != "" && job.Kind != filter.Kind {
			continue
		}
		if filter.TenantID != "" && job.TenantID != filter.TenantID {
			continue
		}
		job.Payload = nil // 与真实 store 一致：列表不返回 payload
		out = append(out, job)
	}
	return out, len(out), nil
}

func (f *fakeStore) GetJob(_ context.Context, id string) (domain.Job, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i := range f.jobs {
		if f.jobs[i].ID == id {
			return f.jobs[i], nil
		}
	}
	return domain.Job{}, domain.Wrap(domain.ErrNotFound, "get_job", "任务不存在", nil)
}

func (f *fakeStore) RequeueJob(_ context.Context, id string, now time.Time) (domain.Job, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i := range f.jobs {
		if f.jobs[i].ID != id {
			continue
		}
		if f.jobs[i].Status != domain.JobStatusFailed {
			return domain.Job{}, domain.Wrap(domain.ErrConflict, "requeue_job",
				"只有失败的任务可以重放", nil)
		}
		f.jobs[i].Status = domain.JobStatusPending
		f.jobs[i].Attempts = 0
		f.jobs[i].LastError = ""
		f.jobs[i].NextAttemptAt = now
		f.jobs[i].UpdatedAt = now
		return f.jobs[i], nil
	}
	return domain.Job{}, domain.Wrap(domain.ErrNotFound, "requeue_job", "任务不存在", nil)
}

func (f *fakeStore) UpsertToken(ctx context.Context, t domain.Token) error {
	return f.CreateToken(ctx, t)
}

func (f *fakeStore) CreateToken(_ context.Context, t domain.Token) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.tokens[t.Hash] = t
	return nil
}

func (f *fakeStore) TokenByHash(_ context.Context, hash [32]byte) (domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	token, ok := f.tokens[hash]
	if !ok {
		return domain.Token{}, domain.Wrap(domain.ErrNotFound, "token_by_hash", "", nil)
	}
	return token, nil
}

func (f *fakeStore) GetToken(_ context.Context, id string) (domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for hash := range f.tokens {
		if f.tokens[hash].ID == id {
			return f.tokens[hash], nil
		}
	}
	return domain.Token{}, domain.NotFound("get_token", "")
}

func (f *fakeStore) ListTokens(_ context.Context, tenantID string) ([]domain.Token, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]domain.Token, 0)
	for hash := range f.tokens {
		token := f.tokens[hash]
		if tenantID == "" || token.TenantID == tenantID {
			out = append(out, token)
		}
	}
	return out, nil
}

func (f *fakeStore) RevokeToken(_ context.Context, id string, at time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for hash := range f.tokens {
		if f.tokens[hash].ID == id {
			token := f.tokens[hash]
			token.RevokedAt = &at
			f.tokens[hash] = token
			return nil
		}
	}
	return domain.Wrap(domain.ErrNotFound, "revoke_token", "Token 不存在", nil)
}

func (f *fakeStore) TouchToken(context.Context, string, time.Time) error { return nil }

func (f *fakeStore) UpdateTokenLabel(context.Context, string, string) error { return nil }
