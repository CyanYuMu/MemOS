package chat

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/chat"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// 心跳间隔：15s 没有事件就写一行注释，避免中间层把空闲连接判死。
const pingInterval = 15 * time.Second

// AuditSink 是审计写入端口；*audit.Recorder 实现，测试可以注入假实现。
type AuditSink interface {
	Record(ctx context.Context, rec domain.AuditRecord)
}

// Deps 是聊天路由的依赖。
type Deps struct {
	Chat     *chat.Service
	Accounts auth.Authenticator
	Tokens   auth.KeyLookup
	// Audit 记录配置变更与对话调用。负载只放元数据，绝不写 API Key 明文与消息正文。
	Audit  AuditSink
	Logger *slog.Logger
}

// Server 提供 /api/chat/* 路由。
type Server struct {
	deps Deps
	mux  *http.ServeMux
}

// New 构造聊天路由。
func New(deps Deps) (*Server, error) {
	if deps.Chat == nil || deps.Accounts == nil {
		return nil, errors.New("transport/chat: Chat 与 Accounts 不能为空")
	}
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	s := &Server{deps: deps, mux: http.NewServeMux()}
	s.register()
	s.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) { httpx.NotFoundJSON(w) })
	return s, nil
}

// Handler 返回挂到 console 角色的处理器。
func (s *Server) Handler() http.Handler { return httpx.MethodNotAllowedJSON(s.mux) }

type handler func(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any, projectID string) (any, error)

func (s *Server) register() {
	base := "/api/chat/"
	s.mux.HandleFunc("GET "+base+"conversations", s.wrap(auth.InBodyStrict(domain.CapProjectView), http.StatusOK, s.conversations))
	s.mux.HandleFunc("POST "+base+"conversations", s.wrap(auth.InBodyStrict(domain.CapProjectWrite), http.StatusCreated, s.newConversation))
	s.mux.HandleFunc("PUT "+base+"conversations/{conversationID}/archive", s.wrap(auth.InBodyStrict(domain.CapProjectWrite), http.StatusOK, s.archiveConversation))
	s.mux.HandleFunc("GET "+base+"state", s.wrap(auth.Authed, http.StatusOK, s.state))
	s.mux.HandleFunc("GET "+base+"config", s.wrap(auth.InBodyStrict(domain.CapProjectView), http.StatusOK, s.getConfig))
	s.mux.HandleFunc("PUT "+base+"config", s.wrap(auth.InBodyStrict(domain.CapProjectWrite), http.StatusOK, s.saveConfig))
	s.mux.HandleFunc("GET "+base+"messages", s.wrap(auth.InBodyStrict(domain.CapProjectView), http.StatusOK, s.messages))
	s.mux.HandleFunc("GET "+base+"stream", s.stream)
	s.mux.HandleFunc("GET "+base+"feedback", s.wrap(auth.InBodyStrict(domain.CapProjectView), http.StatusOK, s.feedback))
	s.mux.HandleFunc("PUT "+base+"feedback", s.wrap(auth.InBodyStrict(domain.CapProjectView), http.StatusOK, s.rateAnswer))
	s.mux.HandleFunc("POST "+base+"messages", s.wrap(auth.InBodyStrict(domain.CapProjectWrite), http.StatusAccepted, s.send))
	s.mux.HandleFunc("POST "+base+"retry", s.wrap(auth.InBodyStrict(domain.CapProjectWrite), http.StatusAccepted, s.retry))
	s.mux.HandleFunc("POST "+base+"abort", s.wrap(auth.InBodyStrict(domain.CapProjectView), http.StatusOK, s.abort))
}

func (s *Server) wrap(rule auth.Rule, status int, fn handler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p, err := rule.Resolve(r.Context(), s.deps.Accounts, r)
		if err != nil {
			s.writeError(w, err)
			return
		}
		body, err := decodeBody(r)
		if err != nil {
			s.writeError(w, err)
			return
		}
		projectID, err := rule.Authorize(r.Context(), auth.AuthzInput{Principal: p, Request: r, Body: body, Keys: s.deps.Tokens})
		if err != nil {
			s.writeError(w, err)
			return
		}
		payload, err := fn(r.Context(), p, r, body, projectID)
		if err != nil {
			s.writeError(w, err)
			return
		}
		if status == http.StatusOK {
			httpx.OK(w, payload)
			return
		}
		httpx.WriteJSON(w, status, httpx.Envelope{Code: httpx.CodeOK, Data: payload, Message: "success"})
	}
}

// state 是唯一允许“不可见项目按未选择处理”的接口：页面第一次进入时
// 还没选项目，返回 200 与 hasProject=false 比 403 更符合产品语义。
func (s *Server) state(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any, _ string) (any, error) {
	projectID := strings.TrimSpace(r.URL.Query().Get("projectId"))
	if projectID != "" && !p.Can(projectID, domain.CapProjectView) {
		projectID = ""
	}
	state, err := s.deps.Chat.State(ctx, projectID, p.AccountID)
	if state.HasProject && !p.Can(projectID, domain.CapProjectWrite) {
		state.ReadOnly = true
		state.CanChat = false
	}
	return state, err
}

