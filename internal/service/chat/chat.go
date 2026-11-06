package chat

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const op = "chat"

// Runtime 是对话会话端口，由 adapter/piagent 实现。
//
// Session 只返回会话对象，不启动进程；启动只发生在 Submit 的派发路径上。
// Available 是可选能力，用于面板如实显示 pi 是否可执行。
type Runtime interface {
	Session(ctx context.Context, spec domain.ChatSessionSpec) (domain.ChatSession, error)
	Stop(ctx context.Context, sessionID string) error
}

// TenantStopper 是可选能力：项目级配置变更后停掉该项目下所有账号的会话。
// 旧实现只有 Stop(sessionID)，保存者以外的人会继续用旧配置。
type TenantStopper interface {
	StopTenant(ctx context.Context, tenantID string) error
}

// ConfigStore 是 BYOK 配置端口，由 adapter/store 实现。
type ConfigStore interface {
	GetChatConfig(ctx context.Context, tenantID string) (domain.ChatConfig, error)
	UpsertChatConfig(ctx context.Context, cfg domain.ChatConfig) error
}

// Projects 是项目注册表端口，由 service/tenant 实现。
type Projects interface {
	Get(ctx context.Context, id string) (domain.Tenant, error)
}

// Tokens 是项目 API Key 端口，由 service/identity 实现。
type Tokens interface {
	ListTokens(ctx context.Context, tenantID string) ([]domain.Token, error)
	GetToken(ctx context.Context, id string) (domain.Token, error)
}

// Availability 是运行时可选的健康报告能力。不实现时 State 认为运行时可用，
// 失败会在真正 Ensure 时以 503 暴露。
type Availability interface {
	Available() bool
}

// Options 是服务构造参数。
type Options struct {
	ContextWindow int
	MaxTokens     int
	Logger        *slog.Logger
	// DefaultConfig 是平台内置对话模型；项目没有自己的 BYOK 配置时直接使用，
	// 用户打开聊天页不需要先填表。APIKey 只来自进程环境，不落库、不回显。
	Conversations ConversationStore
	Feedback      FeedbackStore
	DefaultConfig domain.ChatConfig
}

// Service 提供聊天用例。
type Service struct {
	runtime  Runtime
	configs  ConfigStore
	projects Projects
	tokens   Tokens
	log      *slog.Logger
	opts     Options
	now      func() time.Time
}

// New 构造聊天服务。依赖缺失属于装配错误。
func New(runtime Runtime, configs ConfigStore, projects Projects, tokens Tokens, opts Options) (*Service, error) {
	if runtime == nil || configs == nil || projects == nil || tokens == nil {
		return nil, errors.New("chat: runtime/configs/projects/tokens 不能为空")
	}
	if opts.ContextWindow < 1 || opts.MaxTokens < 1 {
		return nil, errors.New("chat: ContextWindow 与 MaxTokens 必须为正数")
	}
	if err := validateDefaultConfig(opts.DefaultConfig); err != nil {
		return nil, err
	}
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	return &Service{
		runtime:  runtime,
		configs:  configs,
		projects: projects,
		tokens:   tokens,
		log:      opts.Logger,
		opts:     opts,
		now:      time.Now,
	}, nil
}

// KeyView 是 Key 的展示信息，不含明文。
type KeyView struct {
	ID     string `json:"id"`
	Label  string `json:"label"`
	Prefix string `json:"prefix"`
}

// State 是前端判断能否开始对话的全部前置条件。
type State struct {
	ProjectID     string   `json:"projectId"`
	ProjectName   string   `json:"projectName"`
	HasProject    bool     `json:"hasProject"`
	HasAPIKey     bool     `json:"hasApiKey"`
	HasConfig     bool     `json:"hasConfig"`
	CanChat       bool     `json:"canChat"`
	ReadOnly      bool     `json:"readOnly,omitempty"`
	APIKey        *KeyView `json:"apiKey,omitempty"`
	SessionID     string   `json:"sessionId"`
	ContextWindow int      `json:"contextWindow"`
	AutoCompact   bool     `json:"autoCompact"`
	PiAvailable   bool     `json:"piAvailable"`
}

