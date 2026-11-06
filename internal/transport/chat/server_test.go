package chat_test

import (
	"bufio"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/chat"
	transportauth "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
	chattransport "github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/chat"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

type fakeAuth struct{}

func (fakeAuth) Authenticate(context.Context, domain.Credentials) (domain.Principal, error) {
	return domain.Principal{
		Kind:      domain.PrincipalAccount,
		AccountID: "a1",
		Username:  "alice",
		Roles:     map[string]domain.Role{"p1": domain.RoleEditor},
	}, nil
}

type fakeProjects struct{}

func (fakeProjects) Get(_ context.Context, id string) (domain.Tenant, error) {
	if id != "p1" {
		return domain.Tenant{}, domain.NotFound("get_tenant", "项目")
	}
	return domain.Tenant{ID: "p1", Name: "项目一"}, nil
}

type fakeTokens struct {
	tokens []domain.Token
}

func (f fakeTokens) ListTokens(_ context.Context, tenantID string) ([]domain.Token, error) {
	var out []domain.Token
	for i := range f.tokens {
		if f.tokens[i].TenantID == tenantID {
			out = append(out, f.tokens[i])
		}
	}
	return out, nil
}

func (f fakeTokens) GetToken(_ context.Context, id string) (domain.Token, error) {
	for i := range f.tokens {
		if f.tokens[i].ID == id {
			return f.tokens[i], nil
		}
	}
	return domain.Token{}, domain.NotFound("get_token", "Token")
}

type fakeConfigs struct {
	data map[string]domain.ChatConfig
}

func (f fakeConfigs) GetChatConfig(_ context.Context, tenantID string) (domain.ChatConfig, error) {
	cfg, ok := f.data[tenantID]
	if !ok {
		return domain.ChatConfig{}, domain.NotFound("get_chat_config", "聊天配置")
	}
	return cfg, nil
}

func (f fakeConfigs) UpsertChatConfig(_ context.Context, cfg domain.ChatConfig) error {
	f.data[cfg.TenantID] = cfg
	return nil
}

type fakeRuntime struct {
	session *fakeSession
}

func (f *fakeRuntime) Session(context.Context, domain.ChatSessionSpec) (domain.ChatSession, error) {
	return f.session, nil
}

func (f *fakeRuntime) Stop(context.Context, string) error { return nil }

type fakeSession struct {
	events chan domain.ChatEvent
	sub    domain.ChatSubmission
	err    error
	// calls 记录被受理/重试/中断的次数：未登录路径必须一次都不发生。
	calls atomic.Int32
}

func (s *fakeSession) Retry(context.Context) (domain.ChatSubmission, error) {
	s.calls.Add(1)
	if s.err != nil {
		return domain.ChatSubmission{}, s.err
	}
	if s.sub.ID == "" {
		s.sub.ID = "msg-1"
	}
	return s.sub, nil
}

func (s *fakeSession) Submit(context.Context, string) (domain.ChatSubmission, error) {
	s.calls.Add(1)
	if s.err != nil {
		return domain.ChatSubmission{}, s.err
	}
	if s.sub.ID == "" {
		s.sub.ID = "msg-1"
	}
	return s.sub, nil
}

func (s *fakeSession) Abort(context.Context) error {
	s.calls.Add(1)
	return nil
}

func (s *fakeSession) Messages(context.Context) (json.RawMessage, error) {
	return json.RawMessage(`{"messages":[]}`), nil
}

func (s *fakeSession) Subscribe(context.Context) (<-chan domain.ChatEvent, func()) {
	return s.events, func() {}
}

func newServer(t *testing.T, tokens fakeTokens, configs fakeConfigs, session *fakeSession) http.Handler {
	t.Helper()
	return newServerWithAuth(t, fakeAuth{}, tokens, configs, session)
}

// newServerWithAuth 允许替换鉴权器：未登录路径必须在这里能证明是 401。
// fakeAudit 收集审计记录，断言「谁在什么时候调用了对话」。
type fakeAudit struct {
	mu      sync.Mutex
	records []domain.AuditRecord
}

func (f *fakeAudit) Record(_ context.Context, rec domain.AuditRecord) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.records = append(f.records, rec)
}

func newServerWithAuth(t *testing.T, accounts transportauth.Authenticator, tokens fakeTokens, configs fakeConfigs, session *fakeSession) http.Handler {
	return newServerWithAudit(t, accounts, tokens, configs, session, nil)
}