func (s *Server) getConfig(ctx context.Context, _ domain.Principal, _ *http.Request, _ map[string]any, projectID string) (any, error) {
	return s.deps.Chat.GetConfig(ctx, projectID)
}

func (s *Server) saveConfig(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any, projectID string) (any, error) {
	input := chat.SaveConfigInput{
		BaseURL: httpx.String(body["baseUrl"]),
		Model:   httpx.String(body["model"]),
		APIKey:  httpx.String(body["apiKey"]),
	}
	view, err := s.deps.Chat.SaveConfig(ctx, projectID, p.AccountID, input)
	if err != nil {
		return nil, err
	}
	if s.deps.Audit != nil {
		s.deps.Audit.Record(ctx, domain.AuditRecord{
			ID:         domain.NewID(),
			CallID:     domain.NewID(),
			TS:         time.Now(),
			Tool:       "chat.config.save",
			Source:     domain.AuditSourceDashboard,
			UserID:     p.Actor(),
			TenantID:   projectID,
			Input:      map[string]any{"baseUrl": input.BaseURL, "model": input.Model},
			OK:         true,
			HTTPStatus: http.StatusOK,
		})
	}
	return view, nil
}

func (s *Server) messages(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any, projectID string) (any, error) {
	limit := 10
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n <= 0 {
			return nil, domain.Invalid("chat", "limit 必须是正整数")
		}
		limit = n
	}
	before := 0
	if raw := strings.TrimSpace(r.URL.Query().Get("before")); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 0 {
			return nil, domain.Invalid("chat", "before 必须是非负整数")
		}
		before = n
	}
	return s.deps.Chat.Messages(ctx, projectID, p.AccountID, limit, before, r.URL.Query().Get("conversationId"))
}

func (s *Server) send(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any, projectID string) (any, error) {
	text := httpx.String(body["text"])
	res, err := s.deps.Chat.Send(ctx, projectID, p.AccountID, text, httpx.String(body["conversationId"]))
	s.recordChatCall(ctx, p, r, projectID, "chat.send", map[string]any{
		// 审计只放可聚合的元数据：正文属于用户对话内容，留在会话 transcript 里。
		"conversationId": httpx.String(body["conversationId"]),
		"chars":          len([]rune(text)),
		"msg":            submissionID(res),
		"queued":         submissionQueued(res),
		"running":        true,
	}, err)
	return res, err
}

func (s *Server) retry(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any, projectID string) (any, error) {
	res, err := s.deps.Chat.Retry(ctx, projectID, p.AccountID, httpx.String(body["conversationId"]))
	s.recordChatCall(ctx, p, r, projectID, "chat.retry", map[string]any{"msg": submissionID(res), "conversationId": httpx.String(body["conversationId"])}, err)
	return res, err
}

func (s *Server) abort(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any, projectID string) (any, error) {
	err := s.deps.Chat.Abort(ctx, projectID, p.AccountID, httpx.String(body["conversationId"]))
	s.recordChatCall(ctx, p, r, projectID, "chat.abort", map[string]any{"conversationId": httpx.String(body["conversationId"])}, err)
	return nil, err
}

// recordChatCall 记录一次对话调用。管理员 Token 调用会带 principal=admin_token，
// 便于和普通账号区分开（两者共享 admin-token 会话目录）。
func (s *Server) recordChatCall(ctx context.Context, p domain.Principal, _ *http.Request, projectID, tool string, input map[string]any, err error) {
	if s.deps.Audit == nil {
		return
	}
	if input == nil {
		input = map[string]any{}
	}
	input["principal"] = string(p.Kind)
	status := http.StatusAccepted
	record := domain.AuditRecord{
		ID:         domain.NewID(),
		CallID:     domain.NewID(),
		TS:         time.Now(),
		Tool:       tool,
		Source:     domain.AuditSourceDashboard,
		UserID:     p.Actor(),
		TenantID:   projectID,
		SessionID:  chat.SessionID(p.AccountID, projectID),
		Input:      input,
		OK:         err == nil,
		HTTPStatus: status,
		RemoteIP:   httpx.ClientIPFrom(ctx),
	}
	if tool == "chat.abort" {
		record.HTTPStatus = http.StatusOK
	}
	if err != nil {
		record.HTTPStatus = 0
		record.Err = domain.SafeMessage(err)
		record.ErrMessage = domain.SafeMessage(err)
	}
	if id := httpx.String(input["conversationId"]); id != "" {
		record.SessionID = chat.SessionID(p.AccountID, projectID+"|conversation:"+id)
	}
	s.deps.Audit.Record(ctx, record)
}

func submissionID(v any) string {
	sub, ok := v.(domain.ChatSubmission)
	if !ok {
		return ""
	}
	return sub.ID
}

func submissionQueued(v any) bool {
	sub, ok := v.(domain.ChatSubmission)
	return ok && sub.Queued
}