// ConfigView 是 BYOK 配置的对外形状；APIKey 永远只给掩码。
type ConfigView struct {
	BaseURL      string `json:"baseUrl"`
	Model        string `json:"model"`
	APIKeyMasked string `json:"apiKeyMasked"`
	Configured   bool   `json:"configured"`
	// Inherited 表示当前生效的是平台内置模型，项目没有自己的配置。
	Inherited bool `json:"inherited"`
}

// SaveConfigInput 是保存 BYOK 配置的入参；APIKey 为空表示保留原值。
type SaveConfigInput struct {
	BaseURL string
	Model   string
	APIKey  string
}

// SessionID 是（账号，项目）的确定性会话标识：稳定、不可枚举。
// 平台管理员 Token 没有账号 ID，统一归到 AdminTokenUsername，避免所有管理员共用空目录。
func SessionID(accountID, projectID string) string {
	sum := sha256.Sum256([]byte(normalizeAccountID(accountID) + "|" + projectID))
	return "chat-" + hex.EncodeToString(sum[:])[:24]
}

func normalizeAccountID(accountID string) string {
	if strings.TrimSpace(accountID) == "" {
		return domain.AdminTokenUsername
	}
	return accountID
}

// State 返回前置状态。项目为空或不存在时只回 hasProject=false，不报错：
// 页面首次进入还没有选择项目是正常状态。
func (s *Service) State(ctx context.Context, projectID, accountID string) (State, error) {
	state := State{
		ContextWindow: s.opts.ContextWindow,
		AutoCompact:   true,
		PiAvailable:   s.available(),
	}
	projectID = strings.TrimSpace(projectID)
	if projectID == "" {
		return state, nil
	}
	tenant, err := s.projects.Get(ctx, projectID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return state, nil
		}
		return State{}, err
	}
	state.ProjectID = tenant.ID
	state.ProjectName = tenant.Name
	if state.ProjectName == "" {
		state.ProjectName = tenant.ID
	}
	state.HasProject = true
	state.SessionID = SessionID(accountID, tenant.ID)

	token, err := s.pickKey(ctx, tenant.ID, accountID)
	if err != nil {
		return State{}, err
	}
	if token != nil {
		state.HasAPIKey = true
		state.APIKey = &KeyView{ID: token.ID, Label: token.Label, Prefix: token.Prefix}
	}
	cfg, _, err := s.effectiveConfig(ctx, tenant.ID)
	if err != nil {
		return State{}, err
	}
	state.HasConfig = usableConfig(cfg)
	state.CanChat = state.HasProject && state.HasAPIKey && state.HasConfig && state.PiAvailable
	return state, nil
}

// GetConfig 返回当前生效的配置：项目自己的优先，没有时给平台内置模型，
// 并用 inherited 标明来源。
func (s *Service) GetConfig(ctx context.Context, projectID string) (ConfigView, error) {
	projectID = strings.TrimSpace(projectID)
	if projectID == "" {
		return ConfigView{}, domain.Invalid(op, "缺少 projectId")
	}
	cfg, inherited, err := s.effectiveConfig(ctx, projectID)
	if err != nil {
		return ConfigView{}, err
	}
	view := viewOf(cfg)
	view.Inherited = inherited
	return view, nil
}

// effectiveConfig 解析项目当前生效的模型配置。项目配置损坏或为空时回退到
// 平台内置配置，避免页面因为一条坏记录永远进不去。
func (s *Service) effectiveConfig(ctx context.Context, projectID string) (domain.ChatConfig, bool, error) {
	cfg, err := s.configs.GetChatConfig(ctx, projectID)
	if err != nil && !errors.Is(err, domain.ErrNotFound) {
		return domain.ChatConfig{}, false, err
	}
	if err == nil && usableConfig(cfg) {
		return cfg, false, nil
	}
	defaults := s.opts.DefaultConfig
	if usableConfig(defaults) {
		return defaults, true, nil
	}
	return domain.ChatConfig{}, false, nil
}