func newServerWithAudit(t *testing.T, accounts transportauth.Authenticator, tokens fakeTokens, configs fakeConfigs, session *fakeSession, sink *fakeAudit) http.Handler {
	t.Helper()
	if session == nil {
		session = &fakeSession{}
	}
	runtime := &fakeRuntime{session: session}
	svc, err := chat.New(runtime, configs, fakeProjects{}, tokens, chat.Options{
		ContextWindow: 131072,
		Feedback:      &fakeFeedback{ratings: make(map[string]int)},
		MaxTokens:     16384,
	})
	if err != nil {
		t.Fatalf("chat.New: %v", err)
	}
	deps := chattransport.Deps{
		Chat:     svc,
		Accounts: accounts,
		Tokens:   tokens,
	}
	if sink != nil {
		deps.Audit = sink
	}
	server, err := chattransport.New(deps)
	if err != nil {
		t.Fatalf("transport.New: %v", err)
	}
	return server.Handler()
}

func readyTokens() fakeTokens {
	return fakeTokens{tokens: []domain.Token{{
		ID: "k1", TenantID: "p1", UserID: "u1", Prefix: "memos_ab",
		Raw: "memos_secret", Label: "默认", CreatedAt: time.Now(),
	}}}
}

func readyConfigs() fakeConfigs {
	return fakeConfigs{data: map[string]domain.ChatConfig{"p1": {
		TenantID: "p1", BaseURL: "https://gw.example/v1", Model: "m1", APIKey: "sk-model",
	}}}
}

func TestStateWithoutProject(t *testing.T) {
	t.Parallel()

	handler := newServer(t, fakeTokens{}, fakeConfigs{data: map[string]domain.ChatConfig{}}, &fakeSession{})
	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/api/chat/state", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var envelope struct {
		Code int            `json:"code"`
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode envelope: %v", err)
	}
	hasProject, _ := envelope.Data["hasProject"].(bool)
	if envelope.Code != 0 || hasProject {
		t.Fatalf("envelope = %+v", envelope)
	}
}

func TestStreamRejectedWhenKeyMissing(t *testing.T) {
	t.Parallel()

	handler := newServer(t, fakeTokens{}, readyConfigs(), &fakeSession{})
	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/api/chat/stream?projectId=p1", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); !strings.Contains(ct, "application/json") {
		t.Fatalf("content type = %q, want JSON error", ct)
	}
	var envelope httpx.Envelope
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if envelope.Code != httpx.CodeConflict || !strings.Contains(envelope.Message, "API Key") {
		t.Fatalf("envelope = %+v", envelope)
	}
}

func TestStreamFrames(t *testing.T) {
	t.Parallel()

	events := make(chan domain.ChatEvent, 4)
	handler := newServer(t, readyTokens(), readyConfigs(), &fakeSession{events: events})
	server := httptest.NewServer(handler)
	defer server.Close()

	events <- domain.ChatEvent{Seq: 1, Type: "chat_ready", Data: json.RawMessage(`{"sessionId":"chat-x"}`)}
	events <- domain.ChatEvent{Seq: 2, Type: "agent_start", Data: json.RawMessage(`{"type":"agent_start"}`), SubmissionID: "accepted-a"}

	req, err := http.NewRequestWithContext(context.Background(), http.MethodGet,
		server.URL+"/api/chat/stream?projectId=p1", nil)
	if err != nil {
		t.Fatalf("request: %v", err)
	}
	resp, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK || !strings.Contains(resp.Header.Get("Content-Type"), "text/event-stream") {
		t.Fatalf("status/type = %d %q", resp.StatusCode, resp.Header.Get("Content-Type"))
	}
	reader := bufio.NewReader(resp.Body)
	frames := make([]map[string]any, 0, 2)
	for len(frames) < 2 {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("read: %v (frames=%v)", err, frames)
		}
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		var frame map[string]any
		if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &frame); err != nil {
			t.Fatalf("decode frame %q: %v", line, err)
		}
		frames = append(frames, frame)
	}
	if frames[0]["type"] != "chat_ready" || frames[0]["seq"] != float64(1) {
		t.Fatalf("first frame = %+v", frames[0])
	}
	if frames[1]["type"] != "agent_start" || frames[1]["seq"] != float64(2) {
		t.Fatalf("second frame = %+v", frames[1])
	}
	if frames[1]["submissionId"] != "accepted-a" || frames[0]["submissionId"] != nil {
		t.Fatalf("submission identity: %+v", frames)
	}
	data, ok := frames[0]["data"].(map[string]any)
	if !ok || data["sessionId"] != "chat-x" {
		t.Fatalf("frame data = %+v", frames[0]["data"])
	}
}

