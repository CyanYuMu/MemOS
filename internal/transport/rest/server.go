package rest

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// Deps 是 REST 入口需要的依赖。
type Deps struct {
	Memory   *memory.Service
	Identity *identity.Service
	Tenants  Tenants
	Audit    *audit.Recorder
	Logger   *slog.Logger
	Limits   memory.Options
}

// Server 提供 /memory/{add,search,delete,feedback,profile}。
type Server struct {
	deps Deps
	mux  *http.ServeMux
}

// New 构造 REST 入口。
func New(deps Deps) *Server {
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	s := &Server{deps: deps, mux: http.NewServeMux()}
	s.mux.HandleFunc("/memory/add", s.wrap(domain.ToolAddMessage, s.handleAdd))
	// 异步写入的状态查询；call_id 由 /memory/add 的响应返回。
	s.mux.HandleFunc("/memory/search", s.wrap(domain.ToolSearchMemory, s.handleSearch))
	s.mux.HandleFunc("/memory/delete", s.wrap(domain.ToolDeleteMemory, s.handleDelete))
	s.mux.HandleFunc("/memory/update", s.wrap(domain.ToolUpdateMemory, s.handleUpdate))
	s.mux.HandleFunc("/memory/feedback", s.wrap(domain.ToolAddFeedback, s.handleFeedback))
	s.mux.HandleFunc("/memory/profile", s.wrap(domain.ToolGetUserProfile, s.handleProfile))
	// 非工具路径：安装脚本用它确认 token 落在哪个用户 / 项目下；不写审计、不改数据。
	s.mux.HandleFunc("/memory/whoami", s.handleWhoami)
	// 未知路径也回信封；Go 1.22 会优先匹配更具体的 pattern。
	// REST 面的其它错误统一是 {status,message}，未知路径也保持一致；
	// 不要用 dashboard 的信封形状，否则同一客户端要按路径分支解析。
	s.mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		errorBody(w, http.StatusNotFound, "接口不存在")
	})
	return s
}

// Handler 返回挂到 /memory/ 的处理器。
func (s *Server) Handler() http.Handler { return httpx.MethodNotAllowedJSON(s.mux) }

type toolResult struct {
	payload  map[string]any
	deferred bool
	// callID 非空时覆盖审计请求行的 call_id：幂等重放沿用首次请求的 id，
	// 才能对上原任务的异步终态行。
	callID string
}

type toolHandler func(ctx context.Context, caller domain.Caller, body map[string]any, callID string) (toolResult, error)

func errorBody(w http.ResponseWriter, status int, message string) {
	httpx.WriteJSON(w, status, map[string]any{"status": "error", "message": message})
}

func errorStatus(err error) (int, string) {
	switch {
	case errors.Is(err, httpx.ErrBodyTooLarge):
		return http.StatusRequestEntityTooLarge, "请求体过大"
	case errors.Is(err, httpx.ErrNotJSON):
		return http.StatusUnsupportedMediaType, "Content-Type 必须是 application/json"
	case errors.Is(err, httpx.ErrBadJSON):
		return http.StatusBadRequest, "请求体不是合法 JSON"
	case errors.Is(err, domain.ErrInvalidInput):
		return http.StatusBadRequest, err.Error()
	case errors.Is(err, domain.ErrUnauthorized):
		return http.StatusUnauthorized, err.Error()
	case errors.Is(err, domain.ErrForbidden):
		return http.StatusForbidden, err.Error()
	case errors.Is(err, domain.ErrNotFound):
		return http.StatusNotFound, err.Error()
	case errors.Is(err, domain.ErrConflict):
		return http.StatusConflict, err.Error()
	case errors.Is(err, domain.ErrUpstreamRejected):
		return http.StatusBadGateway, err.Error()
	case errors.Is(err, domain.ErrUpstreamDenied):
		return http.StatusBadGateway, err.Error()
	case errors.Is(err, domain.ErrRateLimited):
		return http.StatusTooManyRequests, err.Error()
	case errors.Is(err, domain.ErrUpstreamUnavailable), errors.Is(err, domain.ErrDisabled):
		return http.StatusServiceUnavailable, err.Error()
	default:
		return http.StatusInternalServerError, "内部错误"
	}
}

func decodeBody(r *http.Request) (map[string]any, error) {
	if err := httpx.RequireJSON(r); err != nil {
		return nil, err
	}
	body := map[string]any{}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return nil, err
	}
	return body, nil
}

func marshalValue(value any) (json.RawMessage, error) {
	if value == nil {
		return json.RawMessage("null"), nil
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, domain.Wrap(domain.ErrInvalidInput, "marshal", "参数无法序列化", err)
	}
	return raw, nil
}