// SaveConfig 校验并落库 BYOK 配置，然后停止该账号在该项目的进程，
// 让下一次发消息按新配置重建。
func (s *Service) SaveConfig(ctx context.Context, projectID, accountID string, in SaveConfigInput) (ConfigView, error) {
	projectID = strings.TrimSpace(projectID)
	if projectID == "" {
		return ConfigView{}, domain.Invalid(op, "缺少 projectId")
	}
	baseURL := strings.TrimSpace(in.BaseURL)
	model := strings.TrimSpace(in.Model)
	apiKey := strings.TrimSpace(in.APIKey)
	if err := validateBaseURL(baseURL); err != nil {
		return ConfigView{}, err
	}
	if model == "" || len(model) > 200 {
		return ConfigView{}, domain.Invalid(op, "模型名不能为空，且不超过 200 个字符")
	}
	if len(apiKey) > 8192 {
		return ConfigView{}, domain.Invalid(op, "API Key 过长")
	}

	// 留空的 API Key 沿用当前生效值；项目还没有配置时就是平台内置 Key。
	old, _, err := s.effectiveConfig(ctx, projectID)
	if err != nil {
		return ConfigView{}, err
	}
	if apiKey == "" {
		if old.APIKey == "" {
			return ConfigView{}, domain.Invalid(op, "请填写 API Key")
		}
		apiKey = old.APIKey
	}

	cfg := domain.ChatConfig{
		TenantID:  projectID,
		BaseURL:   baseURL,
		Model:     model,
		APIKey:    apiKey,
		UpdatedAt: s.now(),
	}
	if err := s.configs.UpsertChatConfig(ctx, cfg); err != nil {
		return ConfigView{}, err
	}
	sessionID := SessionID(accountID, projectID)
	var stopErr error
	if stopper, ok := s.runtime.(TenantStopper); ok {
		stopErr = stopper.StopTenant(ctx, projectID)
	} else {
		stopErr = s.runtime.Stop(ctx, sessionID)
	}
	if stopErr != nil {
		// 配置已经落库；停止失败只影响下一次重建，不让保存结果变成 500。
		s.log.WarnContext(ctx, "chat stop sessions after config save",
			slog.String("session_id", sessionID), slog.Any("err", stopErr))
	}
	return viewOf(cfg), nil
}

// Messages 返回 pi 的历史消息。默认只给最后 10 条：一次会话的 JSONL 可能很长，
// 首屏全量下发会把浏览器卡住。before 是上一页的绝对起始下标，用于向上渐进加载。
func (s *Service) Messages(ctx context.Context, projectID, accountID string, limit, before int, conversationID string) (json.RawMessage, error) {
	spec, err := s.ready(ctx, projectID, accountID, conversationID)
	if err != nil {
		return nil, err
	}
	session, err := s.runtime.Session(ctx, spec)
	if err != nil {
		return nil, err
	}
	raw, err := session.Messages(ctx)
	if err != nil {
		return nil, err
	}
	return pageMessages(raw, limit, before), nil
}

const (
	defaultMessageLimit = 10
	maxMessageLimit     = 50
)

