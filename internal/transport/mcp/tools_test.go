package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	mcpgo "github.com/mark3labs/mcp-go/mcp"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
)

const goldenPath = "../../../test/golden/mcp/tools.json"

var updateGolden = flag.Bool("update-golden", false, "重写 test/golden 快照")

type goldenTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
}

func TestToolsGolden(t *testing.T) {
	srv := newTestServer(t, nil, nil)

	tools := srv.MCPServer().ListTools()
	names := make([]string, 0, len(tools))
	for name := range tools {
		names = append(names, name)
	}
	sort.Strings(names)

	golden := make([]goldenTool, 0, len(names))
	for _, name := range names {
		tool := tools[name].Tool
		golden = append(golden, goldenTool{
			Name:        tool.Name,
			Description: tool.Description,
			InputSchema: tool.RawInputSchema,
		})
	}
	got, err := json.MarshalIndent(golden, "", " ")
	if err != nil {
		t.Fatalf("marshal golden: %v", err)
	}
	got = append(got, '\n')

	if *updateGolden {
		if err := os.MkdirAll(filepath.Dir(goldenPath), 0o750); err != nil {
			t.Fatalf("mkdir golden: %v", err)
		}
		if err := os.WriteFile(goldenPath, got, 0o600); err != nil {
			t.Fatalf("write golden: %v", err)
		}
	}
	want, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("MCP 工具契约与 golden 不一致；确认契约变更后运行 go test ./internal/transport/mcp -update-golden")
	}
}

func TestSearchMemoryDreamDescription(t *testing.T) {
	store := &fakeIdentityStore{
		tenants: map[string]domain.Tenant{"cube-a": {ID: "cube-a", UserID: "alice"}},
		tokens:  map[[32]byte]domain.Token{},
	}
	srv, err := New(Deps{
		Memory:   memory.New(nil, nil, memory.Options{MaxTopK: 50, DreamTopK: 2}, slog.Default()),
		Identity: identity.New(store, nil, "", slog.Default()),
		Name:     "memos",
		Version:  "test",
		Limits:   memory.Options{MaxTopK: 50, DreamTopK: 2},
	})
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	got := srv.MCPServer().ListTools()["search_memory"].Tool.Description
	if got != searchMemoryDreamDescription {
		t.Fatalf("DreamTopK>0 时未使用 dream 描述")
	}

	// 除 dream 的 top_k 说明与两条 Notes 外，必须与线上版逐字一致。
	normalized := strings.Replace(got,
		"  - `top_k`: Max items returned per memory category (default 10, max 50). Facts, preferences, tool memories, and cross-case digests are each truncated to this number.",
		"  - `top_k`: Max items returned per memory category (default 10, max 50). Facts, preferences, and tool memories are each truncated to this number.", 1)
	normalized = strings.Replace(normalized,
		"\n  - `dream_memory_detail_list` (when present) holds **cross-case digests**: several separate tasks of one period summarized together, plus insights derived from them. Same item shape as `memory_detail_list` (`memory_key` is a title, `memory_value` is the digest).", "", 1)
	normalized = strings.Replace(normalized,
		"\n  - These digests are **generalizations, not records of the current case**. Treat them as background only; they never satisfy step 5.", "", 1)
	if normalized != searchMemoryDescription {
		t.Fatalf("dream 版改动了线上描述的非 dream 部分")
	}
}

func TestSearchMemoryRequiresToken(t *testing.T) {
	srv := newTestServer(t, nil, nil)
	result, err := srv.handleSearchMemory(context.Background(), callRequest("search_memory", map[string]any{"query": "hello"}))
	if err != nil {
		t.Fatalf("handle: %v", err)
	}
	if !result.IsError {
		t.Fatalf("无 token 时应返回工具错误")
	}
}