// stream 在建连前完成鉴权与前置校验，失败回 JSON 而不是 SSE。
func (s *Server) stream(w http.ResponseWriter, r *http.Request) {
	rule := auth.InBodyStrict(domain.CapProjectView)
	p, err := rule.Resolve(r.Context(), s.deps.Accounts, r)
	if err != nil {
		s.writeError(w, err)
		return
	}
	projectID, err := rule.Authorize(r.Context(), auth.AuthzInput{Principal: p, Request: r, Body: map[string]any{}, Keys: s.deps.Tokens})
	if err != nil {
		s.writeError(w, err)
		return
	}
	events, cancel, err := s.deps.Chat.Stream(r.Context(), projectID, p.AccountID, r.URL.Query().Get("conversationId"))
	if err != nil {
		s.writeError(w, err)
		return
	}
	defer cancel()

	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	// 反向代理可能缓冲响应；显式要求不缓冲，事件才能逐条到达浏览器。
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	rc := http.NewResponseController(w)

	ticker := time.NewTicker(pingInterval)
	defer ticker.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case event, ok := <-events:
			if !ok {
				return
			}
			if err := writeEvent(w, rc, event); err != nil {
				return
			}
		case <-ticker.C:
			if _, err := fmt.Fprint(w, ": ping\n\n"); err != nil {
				return
			}
			if err := rc.Flush(); err != nil {
				return
			}
		}
	}
}

func writeEvent(w http.ResponseWriter, rc *http.ResponseController, event domain.ChatEvent) error {
	data := event.Data
	if len(data) == 0 {
		data = json.RawMessage("null")
	}
	body, err := json.Marshal(struct {
		Seq          int64           `json:"seq"`
		Type         string          `json:"type"`
		Data         json.RawMessage `json:"data"`
		SubmissionID string          `json:"submissionId,omitempty"`
	}{Seq: event.Seq, Type: event.Type, Data: data, SubmissionID: event.SubmissionID})
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "data: %s\n\n", body); err != nil {
		return err
	}
	return rc.Flush()
}

func decodeBody(r *http.Request) (map[string]any, error) {
	body := map[string]any{}
	if r.Method != http.MethodPost && r.Method != http.MethodPut {
		return body, nil
	}
	if err := httpx.RequireJSON(r); err != nil {
		return nil, err
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return nil, err
	}
	return body, nil
}

func (s *Server) writeError(w http.ResponseWriter, err error) {
	status, code, message := mapError(err)
	if status >= http.StatusInternalServerError {
		s.deps.Logger.Error("chat request failed", slog.Any("err", err))
	}
	httpx.Fail(w, status, code, message)
}

func mapError(err error) (int, int, string) {
	switch {
	case errors.Is(err, httpx.ErrBodyTooLarge):
		return http.StatusRequestEntityTooLarge, httpx.CodeBadRequest, "请求体过大"
	case errors.Is(err, httpx.ErrNotJSON):
		return http.StatusUnsupportedMediaType, httpx.CodeBadRequest, "Content-Type 必须是 application/json"
	case errors.Is(err, httpx.ErrBadJSON):
		return http.StatusBadRequest, httpx.CodeBadRequest, "请求体不是合法 JSON"
	case errors.Is(err, domain.ErrChatNotReady):
		return http.StatusConflict, httpx.CodeConflict, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrChatBusy):
		return http.StatusTooManyRequests, httpx.CodeRateLimited, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrChatUnavailable):
		return http.StatusServiceUnavailable, httpx.CodeUnavailable, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrInvalidInput):
		return http.StatusBadRequest, httpx.CodeBadRequest, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrUnauthorized):
		return http.StatusUnauthorized, httpx.CodeUnauthorized, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrForbidden):
		return http.StatusForbidden, httpx.CodeForbidden, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrNotFound):
		return http.StatusNotFound, httpx.CodeNotFound, domain.SafeMessage(err)
	case errors.Is(err, domain.ErrConflict):
		return http.StatusConflict, httpx.CodeConflict, domain.SafeMessage(err)
	default:
		return http.StatusInternalServerError, httpx.CodeInternal, "内部错误"
	}
}

func (s *Server) feedback(ctx context.Context, p domain.Principal, r *http.Request, _ map[string]any, project string) (any, error) {
	return s.deps.Chat.AnswerFeedback(ctx, project, p.AccountID, r.URL.Query().Get("responseId"))
}

func (s *Server) rateAnswer(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any, project string) (any, error) {
	vote, ok := body["vote"].(float64)
	if !ok || (vote != -1 && vote != 0 && vote != 1) {
		return nil, domain.Invalid("chat_feedback", "请选择赞同或不赞同")
	}
	_, details := body["comment"]
	f := domain.ChatAnswerFeedback{Vote: int(vote), Comment: httpx.String(body["comment"]), ConversationID: httpx.String(body["conversationId"]), AnswerExcerpt: httpx.String(body["answerExcerpt"])}
	err := s.deps.Chat.SaveAnswerFeedback(ctx, project, p.AccountID, httpx.String(body["responseId"]), f, details)
	return map[string]any{"vote": int(vote)}, err
}
