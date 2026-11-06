package rest

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
)

const testToken = "memos_rest_test"

func TestRoutes(t *testing.T) {
	srv := newTestServer(t, &fakeUpstream{}, nil)
	cases := []struct {
		path string
		body string
		want string
	}{
		{"/memory/add", `{"messages":[{"role":"user","content":"hi"}],"tags":["t"]}`, `"status":"success"`},
		{"/memory/search", `{"query":"hi","top_k":"20"}`, `"memory_detail_list"`},
		{"/memory/delete", `{"memory_ids":["mem-1"]}`, `"deleted_ids":["mem-1"]`},
		{"/memory/feedback", `{"feedback_content":"prefer tea"}`, `"status":"success"`},
		{"/memory/profile", `{"include_preference":"true","size":"5"}`, `"total":1`},
	}
	for _, tc := range cases {
		req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, tc.path, bytes.NewBufferString(tc.body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+testToken)
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status=%d body=%s", tc.path, rec.Code, rec.Body.String())
		}
		if !bytes.Contains(rec.Body.Bytes(), []byte(tc.want)) {
			t.Fatalf("%s: body=%s 缺少 %s", tc.path, rec.Body.String(), tc.want)
		}
	}
}

func TestDeleteOwnershipSemantics(t *testing.T) {
	srv := newTestServer(t, &fakeUpstream{owners: map[string]string{"foreign": "bob"}}, nil)
	do := func(body string) *httptest.ResponseRecorder {
		req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/delete", bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+testToken)
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, req)
		return rec
	}
	if rec := do(`{"memory_ids":["foreign"]}`); rec.Code != http.StatusForbidden {
		t.Fatalf("foreign delete = %d %s", rec.Code, rec.Body.String())
	}
	rec := do(`{"memory_ids":["missing"]}`)
	if rec.Code != http.StatusOK || !bytes.Contains(rec.Body.Bytes(), []byte(`"not_found_ids":["missing"]`)) {
		t.Fatalf("missing delete = %d %s", rec.Code, rec.Body.String())
	}
}

func TestRequestGuards(t *testing.T) {
	srv := newTestServer(t, &fakeUpstream{}, nil)
	cases := []struct {
		name        string
		method      string
		contentType string
		token       string
		body        string
		want        int
	}{
		{"no token", http.MethodPost, "application/json", "", `{"query":"hi"}`, http.StatusUnauthorized},
		{"bad json", http.MethodPost, "application/json", testToken, `{`, http.StatusBadRequest},
		{"not json", http.MethodPost, "text/plain", testToken, `{"query":"hi"}`, http.StatusUnsupportedMediaType},
		{"wrong method", http.MethodGet, "", testToken, "", http.StatusMethodNotAllowed},
		{"empty query", http.MethodPost, "application/json", testToken, `{"query":"  "}`, http.StatusBadRequest},
	}
	for _, tc := range cases {
		var body *bytes.Buffer
		if tc.body != "" {
			body = bytes.NewBufferString(tc.body)
		} else {
			body = bytes.NewBuffer(nil)
		}
		req := httptest.NewRequestWithContext(context.Background(), tc.method, "/memory/search", body)
		if tc.contentType != "" {
			req.Header.Set("Content-Type", tc.contentType)
		}
		if tc.token != "" {
			req.Header.Set("Authorization", "Bearer "+tc.token)
		}
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, req)
		if rec.Code != tc.want {
			t.Fatalf("%s: status=%d want=%d body=%s", tc.name, rec.Code, tc.want, rec.Body.String())
		}
	}
}

// 未知路径必须和 REST 其它错误同形（{status,message}），不能混入 Dashboard 的
// {code,data,message} 信封，否则同一客户端要按路径分支解析响应。
func TestUnknownPathUsesRESTErrorShape(t *testing.T) {
	srv := newTestServer(t, &fakeUpstream{}, nil)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/nope", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("404 body 不是 JSON: %v (%s)", err, rec.Body.String())
	}
	if body["status"] != "error" || body["message"] != "接口不存在" {
		t.Fatalf("404 body = %v", body)
	}
	if _, ok := body["code"]; ok {
		t.Fatalf("REST 404 不应带 Dashboard 的 code 字段: %s", rec.Body.String())
	}
}

func TestNotFoundMapsTo404(t *testing.T) {
	t.Parallel()

	status, _ := errorStatus(domain.NotFound("memory", "记忆不存在"))
	if status != http.StatusNotFound {
		t.Fatalf("not found status = %d, want 404", status)
	}
}