func TestSearchMemoryResponseFields(t *testing.T) {
	up := &fakeUpstream{search: domain.SearchResult{
		Facts: []domain.Memory{{
			ID:         "mem-1",
			Type:       domain.MemoryTypeText,
			Content:    "likes tea",
			Key:        "drink",
			Relativity: 0.9,
			CreatedAt:  mustTime(t, "2026-01-02T03:04:05Z"),
		}},
	}}
	token := "memos_test"
	srv := newTestServer(t, up, &token)
	ctx := withAuthorization(context.Background(), "Bearer "+token)

	result, err := srv.handleSearchMemory(ctx, callRequest("search_memory", map[string]any{"query": "tea"}))
	if err != nil {
		t.Fatalf("handle: %v", err)
	}
	if result.IsError {
		t.Fatalf("unexpected tool error: %+v", result.Content)
	}
	content, ok := result.Content[0].(mcpgo.TextContent)
	if !ok {
		t.Fatalf("unexpected content type: %T", result.Content[0])
	}
	text := content.Text
	for _, want := range []string{`"memory_detail_list"`, `"memory_key": "drink"`, `"memory_id": "mem-1"`} {
		if !bytes.Contains([]byte(text), []byte(want)) {
			t.Fatalf("输出缺少 %s: %s", want, text)
		}
	}
	if up.gotTenant != "cube-a" || up.gotUser != "alice" {
		t.Fatalf("身份未从 token 解析: tenant=%q user=%q", up.gotTenant, up.gotUser)
	}
}

// 显式 session_id 必须优先于 conversation_first_message，三个工具一致。
func TestExplicitSessionIDTakesPrecedence(t *testing.T) {
	up := &fakeUpstream{}
	token := "memos_test"
	srv := newTestServer(t, up, &token)
	ctx := withAuthorization(context.Background(), "Bearer "+token)

	call := func(tool string, args map[string]any) {
		t.Helper()
		var err error
		switch tool {
		case domain.ToolAddMessage:
			_, err = srv.handleAddMessage(ctx, callRequest(tool, args))
		case domain.ToolSearchMemory:
			_, err = srv.handleSearchMemory(ctx, callRequest(tool, args))
		case domain.ToolAddFeedback:
			_, err = srv.handleAddFeedback(ctx, callRequest(tool, args))
		}
		if err != nil {
			t.Fatalf("%s: %v", tool, err)
		}
	}

	addArgs := map[string]any{
		"conversation_first_message": "你好",
		"messages":                   []any{map[string]any{"role": "user", "content": "hi"}},
	}
	addArgs["session_id"] = "sess-explicit"
	call(domain.ToolAddMessage, addArgs)
	if up.gotAdd.SessionID != "sess-explicit" {
		t.Fatalf("add session = %q", up.gotAdd.SessionID)
	}

	searchArgs := map[string]any{"query": "hi", "conversation_first_message": "你好", "session_id": "sess-explicit"}
	call(domain.ToolSearchMemory, searchArgs)
	if up.gotSearch.SessionID != "sess-explicit" {
		t.Fatalf("search session = %q", up.gotSearch.SessionID)
	}

	feedbackArgs := map[string]any{
		"feedback_content": "用户想改成别的", "conversation_first_message": "你好", "session_id": "sess-explicit",
	}
	call(domain.ToolAddFeedback, feedbackArgs)
	if up.gotFeedback.SessionID != "sess-explicit" {
		t.Fatalf("feedback session = %q", up.gotFeedback.SessionID)
	}

	// 不传时仍回落到首条消息哈希，老客户端行为不变。
	delete(addArgs, "session_id")
	call(domain.ToolAddMessage, addArgs)
	if up.gotAdd.SessionID != memory.ResolveSessionID("", "你好") {
		t.Fatalf("fallback session = %q", up.gotAdd.SessionID)
	}
}

func newTestServer(t *testing.T, upstream *fakeUpstream, token *string) *Server {
	t.Helper()
	return newTestServerWith(t, upstream, token, nil, nil, memory.Options{})
}

func newTestServerWith(t *testing.T, upstream *fakeUpstream, token *string, jobs memory.Jobs, recorder *audit.Recorder, opts memory.Options) *Server {
	t.Helper()
	store := &fakeIdentityStore{
		tenants: map[string]domain.Tenant{"cube-a": {ID: "cube-a", UserID: "alice"}},
		tokens:  map[[32]byte]domain.Token{},
	}
	if token != nil {
		raw := *token
		store.tokens[domain.HashToken(raw)] = domain.Token{
			ID:        "tok-1",
			Kind:      domain.TokenKindTenant,
			TenantID:  "cube-a",
			UserID:    "alice",
			Hash:      domain.HashToken(raw),
			CreatedAt: time.Now(),
		}
	}
	var up memory.Upstream
	if upstream != nil {
		up = upstream
	}
	if opts.MaxTopK == 0 {
		opts.MaxTopK = 50
	}
	srv, err := New(Deps{
		Memory:   memory.New(up, jobs, opts, slog.Default()),
		Identity: identity.New(store, nil, "", slog.Default()),
		Audit:    recorder,
		Name:     "memos",
		Version:  "test",
	})
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	return srv
}