func TestSendReturnsAcceptedSubmission(t *testing.T) {
	t.Parallel()

	session := &fakeSession{sub: domain.ChatSubmission{ID: "msg-9", Queued: true}}
	handler := newServer(t, readyTokens(), readyConfigs(), session)
	body := strings.NewReader(`{"projectId":"p1","text":"你好"}`)
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost,
		"/api/chat/messages", body)
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var envelope struct {
		Code int            `json:"code"`
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode: %v", err)
	}
	queued, _ := envelope.Data["queued"].(bool)
	if envelope.Code != 0 || envelope.Data["id"] != "msg-9" || !queued {
		t.Fatalf("envelope = %+v", envelope)
	}
}

// unauthenticatedAuth 模拟「没有登录」：零信任必需但没有外部身份，也没有会话。
type unauthenticatedAuth struct{}

func (unauthenticatedAuth) Authenticate(context.Context, domain.Credentials) (domain.Principal, error) {
	return domain.Principal{}, domain.Wrap(domain.ErrUnauthorized, "auth", "请先登录", nil)
}

// TestChatRequiresLogin 锁住入口：没有主体时 chat 的每个入口都必须 401，
// 且不能受理任何消息（不能出现「没登录也能和 agent 对话」）。
func TestChatRequiresLogin(t *testing.T) {
	t.Parallel()

	session := &fakeSession{}
	handler := newServerWithAuth(t, unauthenticatedAuth{}, readyTokens(), readyConfigs(), session)
	cases := []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodGet, "/api/chat/state?projectId=p1", ""},
		{http.MethodGet, "/api/chat/feedback?projectId=p1&responseId=" + strings.Repeat("a", 64), ""},
		{http.MethodPut, "/api/chat/feedback", `{"projectId":"p1","responseId":"test","vote":1}`},
		{http.MethodGet, "/api/chat/messages?projectId=p1", ""},
		{http.MethodGet, "/api/chat/stream?projectId=p1", ""},
		{http.MethodPost, "/api/chat/messages", `{"projectId":"p1","text":"hi"}`},
		{http.MethodPost, "/api/chat/retry", `{"projectId":"p1"}`},
		{http.MethodPost, "/api/chat/abort", `{"projectId":"p1"}`},
	}
	for _, c := range cases {
		req := httptest.NewRequestWithContext(context.Background(), c.method, c.path, strings.NewReader(c.body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("%s %s status = %d, body = %s", c.method, c.path, rec.Code, rec.Body.String())
		}
	}
	if n := session.calls.Load(); n != 0 {
		t.Fatalf("未登录不应触达会话对象，calls = %d", n)
	}
}

// TestChatCallsAreAudited 锁住审计：发消息/重试/中断都要留一条记录，带项目、
// 主体与来源，但**不能**包含消息正文。
func TestChatCallsAreAudited(t *testing.T) {
	t.Parallel()

	sink := &fakeAudit{}
	session := &fakeSession{sub: domain.ChatSubmission{ID: "msg-7"}}
	handler := newServerWithAudit(t, fakeAuth{}, readyTokens(), readyConfigs(), session, sink)
	for _, c := range []struct{ path, body string }{
		{"/api/chat/messages", `{"projectId":"p1","text":"我的密码是 hunter2"}`},
		{"/api/chat/retry", `{"projectId":"p1"}`},
		{"/api/chat/abort", `{"projectId":"p1"}`},
	} {
		req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, c.path, strings.NewReader(c.body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code < 200 || rec.Code >= 300 {
			t.Fatalf("%s status = %d, body = %s", c.path, rec.Code, rec.Body.String())
		}
	}

	sink.mu.Lock()
	defer sink.mu.Unlock()
	if len(sink.records) != 3 {
		t.Fatalf("审计条数 = %d，期望 3", len(sink.records))
	}
	wantTools := []string{"chat.send", "chat.retry", "chat.abort"}
	for i, rec := range sink.records {
		if rec.Tool != wantTools[i] || rec.TenantID != "p1" || rec.UserID != "alice" ||
			rec.Source != domain.AuditSourceDashboard || !rec.OK {
			t.Fatalf("审计记录 = %+v", rec)
		}
		if rec.SessionID == "" {
			t.Fatalf("审计缺少会话 ID: %+v", rec)
		}
		if payload, ok := rec.Input["chars"].(int); ok && payload == 0 {
			t.Fatalf("send 审计应记录正文长度: %+v", rec.Input)
		}
		for _, v := range rec.Input {
			if text, ok := v.(string); ok && strings.Contains(text, "hunter2") {
				t.Fatalf("审计不应包含消息正文: %+v", rec.Input)
			}
		}
	}
}

type fakeFeedback struct {
	mu      sync.Mutex
	ratings map[string]int
}

func (f *fakeFeedback) ChatFeedback(_ context.Context, p, a, r string) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.ratings[p+"/"+a+"/"+r], nil
}