func TestUpstreamErrorMapping(t *testing.T) {
	up := &fakeUpstream{searchErr: domain.Wrap(domain.ErrUpstreamUnavailable, "search", "上游不可用", nil)}
	srv := newTestServer(t, up, nil)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/search", bytes.NewBufferString(`{"query":"hi"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestAuditRecorded(t *testing.T) {
	store := &fakeAuditStore{}
	recorder, err := audit.New(store, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = recorder.Run(ctx) }()

	srv := newTestServer(t, &fakeUpstream{}, recorder)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/add", bytes.NewBufferString(`{"messages":[{"role":"user","content":"hi"}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	cancel()
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := recorder.Stop(stopCtx); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}
	records := store.records()
	if len(records) != 1 {
		t.Fatalf("审计记录数=%d", len(records))
	}
	got := records[0]
	if got.Tool != "add_message" || got.TenantID != "cube-a" || got.UserID != "alice" || !got.OK {
		t.Fatalf("审计字段不符: %+v", got)
	}
	// 这个用例的 service 没有 job 表，异步写入会退回同步：请求行仍带 call_id，
	// 但不 deferred。异步路径见 TestAddIsAsyncAndDoesNotWaitForUpstream。
	if got.CallID == "" || got.Deferred {
		t.Fatalf("同步 add 的审计行 call_id=%q deferred=%v", got.CallID, got.Deferred)
	}
	// 同一个 add_message 既可能来自 MCP 也可能来自这里，审计必须能分辨入口：
	// MCP 是异步入队，这条是同步等上游改写，两者的“谁在压上游”完全不同。
	if got.Source != domain.AuditSourceREST {
		t.Fatalf("source=%q want %q", got.Source, domain.AuditSourceREST)
	}
	if got.CaseLike == nil || *got.CaseLike {
		t.Fatalf("普通内容不应标记 case_like: %+v", got.CaseLike)
	}
}

// 审计请求行的 call_id 以 service 返回值为准：幂等重放时它指向首次任务，
// 否则重放请求行会带着一个没有终态行的新 call_id。
func TestAuditUsesResultCallID(t *testing.T) {
	store := &fakeAuditStore{}
	recorder, err := audit.New(store, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = recorder.Run(ctx) }()

	srv := newTestServer(t, &fakeUpstream{callID: "call-from-service"}, recorder)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/add", bytes.NewBufferString(`{"messages":[{"role":"user","content":"hi"}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	cancel()
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := recorder.Stop(stopCtx); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}
	records := store.records()
	if len(records) != 1 {
		t.Fatalf("审计记录数=%d", len(records))
	}
	if records[0].CallID != "call-from-service" {
		t.Fatalf("call_id=%q want call-from-service（应覆盖为 service 返回值）", records[0].CallID)
	}
}