func callRequest(name string, args map[string]any) mcpgo.CallToolRequest {
	req := mcpgo.CallToolRequest{}
	req.Params.Name = name
	req.Params.Arguments = args
	return req
}

func mustTime(t *testing.T, value string) domain.UTCTime {
	t.Helper()
	parsed, err := domain.ParseUTC(value)
	if err != nil {
		t.Fatalf("parse time: %v", err)
	}
	return parsed
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

func (f *fakeIdentityStore) GetToken(_ context.Context, id string) (domain.Token, error) {
	for hash := range f.tokens {
		if f.tokens[hash].ID == id {
			return f.tokens[hash], nil
		}
	}
	return domain.Token{}, domain.Wrap(domain.ErrNotFound, "get_token", "", nil)
}

func (f *fakeIdentityStore) ListTokens(context.Context, string) ([]domain.Token, error) {
	return nil, nil
}

func (f *fakeIdentityStore) RevokeToken(context.Context, string, time.Time) error { return nil }

func (f *fakeIdentityStore) TouchToken(context.Context, string, time.Time) error { return nil }

func (f *fakeIdentityStore) UpdateTokenLabel(context.Context, string, string) error { return nil }

type fakeUpstream struct {
	search      domain.SearchResult
	gotTenant   string
	gotUser     string
	gotAdd      domain.AddPayload
	gotSearch   domain.SearchQuery
	gotFeedback domain.FeedbackRequest
}

func (f *fakeUpstream) Add(_ context.Context, p domain.AddPayload) (domain.AddResult, error) {
	f.gotAdd = p
	return domain.AddResult{Status: "success"}, nil
}

func (f *fakeUpstream) Search(_ context.Context, q domain.SearchQuery) (domain.SearchResult, error) {
	f.gotTenant = q.TenantID
	f.gotUser = q.UserID
	f.gotSearch = q
	return f.search, nil
}

func (f *fakeUpstream) Profile(context.Context, domain.ProfileQuery) (domain.ProfileResult, error) {
	return domain.ProfileResult{}, errors.New("not used")
}

func (f *fakeUpstream) OwnedMemory(context.Context, string, string, string) (domain.Memory, bool, error) {
	return domain.Memory{}, false, nil
}

func (f *fakeUpstream) OwnedMemories(context.Context, string, string, []string) (map[string]bool, error) {
	return map[string]bool{}, nil
}

func (f *fakeUpstream) Owners(context.Context, []string) (map[string]string, error) {
	return map[string]string{}, nil
}

func (f *fakeUpstream) Delete(context.Context, string, []string) error { return nil }

func (f *fakeUpstream) Feedback(_ context.Context, r domain.FeedbackRequest) (domain.FeedbackResult, error) {
	f.gotFeedback = r
	return domain.FeedbackResult{}, nil
}

// search_memory 的 schema 默认值必须跟随配置，否则对外声明与实际行为不一致。
func TestSearchSchemaFollowsConfiguredTopK(t *testing.T) {
	t.Parallel()

	srv, err := New(Deps{
		Memory:   memory.New(nil, nil, memory.Options{DefaultTopK: 25, MaxTopK: 50}, slog.Default()),
		Identity: identity.New(&fakeIdentityStore{}, nil, "", slog.Default()),
		Limits:   memory.Options{DefaultTopK: 25, MaxTopK: 50},
	})
	if err != nil {
		t.Fatalf("new server: %v", err)
	}
	tool := srv.MCPServer().ListTools()["search_memory"].Tool
	var schema struct {
		Properties struct {
			TopK struct {
				Default int `json:"default"`
			} `json:"top_k"`
		} `json:"properties"`
	}
	if err := json.Unmarshal(tool.RawInputSchema, &schema); err != nil {
		t.Fatalf("decode schema: %v", err)
	}
	if schema.Properties.TopK.Default != 25 {
		t.Fatalf("top_k default = %d, want 25", schema.Properties.TopK.Default)
	}
}

// 异步 add 的 job.ID 必须等于审计请求行的 call_id，worker 的终态行才能按 call_id 合并。
func TestAddMessageAsyncCarriesCallIDToAudit(t *testing.T) {
	t.Parallel()

	token := "memos_test"
	jobs := &captureJobs{}
	audits := &captureAuditStore{}
	recorder, err := audit.New(audits, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	srv := newTestServerWith(t, &fakeUpstream{}, &token, jobs, recorder, memory.Options{AddAsync: true})

	ctx := withAuthorization(context.Background(), "Bearer "+token)
	if _, err := srv.handleAddMessage(ctx, callRequest(domain.ToolAddMessage, map[string]any{
		"conversation_first_message": "你好",
		"messages":                   []any{map[string]any{"role": "user", "content": "hi"}},
	})); err != nil {
		t.Fatalf("add_message: %v", err)
	}
	if err := recorder.Stop(context.Background()); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}
	rows := audits.records()
	if len(rows) != 1 {
		t.Fatalf("审计行数=%d want 1", len(rows))
	}
	rec := rows[0]
	if !rec.Deferred || rec.CallID == "" {
		t.Fatalf("异步请求行应 deferred 且带 call_id: %+v", rec)
	}
	// 与 REST 门面同名的工具靠 source 区分：MCP 这条是异步入队。
	if rec.Source != domain.AuditSourceMCP {
		t.Fatalf("source=%q want %q", rec.Source, domain.AuditSourceMCP)
	}
	if jobs.enqueued().ID != rec.CallID {
		t.Fatalf("job.ID=%q want call_id=%q", jobs.enqueued().ID, rec.CallID)
	}
}

// 幂等重放与首次请求共用 call_id：两条审计请求行的 call_id 相同，重放不再永久 RUNNING。
func TestAddMessageIdempotentReplaySharesCallID(t *testing.T) {
	t.Parallel()

	token := "memos_test"
	jobs := &captureJobs{}
	audits := &captureAuditStore{}
	recorder, err := audit.New(audits, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	srv := newTestServerWith(t, &fakeUpstream{}, &token, jobs, recorder,
		memory.Options{AddAsync: true, Idem: newMemoryIdem()})

	ctx := withAuthorization(context.Background(), "Bearer "+token)
	args := map[string]any{
		"conversation_first_message": "你好",
		"messages":                   []any{map[string]any{"role": "user", "content": "hi"}},
	}
	for i := range 2 {
		if _, err := srv.handleAddMessage(ctx, callRequest(domain.ToolAddMessage, args)); err != nil {
			t.Fatalf("add_message #%d: %v", i+1, err)
		}
	}
	if err := recorder.Stop(context.Background()); err != nil {
		t.Fatalf("stop recorder: %v", err)
	}

	rows := audits.records()
	if len(rows) != 2 {
		t.Fatalf("审计行数=%d want 2", len(rows))
	}
	first, replay := rows[0], rows[1]
	if first.CallID == "" || replay.CallID != first.CallID {
		t.Fatalf("重放应沿用首次 call_id: first=%q replay=%q", first.CallID, replay.CallID)
	}
	if !first.Deferred || !replay.Deferred {
		t.Fatalf("两次异步 add 都应 deferred: first=%+v replay=%+v", first, replay)
	}
	if jobs.enqueued().ID != first.CallID {
		t.Fatalf("job.ID=%q want call_id=%q", jobs.enqueued().ID, first.CallID)
	}
}

type captureJobs struct {
	mu  sync.Mutex
	job domain.Job
}

func (c *captureJobs) EnqueueJob(_ context.Context, j domain.Job) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.job = j
	return nil
}

func (c *captureJobs) enqueued() domain.Job {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.job
}

// captureAuditStore 只用于观察 Record 的落库结果，查询方法不会被调用。
type captureAuditStore struct {
	mu   sync.Mutex
	rows []domain.AuditRecord
}

func (c *captureAuditStore) InsertAudit(ctx context.Context, rec domain.AuditRecord) error {
	return c.InsertAudits(ctx, []domain.AuditRecord{rec})
}

func (c *captureAuditStore) InsertAudits(_ context.Context, rows []domain.AuditRecord) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rows = append(c.rows, rows...)
	return nil
}

