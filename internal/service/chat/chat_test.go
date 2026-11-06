package chat_test

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/chat"
)

type fakeRuntime struct {
	mu         sync.Mutex
	sessions   []domain.ChatSessionSpec
	stopped    []string
	session    *fakeSession
	sessionErr error
}

func (f *fakeRuntime) Session(_ context.Context, spec domain.ChatSessionSpec) (domain.ChatSession, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.sessions = append(f.sessions, spec)
	if f.sessionErr != nil {
		return nil, f.sessionErr
	}
	if f.session == nil {
		f.session = &fakeSession{}
	}
	return f.session, nil
}

func (f *fakeRuntime) Stop(_ context.Context, sessionID string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stopped = append(f.stopped, sessionID)
	return nil
}

func (f *fakeRuntime) snapshot() ([]domain.ChatSessionSpec, []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]domain.ChatSessionSpec(nil), f.sessions...), append([]string(nil), f.stopped...)
}

type fakeSession struct {
	mu         sync.Mutex
	submitted  []string
	submission domain.ChatSubmission
	submitErr  error
	messages   json.RawMessage
	aborted    bool
	retried    bool
}

func (s *fakeSession) Retry(_ context.Context) (domain.ChatSubmission, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.submitErr != nil {
		return domain.ChatSubmission{}, s.submitErr
	}
	s.retried = true
	sub := s.submission
	if sub.ID == "" {
		sub.ID = "msg-1"
	}
	return sub, nil
}

func (s *fakeSession) Submit(_ context.Context, text string) (domain.ChatSubmission, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.submitErr != nil {
		return domain.ChatSubmission{}, s.submitErr
	}
	s.submitted = append(s.submitted, text)
	sub := s.submission
	if sub.ID == "" {
		sub.ID = "msg-1"
	}
	return sub, nil
}

func (s *fakeSession) Abort(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.aborted = true
	return nil
}

func (s *fakeSession) Messages(context.Context) (json.RawMessage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.messages != nil {
		return s.messages, nil
	}
	return json.RawMessage(`{"messages":[{"role":"user","content":"hi"}]}`), nil
}

func (s *fakeSession) Subscribe(context.Context) (<-chan domain.ChatEvent, func()) {
	ch := make(chan domain.ChatEvent)
	return ch, func() {}
}

func (s *fakeSession) sent() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.submitted...)
}

type fakeConfigs struct {
	mu   sync.Mutex
	data map[string]domain.ChatConfig
}

func (f *fakeConfigs) GetChatConfig(_ context.Context, tenantID string) (domain.ChatConfig, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cfg, ok := f.data[tenantID]
	if !ok {
		return domain.ChatConfig{}, domain.NotFound("get_chat_config", "聊天配置")
	}
	return cfg, nil
}

func (f *fakeConfigs) UpsertChatConfig(_ context.Context, cfg domain.ChatConfig) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.data == nil {
		f.data = map[string]domain.ChatConfig{}
	}
	f.data[cfg.TenantID] = cfg
	return nil
}

type fakeProjects struct {
	tenants map[string]domain.Tenant
}

func (f *fakeProjects) Get(_ context.Context, id string) (domain.Tenant, error) {
	t, ok := f.tenants[id]
	if !ok {
		return domain.Tenant{}, domain.NotFound("get_tenant", "项目")
	}
	return t, nil
}

type fakeTokens struct {
	tokens []domain.Token
}

func (f *fakeTokens) ListTokens(_ context.Context, tenantID string) ([]domain.Token, error) {
	out := make([]domain.Token, 0, len(f.tokens))
	for i := range f.tokens {
		if f.tokens[i].TenantID == tenantID {
			out = append(out, f.tokens[i])
		}
	}
	return out, nil
}

func (f *fakeTokens) GetToken(_ context.Context, id string) (domain.Token, error) {
	for i := range f.tokens {
		if f.tokens[i].ID == id {
			return f.tokens[i], nil
		}
	}
	return domain.Token{}, domain.NotFound("get_token", "Token")
}