func (f *fakeFeedback) SaveChatFeedback(_ context.Context, p, a, r string, v int, _ time.Time) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ratings[p+"/"+a+"/"+r] = v
	return nil
}

func (f *fakeFeedback) AnswerFeedback(ctx context.Context, p, a, r string) (domain.ChatAnswerFeedback, error) {
	v, e := f.ChatFeedback(ctx, p, a, r)
	return domain.ChatAnswerFeedback{Vote: v}, e
}

func (f *fakeFeedback) SaveAnswerFeedback(ctx context.Context, p, a, r string, v domain.ChatAnswerFeedback, _ bool, at time.Time) error {
	return f.SaveChatFeedback(ctx, p, a, r, v.Vote, at)
}

func TestAnswerFeedbackPersistsAndRejectsForeignProject(t *testing.T) {
	t.Parallel()
	session := &fakeSession{}
	handler := newServer(t, readyTokens(), readyConfigs(), session)
	id := strings.Repeat("a", 64)
	cases := []struct {
		method, path, body string
		status             int
		vote               int
	}{
		{http.MethodPut, "/api/chat/feedback", `{"projectId":"p1","responseId":"` + id + `","vote":1}`, 200, 1},
		{http.MethodGet, "/api/chat/feedback?projectId=p1&responseId=" + id, "", 200, 1},
		{http.MethodPut, "/api/chat/feedback", `{"projectId":"p1","responseId":"` + id + `","vote":-1}`, 200, -1},
		{http.MethodPut, "/api/chat/feedback", `{"projectId":"p1","responseId":"` + id + `","vote":0}`, 200, 0},
		{http.MethodGet, "/api/chat/feedback?projectId=p2&responseId=" + id, "", 403, 0},
		{http.MethodPut, "/api/chat/feedback", `{"projectId":"p2","responseId":"` + id + `","vote":1}`, 403, 0},
		{http.MethodPut, "/api/chat/feedback", `{"projectId":"p1","responseId":"` + id + `","vote":0.5}`, 400, 0},
	}
	for _, c := range cases {
		req := httptest.NewRequestWithContext(context.Background(), c.method, c.path, strings.NewReader(c.body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != c.status {
			t.Fatalf("%s %s status=%d body=%s", c.method, c.path, rec.Code, rec.Body.String())
		}
		if c.status == 200 {
			var result struct{ Data struct{ Vote int } }
			if err := json.Unmarshal(rec.Body.Bytes(), &result); err != nil || result.Data.Vote != c.vote {
				t.Fatalf("response=%s err=%v", rec.Body.String(), err)
			}
		}
	}
	if session.calls.Load() != 0 {
		t.Fatal("评分不能触发会话或模型")
	}
}

type readOnlyAuth struct{}

func (readOnlyAuth) Authenticate(context.Context, domain.Credentials) (domain.Principal, error) {
	return domain.Principal{Kind: domain.PrincipalAccount, AccountID: "a1", Username: "alice", Roles: map[string]domain.Role{"p1": domain.RoleViewer}}, nil
}

func TestReadOnlyMemberCannotInvokeWriteCapableChat(t *testing.T) {
	t.Parallel()
	h := newServerWithAuth(t, readOnlyAuth{}, fakeTokens{}, fakeConfigs{}, nil)
	for _, path := range []string{"messages", "retry"} {
		r := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/api/chat/"+path, strings.NewReader(`{"projectId":"p1","text":"delete memory"}`))
		r.Header.Set("Content-Type", "application/json")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != http.StatusForbidden {
			t.Fatalf("%s: %d %s", path, w.Code, w.Body.String())
		}
	}
	r := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/api/chat/state?projectId=p1", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	var body struct {
		Data struct {
			ReadOnly bool `json:"readOnly"`
			CanChat  bool `json:"canChat"`
		}
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || !body.Data.ReadOnly || body.Data.CanChat {
		t.Fatalf("state: %s %v", w.Body.String(), err)
	}
}