func (c *captureAuditStore) ListAudit(context.Context, service.AuditFilter) ([]domain.AuditRecord, int, error) {
	return nil, 0, nil
}

func (c *captureAuditStore) GetAudit(context.Context, string) (domain.AuditRecord, error) {
	return domain.AuditRecord{}, domain.NotFound("get_audit", "审计记录不存在")
}

func (c *captureAuditStore) AuditHourCounts(context.Context, service.AuditFilter) ([]domain.AuditHourCount, error) {
	return nil, nil
}

func (c *captureAuditStore) PurgeAuditBefore(context.Context, time.Time) (int64, error) {
	return 0, nil
}

func (c *captureAuditStore) records() []domain.AuditRecord {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]domain.AuditRecord(nil), c.rows...)
}

// memoryIdem 是测试用的请求去重实现，语义与 store 一致：
// 首次 Claim 占用，异步入队后记 accepted，重放取缓存响应。
type memoryIdem struct {
	mu      sync.Mutex
	records map[string]domain.IdempotencyRecord
}

func newMemoryIdem() *memoryIdem {
	return &memoryIdem{records: map[string]domain.IdempotencyRecord{}}
}

func (m *memoryIdem) ClaimIdempotency(_ context.Context, rec domain.IdempotencyRecord) (domain.IdempotencyRecord, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	key := rec.TenantID + "|" + rec.Key
	if existing, ok := m.records[key]; ok {
		return existing, false, nil
	}
	m.records[key] = rec
	return rec, true, nil
}