func newService(t *testing.T, runtime *fakeRuntime, configs *fakeConfigs, projects *fakeProjects, tokens *fakeTokens) *chat.Service {
	t.Helper()
	if runtime == nil {
		runtime = &fakeRuntime{}
	}
	if configs == nil {
		configs = &fakeConfigs{data: map[string]domain.ChatConfig{}}
	}
	if projects == nil {
		projects = &fakeProjects{tenants: map[string]domain.Tenant{}}
	}
	if tokens == nil {
		tokens = &fakeTokens{}
	}
	svc, err := chat.New(runtime, configs, projects, tokens, chat.Options{
		ContextWindow: 131072,
		MaxTokens:     16384,
	})
	if err != nil {
		t.Fatalf("chat.New: %v", err)
	}
	return svc
}

func testToken(id, account string, created time.Time, raw string) domain.Token {
	return domain.Token{
		ID:                 id,
		Kind:               domain.TokenKindTenant,
		TenantID:           "p1",
		UserID:             "user",
		Prefix:             "memos_ab",
		Raw:                raw,
		Label:              id,
		CreatedAt:          created,
		CreatedByAccountID: account,
	}
}

func TestStatePreconditions(t *testing.T) {
	t.Parallel()

	base := time.Now()
	tests := []struct {
		name       string
		projectID  string
		projects   *fakeProjects
		tokens     *fakeTokens
		configs    *fakeConfigs
		hasProject bool
		hasKey     bool
		hasConfig  bool
		canChat    bool
	}{
		{
			name: "empty project",
		},
		{
			name:      "unknown project",
			projectID: "missing",
		},
		{
			name:       "project without key",
			projectID:  "p1",
			projects:   &fakeProjects{tenants: map[string]domain.Tenant{"p1": {ID: "p1", Name: "项目一"}}},
			hasProject: true,
		},
		{
			name:       "key without config",
			projectID:  "p1",
			projects:   &fakeProjects{tenants: map[string]domain.Tenant{"p1": {ID: "p1"}}},
			tokens:     &fakeTokens{tokens: []domain.Token{testToken("k1", "a1", base, "memos_raw")}},
			hasProject: true,
			hasKey:     true,
		},
		{
			name:      "all ready",
			projectID: "p1",
			projects:  &fakeProjects{tenants: map[string]domain.Tenant{"p1": {ID: "p1", Name: "项目一"}}},
			tokens:    &fakeTokens{tokens: []domain.Token{testToken("k1", "a1", base, "memos_raw")}},
			configs: &fakeConfigs{data: map[string]domain.ChatConfig{"p1": {
				TenantID: "p1", BaseURL: "https://gw.example/v1", Model: "m1", APIKey: "sk-test",
			}}},
			hasProject: true,
			hasKey:     true,
			hasConfig:  true,
			canChat:    true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			svc := newService(t, nil, tt.configs, tt.projects, tt.tokens)
			state, err := svc.State(context.Background(), tt.projectID, "a1")
			if err != nil {
				t.Fatalf("State: %v", err)
			}
			if state.HasProject != tt.hasProject || state.HasAPIKey != tt.hasKey ||
				state.HasConfig != tt.hasConfig || state.CanChat != tt.canChat {
				t.Fatalf("state = %+v, want project/key/config/can = %v/%v/%v/%v",
					state, tt.hasProject, tt.hasKey, tt.hasConfig, tt.canChat)
			}
			if tt.hasKey && (state.APIKey == nil || state.APIKey.ID != "k1") {
				t.Errorf("api key view = %+v", state.APIKey)
			}
			if tt.hasProject && state.SessionID != chat.SessionID("a1", "p1") {
				t.Errorf("session id = %q", state.SessionID)
			}
			if state.ContextWindow != 131072 || !state.AutoCompact {
				t.Errorf("state defaults = %+v", state)
			}
		})
	}
}