// pageMessages 按绝对下标切一页历史。before 为上一页的 start（0 表示取最后一页）。
// 页首落在工具结果上时向前多带几条，页尾落在工具调用上时向后多带几条；相邻页因此
// 可能重叠，前端按 toolCallId 与消息 id 去重。
func pageMessages(raw json.RawMessage, limit, before int) json.RawMessage {
	if limit <= 0 {
		limit = defaultMessageLimit
	}
	if limit > maxMessageLimit {
		limit = maxMessageLimit
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err != nil {
		return raw
	}
	var messages []json.RawMessage
	if err := json.Unmarshal(obj["messages"], &messages); err != nil {
		return raw
	}
	total := len(messages)
	end := total
	if before > 0 && before < total {
		end = before
	}
	start := max(end-limit, 0)
	for extra := 0; start > 0 && extra < 3 && isToolResultMessage(messages[start]); extra++ {
		start--
	}
	for extra := 0; end < total && extra < 3 && endsWithToolCall(messages[end-1]); extra++ {
		end++
	}
	page, err := json.Marshal(messages[start:end])
	if err != nil {
		return raw
	}
	obj["messages"] = page
	obj["start"] = json.RawMessage(strconv.Itoa(start))
	obj["total"] = json.RawMessage(strconv.Itoa(total))
	if start > 0 {
		obj["hasMore"] = json.RawMessage("true")
	} else {
		delete(obj, "hasMore")
	}
	out, err := json.Marshal(obj)
	if err != nil {
		return raw
	}
	return out
}

func isToolResultMessage(raw json.RawMessage) bool {
	var message struct {
		Role string `json:"role"`
	}
	if err := json.Unmarshal(raw, &message); err != nil {
		return false
	}
	switch strings.ToLower(message.Role) {
	case "toolresult", "tool_result", "tool":
		return true
	default:
		return false
	}
}

func endsWithToolCall(raw json.RawMessage) bool {
	var message struct {
		Role    string `json:"role"`
		Content []struct {
			Type string `json:"type"`
		} `json:"content"`
	}
	if err := json.Unmarshal(raw, &message); err != nil {
		return false
	}
	if !strings.EqualFold(message.Role, "assistant") {
		return false
	}
	for _, block := range message.Content {
		switch strings.ToLower(block.Type) {
		case "toolcall", "tool_call", "tool_use":
			return true
		}
	}
	return false
}

// Send 受理一条用户消息：落盘并入队后立即返回，不等待 pi 的 prompt 应答。
func (s *Service) Send(ctx context.Context, projectID, accountID, text, conversationID string) (domain.ChatSubmission, error) {
	text = strings.TrimSpace(text)
	if text == "" {
		return domain.ChatSubmission{}, domain.Invalid(op, "请输入要发送的内容")
	}
	if len(text) > 32*1024 {
		return domain.ChatSubmission{}, domain.Invalid(op, "消息过长，请分成多条发送")
	}
	spec, err := s.ready(ctx, projectID, accountID, conversationID)
	if err != nil {
		return domain.ChatSubmission{}, err
	}
	session, err := s.runtime.Session(ctx, spec)
	if err != nil {
		return domain.ChatSubmission{}, err
	}
	return session.Submit(ctx, text)
}

// Abort 中断当前轮并清空排队。进程不存在时只是清空队列，不再拉起进程。
// Retry 重新投递最早一条待重试消息。走的是会话自己的重试队列，避免前端拿
// 最后一条用户消息盲目重发（那会把已经成功的一轮再跑一次）。
func (s *Service) Retry(ctx context.Context, projectID, accountID, conversationID string) (domain.ChatSubmission, error) {
	spec, err := s.ready(ctx, projectID, accountID, conversationID)
	if err != nil {
		return domain.ChatSubmission{}, err
	}
	session, err := s.runtime.Session(ctx, spec)
	if err != nil {
		return domain.ChatSubmission{}, err
	}
	return session.Retry(ctx)
}

func (s *Service) Abort(ctx context.Context, projectID, accountID, conversationID string) error {
	spec, err := s.ready(ctx, projectID, accountID, conversationID)
	if err != nil {
		return err
	}
	session, err := s.runtime.Session(ctx, spec)
	if err != nil {
		return err
	}
	return session.Abort(ctx)
}

// Stream 建立事件订阅。返回的 cancel 只注销订阅，不停进程，也不启动进程。
func (s *Service) Stream(ctx context.Context, projectID, accountID, conversationID string) (<-chan domain.ChatEvent, func(), error) {
	spec, err := s.ready(ctx, projectID, accountID, conversationID)
	if err != nil {
		return nil, nil, err
	}
	session, err := s.runtime.Session(ctx, spec)
	if err != nil {
		return nil, nil, err
	}
	events, cancel := session.Subscribe(ctx)
	return events, cancel, nil
}

// ready 按固定顺序检查三项前置条件，并组装进程启动参数。
// 缺项目、缺 Key、缺配置分别返回同一条 ErrChatNotReady，消息说明缺哪一项。
func (s *Service) ready(ctx context.Context, projectID, accountID, conversationID string) (domain.ChatSessionSpec, error) {
	projectID = strings.TrimSpace(projectID)
	if projectID == "" {
		return domain.ChatSessionSpec{}, domain.Wrap(domain.ErrChatNotReady, op,
			"请先选择一个项目", nil)
	}
	tenant, err := s.projects.Get(ctx, projectID)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return domain.ChatSessionSpec{}, domain.Wrap(domain.ErrChatNotReady, op,
				"项目不存在或不可见，请重新选择项目", nil)
		}
		return domain.ChatSessionSpec{}, err
	}
	token, err := s.pickKey(ctx, tenant.ID, accountID)
	if err != nil {
		return domain.ChatSessionSpec{}, err
	}
	if token == nil {
		return domain.ChatSessionSpec{}, domain.Wrap(domain.ErrChatNotReady, op,
			"请先在项目中创建一个可用的 API Key", nil)
	}
	cfg, _, err := s.effectiveConfig(ctx, tenant.ID)
	if err != nil {
		return domain.ChatSessionSpec{}, err
	}
	if !usableConfig(cfg) {
		return domain.ChatSessionSpec{}, domain.Wrap(domain.ErrChatNotReady, op,
			"请先填写模型配置（baseUrl / model / apiKey）", nil)
	}
	return s.selectConversation(ctx, domain.ChatSessionSpec{
		SessionID:  SessionID(accountID, tenant.ID),
		TenantID:   tenant.ID,
		AccountID:  normalizeAccountID(accountID),
		ProjectKey: token.Raw,
		BaseURL:    cfg.BaseURL,
		Model:      cfg.Model,
		APIKey:     cfg.APIKey,
	}, conversationID)
}