// TestUnauthenticatedNotAudited 未认证请求不写审计，也不解析请求体。
func TestUnauthenticatedNotAudited(t *testing.T) {
	store := &fakeAuditStore{}
	recorder, err := audit.New(store, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() { _ = recorder.Run(ctx) }()

	srv := newTestServer(t, &fakeUpstream{}, recorder)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/add",
		bytes.NewBufferString(`{"messages":[{"role":"user","content":"hi"}]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	cancel()
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := recorder.Stop(stopCtx); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}
	if n := len(store.records()); n != 0 {
		t.Fatalf("未认证请求不应写审计，实际 %d 条", n)
	}
}

func newTestServer(t *testing.T, up *fakeUpstream, recorder *audit.Recorder) *Server {
	t.Helper()
	store := &fakeIdentityStore{
		tenants: map[string]domain.Tenant{"cube-a": {ID: "cube-a", UserID: "alice"}},
		tokens:  map[[32]byte]domain.Token{},
	}
	hash := domain.HashToken(testToken)
	store.tokens[hash] = domain.Token{
		ID: "tok-1", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: hash, CreatedAt: time.Now(),
	}
	return New(Deps{
		Memory:   memory.New(up, nil, memory.Options{MaxTopK: 50}, slog.Default()),
		Identity: identity.New(store, nil, "", slog.Default()),
		Audit:    recorder,
		Logger:   slog.Default(),
	})
}

type fakeIdentityStore struct {
	tenants map[string]domain.Tenant
	tokens  map[[32]byte]domain.Token
}

func (f *fakeIdentityStore) GetTenant(_ context.Context, id string) (domain.Tenant, error) {
	tenant, ok := f.tenants[id]
	if !ok {
		return domain.Tenant{}, domain.Wrap(domain.ErrNotFound, "get_tenant", "", nil)
	}
	return tenant, nil
}

func (f *fakeIdentityStore) UpsertTenant(context.Context, domain.Tenant) error { return nil }

func (f *fakeIdentityStore) EnsureTenant(context.Context, domain.Tenant) error { return nil }

func (f *fakeIdentityStore) CreateToken(context.Context, domain.Token) error { return nil }

func (f *fakeIdentityStore) UpsertToken(context.Context, domain.Token) error { return nil }

func (f *fakeIdentityStore) TokenByHash(_ context.Context, hash [32]byte) (domain.Token, error) {
	token, ok := f.tokens[hash]
	if !ok {
		return domain.Token{}, domain.Wrap(domain.ErrNotFound, "token_by_hash", "", nil)
	}
	return token, nil
}

func (f *fakeIdentityStore) GetToken(context.Context, string) (domain.Token, error) {
	return domain.Token{}, domain.Wrap(domain.ErrNotFound, "get_token", "", nil)
}

func (f *fakeIdentityStore) ListTokens(context.Context, string) ([]domain.Token, error) {
	return nil, nil
}

func (f *fakeIdentityStore) RevokeToken(context.Context, string, time.Time) error { return nil }

func (f *fakeIdentityStore) TouchToken(context.Context, string, time.Time) error { return nil }

func (f *fakeIdentityStore) UpdateTokenLabel(context.Context, string, string) error { return nil }

type fakeAuditStore struct {
	mu   sync.Mutex
	rows []domain.AuditRecord
}

func (f *fakeAuditStore) InsertAudit(ctx context.Context, r domain.AuditRecord) error {
	return f.InsertAudits(ctx, []domain.AuditRecord{r})
}

func (f *fakeAuditStore) InsertAudits(_ context.Context, rows []domain.AuditRecord) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rows = append(f.rows, rows...)
	return nil
}

func (f *fakeAuditStore) GetAudit(context.Context, string) (domain.AuditRecord, error) {
	return domain.AuditRecord{}, domain.NotFound("get_audit", "")
}

func (f *fakeAuditStore) AuditHourCounts(context.Context, service.AuditFilter) ([]domain.AuditHourCount, error) {
	return nil, nil
}

func (f *fakeAuditStore) ListAudit(context.Context, service.AuditFilter) ([]domain.AuditRecord, int, error) {
	return nil, 0, nil
}

func (f *fakeAuditStore) PurgeAuditBefore(context.Context, time.Time) (int64, error) { return 0, nil }

func (f *fakeAuditStore) records() []domain.AuditRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]domain.AuditRecord(nil), f.rows...)
}

type fakeUpstream struct {
	current    domain.Memory
	lastSearch domain.SearchQuery
	searchErr  error
	owners     map[string]string
	callID     string
	// addCalls 记录上游被真正调用的次数：异步 add 应该一次都不调。
	addCalls int
}

func (f *fakeUpstream) Add(context.Context, domain.AddPayload) (domain.AddResult, error) {
	f.addCalls++
	return domain.AddResult{Status: "success", Message: "ok", CallID: f.callID}, nil
}

func (f *fakeUpstream) Search(_ context.Context, q domain.SearchQuery) (domain.SearchResult, error) {
	if f.searchErr != nil {
		return domain.SearchResult{}, f.searchErr
	}
	f.lastSearch = q
	return domain.SearchResult{Facts: []domain.Memory{{ID: "mem-1", Content: "tea"}}}, nil
}

func (f *fakeUpstream) Profile(context.Context, domain.ProfileQuery) (domain.ProfileResult, error) {
	return domain.ProfileResult{Total: 1}, nil
}

func (f *fakeUpstream) OwnedMemory(_ context.Context, _, _, id string) (domain.Memory, bool, error) {
	if id != "mem-1" {
		return domain.Memory{}, false, nil
	}
	if f.current.ID == "" {
		f.current = domain.Memory{ID: id, Content: "tea", Version: 1}
	}
	return f.current, true, nil
}

func (f *fakeUpstream) OwnedMemories(_ context.Context, _, _ string, ids []string) (map[string]bool, error) {
	out := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id == "mem-1" {
			out[id] = true
		}
	}
	return out, nil
}

func (f *fakeUpstream) Owners(_ context.Context, _ []string) (map[string]string, error) {
	if f.owners == nil {
		return map[string]string{}, nil
	}
	return f.owners, nil
}

func (f *fakeUpstream) Delete(context.Context, string, []string) error { return nil }

func (f *fakeUpstream) Feedback(context.Context, domain.FeedbackRequest) (domain.FeedbackResult, error) {
	return domain.FeedbackResult{Status: "success", Message: "recorded"}, nil
}

// newAsyncTestServer 是启用异步写入的入口：真实部署里 AddAsync 打开、job 表可用，
// /memory/add 只入队不碰上游。
func newAsyncTestServer(t *testing.T, recorder *audit.Recorder) (*Server, *fakeJobs, *fakeUpstream) {
	t.Helper()
	store := &fakeIdentityStore{
		tenants: map[string]domain.Tenant{"cube-a": {ID: "cube-a", UserID: "alice"}},
		tokens:  map[[32]byte]domain.Token{},
	}
	hash := domain.HashToken(testToken)
	store.tokens[hash] = domain.Token{
		ID: "tok-1", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: hash, CreatedAt: time.Now(),
	}
	jobs := &fakeJobs{}
	up := &fakeUpstream{}
	return New(Deps{
		Memory:   memory.New(up, jobs, memory.Options{MaxTopK: 50, AddAsync: true}, slog.Default()),
		Identity: identity.New(store, nil, "", slog.Default()),
		Audit:    recorder,
		Logger:   slog.Default(),
	}), jobs, up
}

type fakeJobs struct{ jobs []domain.Job }

func (f *fakeJobs) EnqueueJob(_ context.Context, j domain.Job) error {
	f.jobs = append(f.jobs, j)
	return nil
}

// /memory/add 异步：受理即返回，不把 agent 卡在上游 LLM 改写上。
func TestAddIsAsyncAndDoesNotWaitForUpstream(t *testing.T) {
	srv, jobs, up := newAsyncTestServer(t, nil)
	rec := postJSON(t, srv, "/memory/add", `{"messages":[{"role":"user","content":"hi"}]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是 JSON: %v", err)
	}
	if deferred, _ := body["deferred"].(bool); !deferred {
		t.Fatalf("异步 add 的响应应带 deferred=true: %s", rec.Body.String())
	}
	callID, _ := body["call_id"].(string)
	if callID == "" {
		t.Fatalf("异步 add 应返回 call_id: %s", rec.Body.String())
	}
	if up.addCalls != 0 {
		t.Fatalf("异步 add 不应调用上游，实际 %d 次", up.addCalls)
	}
	if len(jobs.jobs) != 1 || jobs.jobs[0].Kind != domain.JobKindMemoryAdd {
		t.Fatalf("应入队一条 add 任务: %+v", jobs.jobs)
	}
	// 请求行与任务同 id，死信页/任务表才找得到这条写入。
	if jobs.jobs[0].ID != callID {
		t.Fatalf("任务 id=%q 与响应 call_id=%q 不一致", jobs.jobs[0].ID, callID)
	}
}

// 插件与直连脚本走同一批端点，审计靠客户端自报标识区分。
func TestAuditSourceDistinguishesPlugin(t *testing.T) {
	store := &fakeAuditStore{}
	recorder, err := audit.New(store, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = recorder.Run(ctx) }()

	srv := newTestServer(t, &fakeUpstream{}, recorder)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/search",
		bytes.NewBufferString(`{"query":"hi"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	req.Header.Set("X-GraphMemory-Client", "graphmemory/0.3.0")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}

	// 没有标识的算普通 REST 调用（运维脚本、其它客户端）。
	postJSON(t, srv, "/memory/search", `{"query":"hi"}`)

	cancel()
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := recorder.Stop(stopCtx); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}
	records := store.records()
	if len(records) != 2 {
		t.Fatalf("审计记录数=%d", len(records))
	}
	if records[0].Source != domain.AuditSourcePlugin {
		t.Fatalf("带插件标识的 source=%q want %q", records[0].Source, domain.AuditSourcePlugin)
	}
	if records[1].Source != domain.AuditSourceREST {
		t.Fatalf("无标识的 source=%q want %q", records[1].Source, domain.AuditSourceREST)
	}
}

// postJSON 发一个已鉴权的 POST，复用测试里到处重复的三行样板。
func postJSON(t *testing.T, srv *Server, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, path, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	return rec
}

// 插件头是 X-GraphMemory-Client；User-Agent 兜底。
func TestRequestSourceDistinguishesPluginByHeaderOrUserAgent(t *testing.T) {
	cases := []struct {
		name   string
		header string
		value  string
		want   string
	}{
		{"header", "X-GraphMemory-Client", "graphmemory/0.2.10", domain.AuditSourcePlugin},
		{"user agent only", "User-Agent", "graphmemory/0.2.10", domain.AuditSourcePlugin},
		{"no marker", "", "", domain.AuditSourceREST},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/search", nil)
			req.Header.Set("User-Agent", "curl/8.5.0")
			if tc.header != "" {
				req.Header.Set(tc.header, tc.value)
			}
			if got := requestSource(req); got != tc.want {
				t.Fatalf("requestSource=%q want %q", got, tc.want)
			}
		})
	}
}

// 插件自报的元数据要落进审计：版本、部署身份、上下文、轮次，无法从路径或 token 推导。
func TestAuditRecordsClientMeta(t *testing.T) {
	store := &fakeAuditStore{}
	recorder, err := audit.New(store, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = recorder.Run(ctx) }()

	srv := newTestServer(t, &fakeUpstream{}, recorder)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/search", bytes.NewBufferString(`{"query":"hi"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	req.Header.Set("X-GraphMemory-Client", "graphmemory/0.2.12")
	req.Header.Set("X-GraphMemory-Host", "hermes/0.21.5")
	req.Header.Set("X-GraphMemory-Agent", "example-agent")
	req.Header.Set("X-GraphMemory-Agent-Context", "primary")
	req.Header.Set("X-GraphMemory-Turn", "12")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	cancel()
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := recorder.Stop(stopCtx); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}
	records := store.records()
	if len(records) != 1 {
		t.Fatalf("审计记录数=%d", len(records))
	}
	got := records[0]
	if got.ClientVersion != "0.2.12" || got.HostVersion != "hermes/0.21.5" ||
		got.AgentIdentity != "example-agent" || got.AgentContext != "primary" || got.TurnNumber != 12 {
		t.Fatalf("自报元数据不符: %+v", got)
	}
	if got.Source != domain.AuditSourcePlugin {
		t.Fatalf("source=%q want %q", got.Source, domain.AuditSourcePlugin)
	}
}

// 头部里的非法轮次与超长值不能让写入失败，也不能原样落库。
func TestAuditClientMetaBounds(t *testing.T) {
	store := &fakeAuditStore{}
	recorder, err := audit.New(store, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = recorder.Run(ctx) }()

	srv := newTestServer(t, &fakeUpstream{}, recorder)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/search", bytes.NewBufferString(`{"query":"hi"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+testToken)
	req.Header.Set("X-GraphMemory-Turn", "abc")
	req.Header.Set("X-GraphMemory-Agent", strings.Repeat("x", 200))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	cancel()
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	if err := recorder.Stop(stopCtx); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}
	got := store.records()[0]
	if got.TurnNumber != 0 {
		t.Fatalf("非法轮次应记 0: %d", got.TurnNumber)
	}
	if len([]rune(got.AgentIdentity)) != metaMaxRunes {
		t.Fatalf("超长身份应截断到 %d 个字符，得到 %d", metaMaxRunes, len([]rune(got.AgentIdentity)))
	}
}

func TestSearchMode(t *testing.T) {
	up := &fakeUpstream{}
	srv := newTestServer(t, up, nil)
	post := func(body string) *httptest.ResponseRecorder {
		req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/memory/search", bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+testToken)
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, req)
		return rec
	}

	if rec := post(`{"query":"q","mode":"fast"}`); rec.Code != http.StatusOK || up.lastSearch.Mode != "fast" {
		t.Fatalf("status=%d mode=%q", rec.Code, up.lastSearch.Mode)
	}
	if rec := post(`{"query":"q","mode":"bogus"}`); rec.Code != http.StatusBadRequest {
		t.Fatalf("invalid mode: status=%d", rec.Code)
	}
}

func (f *fakeUpstream) UpdateMemory(_ context.Context, in domain.MemoryUpdate) error {
	f.current.Content = in.Content
	f.current.Version++
	if in.Title != nil {
		f.current.Key = *in.Title
	}
	return nil
}

func TestUpdateRouteDoesNotTrustBodyIdentity(t *testing.T) {
	t.Parallel()
	srv := newTestServer(t, &fakeUpstream{}, nil)
	for _, tc := range []struct {
		body   string
		status int
	}{
		{`{"memory_id":"foreign","content":"new","mem_cube_id":"other"}`, 404},
		{`{"memory_id":"mem-1","content":" "}`, 400},
		{`{"memory_id":"mem-1","content":"coffee"}`, 200},
	} {
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, newJSONRequest(context.Background(), t, "/memory/update", tc.body))
		if rec.Code != tc.status {
			t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
		}
	}
}