func TestPickKeyPrefersCurrentAccountAndSkipsUnusable(t *testing.T) {
	t.Parallel()

	now := time.Now()
	tokens := []domain.Token{
		testToken("expired-preferred", "a1", now.Add(-time.Minute), "memos_expired"),
		testToken("revoked", "a1", now, "memos_revoked"),
		testToken("no-raw", "a1", now, ""),
		testToken("other-newer", "a2", now.Add(2*time.Minute), "memos_other"),
		testToken("mine-older", "a1", now.Add(-time.Hour), "memos_mine"),
	}
	tokens[0].ExpiresAt = &now
	tokens[1].RevokedAt = &now
	svc := newService(t, nil, nil,
		&fakeProjects{tenants: map[string]domain.Tenant{"p1": {ID: "p1"}}},
		&fakeTokens{tokens: tokens})

	state, err := svc.State(context.Background(), "p1", "a1")
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if state.APIKey == nil || state.APIKey.ID != "mine-older" {
		t.Fatalf("api key = %+v, want mine-older", state.APIKey)
	}

	// 当前账号没有可用 Key 时回退到最近创建的一把。
	state, err = svc.State(context.Background(), "p1", "a9")
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if state.APIKey == nil || state.APIKey.ID != "other-newer" {
		t.Fatalf("fallback api key = %+v, want other-newer", state.APIKey)
	}
}

func TestSessionIDStableAndOpaque(t *testing.T) {
	t.Parallel()

	id := chat.SessionID("account-1", "project-1")
	if id != chat.SessionID("account-1", "project-1") {
		t.Fatal("session id is not stable")
	}
	if !strings.HasPrefix(id, "chat-") || len(id) != len("chat-")+24 {
		t.Fatalf("session id = %q", id)
	}
	if id == chat.SessionID("account-1", "project-2") {
		t.Fatal("session id must depend on project")
	}
	// 平台管理员 Token 没有账号 ID：必须落到稳定的 admin-token 标识，
	// 否则所有空账号会共用同一个空目录。
	if chat.SessionID("", "project-1") != chat.SessionID(domain.AdminTokenUsername, "project-1") {
		t.Fatal("empty account id must map to admin-token")
	}
}

func TestSaveConfigKeepsExistingKeyAndStopsSession(t *testing.T) {
	t.Parallel()

	configs := &fakeConfigs{data: map[string]domain.ChatConfig{"p1": {
		TenantID: "p1", BaseURL: "https://old.example/v1", Model: "old", APIKey: "sk-old",
	}}}
	runtime := &fakeRuntime{}
	svc := newService(t, runtime, configs, nil, nil)

	view, err := svc.SaveConfig(context.Background(), "p1", "a1", chat.SaveConfigInput{
		BaseURL: "https://new.example/v1",
		Model:   "new-model",
	})
	if err != nil {
		t.Fatalf("SaveConfig: %v", err)
	}
	if view.BaseURL != "https://new.example/v1" || view.Model != "new-model" || !view.Configured {
		t.Fatalf("view = %+v", view)
	}
	if view.APIKeyMasked == "" || view.APIKeyMasked == "sk-old" {
		t.Fatalf("api key must be masked, got %q", view.APIKeyMasked)
	}
	if got := configs.data["p1"].APIKey; got != "sk-old" {
		t.Fatalf("stored api key = %q, want retained value", got)
	}
	wantSession := chat.SessionID("a1", "p1")
	if _, stopped := runtime.snapshot(); len(stopped) != 1 || stopped[0] != wantSession {
		t.Fatalf("stopped = %v, want %s", stopped, wantSession)
	}
}

func TestReadyRejectsBeforeSpawn(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name    string
		project string
		fixture func(*fakeProjects, *fakeTokens, *fakeConfigs)
		want    string
	}{
		{name: "no project", want: "请先选择一个项目"},
		{
			name:    "unknown project",
			project: "missing",
			want:    "项目不存在或不可见",
		},
		{
			name:    "no key",
			project: "p1",
			fixture: func(p *fakeProjects, _ *fakeTokens, _ *fakeConfigs) {
				p.tenants["p1"] = domain.Tenant{ID: "p1"}
			},
			want: "API Key",
		},
		{
			name:    "no config",
			project: "p1",
			fixture: func(p *fakeProjects, tk *fakeTokens, _ *fakeConfigs) {
				p.tenants["p1"] = domain.Tenant{ID: "p1"}
				tk.tokens = []domain.Token{testToken("k1", "a1", time.Now(), "memos_raw")}
			},
			want: "模型配置",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			projects := &fakeProjects{tenants: map[string]domain.Tenant{}}
			tokens := &fakeTokens{}
			configs := &fakeConfigs{data: map[string]domain.ChatConfig{}}
			if tt.fixture != nil {
				tt.fixture(projects, tokens, configs)
			}
			runtime := &fakeRuntime{}
			svc := newService(t, runtime, configs, projects, tokens)

			err := func() error {
				_, err := svc.Send(context.Background(), tt.project, "a1", "hello", "")
				return err
			}()
			if !errors.Is(err, domain.ErrChatNotReady) {
				t.Fatalf("err = %v, want ErrChatNotReady", err)
			}
			if !strings.Contains(domain.SafeMessage(err), tt.want) {
				t.Fatalf("message = %q, want substring %q", domain.SafeMessage(err), tt.want)
			}
			if sessions, _ := runtime.snapshot(); len(sessions) != 0 {
				t.Fatalf("runtime must not be reached, sessions = %+v", sessions)
			}
		})
	}
}