// pickKey 选一把可用于 MCP 的项目 Key：未吊销、未过期、有明文。
// 同项目优先当前账号创建的，其次取最近创建的一把。
func (s *Service) pickKey(ctx context.Context, projectID, accountID string) (*domain.Token, error) {
	tokens, err := s.tokens.ListTokens(ctx, projectID)
	if err != nil {
		return nil, err
	}
	now := s.now()
	var fallback *domain.Token
	for i := range tokens {
		t := &tokens[i]
		if !t.Active() || t.Expired(now) || strings.TrimSpace(t.Raw) == "" {
			continue
		}
		if accountID != "" && t.CreatedByAccountID == accountID {
			return t, nil
		}
		if fallback == nil || newer(t, fallback) {
			fallback = t
		}
	}
	return fallback, nil
}

func newer(a, b *domain.Token) bool {
	if a.CreatedAt.Equal(b.CreatedAt) {
		return a.ID > b.ID
	}
	return a.CreatedAt.After(b.CreatedAt)
}

func usableConfig(cfg domain.ChatConfig) bool {
	return strings.TrimSpace(cfg.BaseURL) != "" &&
		strings.TrimSpace(cfg.Model) != "" &&
		strings.TrimSpace(cfg.APIKey) != ""
}

// validateDefaultConfig 允许完全不配内置模型（此时仍要求项目自配 BYOK），
// 但只要配了一项，三项就必须齐全，否则启动阶段直接失败而不是让页面 409。
func validateDefaultConfig(cfg domain.ChatConfig) error {
	baseURL := strings.TrimSpace(cfg.BaseURL)
	model := strings.TrimSpace(cfg.Model)
	apiKey := strings.TrimSpace(cfg.APIKey)
	if baseURL == "" && model == "" && apiKey == "" {
		return nil
	}
	if err := validateBaseURL(baseURL); err != nil {
		return err
	}
	if model == "" {
		return errors.New("chat: 内置模型名不能为空")
	}
	if apiKey == "" {
		return errors.New("chat: 内置模型 API Key 不能为空")
	}
	return nil
}

func viewOf(cfg domain.ChatConfig) ConfigView {
	return ConfigView{
		BaseURL:      cfg.BaseURL,
		Model:        cfg.Model,
		APIKeyMasked: domain.MaskToken(cfg.APIKey),
		Configured:   usableConfig(cfg),
	}
}

func validateBaseURL(raw string) error {
	if raw == "" {
		return domain.Invalid(op, "请填写 baseUrl")
	}
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return domain.Invalid(op, "baseUrl 必须是 http(s) 地址")
	}
	if u.User != nil {
		return domain.Invalid(op, "baseUrl 不能包含用户名或口令")
	}
	return nil
}

// available 使用运行时的可选能力报告 pi 是否可执行；不实现时按可用处理。
func (s *Service) available() bool {
	if a, ok := s.runtime.(Availability); ok {
		return a.Available()
	}
	return true
}