func (m *memoryIdem) CompleteIdempotency(_ context.Context, p domain.IdempotencyOutcome) error {
	m.set(p.TenantID, p.Key, domain.IdempotencySucceeded, p.Response)
	return nil
}

func (m *memoryIdem) AcceptIdempotency(_ context.Context, p domain.IdempotencyOutcome) error {
	m.set(p.TenantID, p.Key, domain.IdempotencyAccepted, p.Response)
	return nil
}

func (m *memoryIdem) SucceedIdempotency(_ context.Context, tenantID, key string, _ time.Time) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec := m.records[tenantID+"|"+key]
	rec.State = domain.IdempotencySucceeded
	m.records[tenantID+"|"+key] = rec
	return nil
}

func (m *memoryIdem) ReleaseIdempotency(_ context.Context, tenantID, key string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.records, tenantID+"|"+key)
	return nil
}

func (m *memoryIdem) set(tenantID, key, state string, response []byte) {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec := m.records[tenantID+"|"+key]
	rec.State = state
	rec.Response = response
	m.records[tenantID+"|"+key] = rec
}

func TestFailedInvocationRetainsErrorAndCallIdentity(t *testing.T) {
	t.Parallel()
	for _, authenticated := range []bool{false, true} {
		t.Run(strconv.FormatBool(authenticated), func(t *testing.T) {
			t.Parallel()
			srv := newTestServer(t, nil, nil)
			ctx := context.Background()
			if authenticated {
				ctx = withCaller(ctx, domain.Caller{TenantID: "cube-a", UserID: "alice"})
			}
			result, err := srv.invoke(ctx, domain.ToolSearchMemory, callRequest("search_memory", map[string]any{}),
				func(context.Context, domain.Caller, string) (map[string]any, bool, string, error) {
					return nil, false, "actual-failed-call", domain.Invalid("query", "请填写查询内容")
				})
			if err != nil || !result.IsError {
				t.Fatalf("expected tool failure: %v %+v", err, result)
			}
			if !authenticated {
				if len(result.Content) != 1 {
					t.Fatal("unauthenticated error must not expose call identity")
				}
				return
			}
			if len(result.Content) != 2 {
				t.Fatalf("missing correlation block: %+v", result)
			}
			first, ok := result.Content[0].(mcpgo.TextContent)
			if !ok || first.Text != "query: 请填写查询内容" {
				t.Fatalf("original error changed: %+v", result)
			}
			second, ok := result.Content[1].(mcpgo.TextContent)
			if !ok {
				t.Fatalf("unexpected correlation block: %T", result.Content[1])
			}
			var payload map[string]string
			if err := json.Unmarshal([]byte(second.Text), &payload); err != nil {
				t.Fatal(err)
			}
			if payload["status"] != "failed" || payload["call_id"] != "actual-failed-call" {
				t.Fatalf("wrong identity: %+v", payload)
			}
		})
	}
}