func TestSendStartsRuntimeWithProjectContext(t *testing.T) {
	t.Parallel()

	projects := &fakeProjects{tenants: map[string]domain.Tenant{"p1": {ID: "p1"}}}
	tokens := &fakeTokens{tokens: []domain.Token{testToken("k1", "a1", time.Now(), "memos_project")}}
	configs := &fakeConfigs{data: map[string]domain.ChatConfig{"p1": {
		TenantID: "p1", BaseURL: "https://gw.example/v1", Model: "m1", APIKey: "sk-model",
	}}}
	runtime := &fakeRuntime{}
	svc := newService(t, runtime, configs, projects, tokens)

	if _, err := svc.Send(context.Background(), "p1", "a1", " 你好 ", ""); err != nil {
		t.Fatalf("Send: %v", err)
	}
	sessions, _ := runtime.snapshot()
	if len(sessions) != 1 {
		t.Fatalf("sessions = %+v", sessions)
	}
	spec := sessions[0]
	if spec.SessionID != chat.SessionID("a1", "p1") || spec.ProjectKey != "memos_project" ||
		spec.APIKey != "sk-model" || spec.TenantID != "p1" {
		t.Fatalf("spec = %+v", spec)
	}
	if sent := runtime.session.sent(); len(sent) != 1 || sent[0] != "你好" {
		t.Fatalf("sent = %v", sent)
	}
}

func TestDefaultConfigUsedWhenProjectHasNone(t *testing.T) {
	t.Parallel()

	now := time.Now()
	projects := &fakeProjects{tenants: map[string]domain.Tenant{"p1": {ID: "p1", Name: "p1"}}}
	tokens := &fakeTokens{tokens: []domain.Token{testToken("t1", "a1", now, "memos_raw")}}
	configs := &fakeConfigs{data: map[string]domain.ChatConfig{}}
	runtime := &fakeRuntime{}
	def := domain.ChatConfig{
		BaseURL: "https://oneapi.example/v1",
		Model:   "DeepSeek-V4.1-Flash",
		APIKey:  "sk-platform-secret",
	}
	svc, err := chat.New(runtime, configs, projects, tokens, chat.Options{
		ContextWindow: 131072,
		MaxTokens:     16384,
		DefaultConfig: def,
	})
	if err != nil {
		t.Fatalf("chat.New: %v", err)
	}

	state, err := svc.State(context.Background(), "p1", "a1")
	if err != nil {
		t.Fatalf("State: %v", err)
	}
	if !state.HasConfig || !state.CanChat {
		t.Fatalf("state = %+v, 期望内置模型直接可用", state)
	}

	view, err := svc.GetConfig(context.Background(), "p1")
	if err != nil {
		t.Fatalf("GetConfig: %v", err)
	}
	if !view.Inherited || view.Model != def.Model {
		t.Fatalf("view = %+v, 期望继承内置模型", view)
	}
	if view.APIKeyMasked == "" || strings.Contains(view.APIKeyMasked, "platform-secret") {
		t.Fatalf("掩码泄漏明文: %q", view.APIKeyMasked)
	}

	if _, err := svc.Send(context.Background(), "p1", "a1", "你好", ""); err != nil {
		t.Fatalf("Send: %v", err)
	}
	specs, _ := runtime.snapshot()
	if len(specs) != 1 || specs[0].BaseURL != def.BaseURL || specs[0].Model != def.Model || specs[0].APIKey != def.APIKey {
		t.Fatalf("spawn spec = %+v, 期望使用内置模型", specs)
	}
}
