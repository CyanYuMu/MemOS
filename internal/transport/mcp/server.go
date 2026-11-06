package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"

	"github.com/mark3labs/mcp-go/server"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// instructions 是服务级提示，逐字取自网关 INSTRUCTIONS。
const instructions = "长期记忆服务。回答用户前先调用 search_memory 检索相关记忆；" +
	"用户明确要求记住某事时调用 add_message。" +
	"写入只沉淀可泛化的口径/规则/流程/偏好，个案判定结论与过程流水不要写入；" +
	"检索到的历史结论仅作背景参考，不得作为本轮判定的依据（判定须基于实时数据独立得出）。"

// invalidTokenDescription 与线上 FastMCP 的 401 响应逐字一致。
// MCP 客户端据此判断凭据失效并重新登记，而不是把鉴权失败当成工具执行错误。
const invalidTokenDescription = "Authentication failed. The provided bearer token is invalid, expired, or no longer recognized by the server. To resolve: clear authentication tokens in your MCP client and reconnect. Your client should automatically re-register and obtain new tokens."

// Deps 是 MCP 适配层需要的依赖。
type Deps struct {
	Memory   *memory.Service
	Identity *identity.Service
	Audit    *audit.Recorder
	Logger   *slog.Logger
	Name     string
	Version  string
	Limits   memory.Options // 只读 top_k 默认值与 dream 开关
}

// Server 是 /mcp 的 Streamable HTTP 适配。
type Server struct {
	deps    Deps
	mcp     *server.MCPServer
	handler http.Handler
}

// New 构造 MCP 服务；依赖缺失时返回错误，避免启动后才发现。
func New(deps Deps) (*Server, error) {
	if deps.Memory == nil || deps.Identity == nil {
		return nil, errors.New("mcp: Memory 与 Identity 不能为空")
	}
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	if deps.Name == "" {
		deps.Name = "memos"
	}
	if deps.Version == "" {
		deps.Version = "dev"
	}

	s := &Server{deps: deps}
	s.mcp = server.NewMCPServer(
		deps.Name,
		deps.Version,
		server.WithInstructions(instructions),
		server.WithRecovery(),
		server.WithToolCapabilities(false),
	)
	s.registerTools()

	httpServer := server.NewStreamableHTTPServer(s.mcp,
		// 与线上 FastMCP 的 stateless_http=True 对齐：不要求客户端回传 Mcp-Session-Id。
		server.WithStateLess(true),
		server.WithHTTPContextFunc(func(ctx context.Context, r *http.Request) context.Context {
			return withChatSession(withAuthorization(ctx, r.Header.Get("Authorization")), r.Header.Get("X-GraphMemory-Chat-Session"))
		}),
		server.WithStreamableHTTPLogger(deps.Logger),
	)
	s.handler = httpServer
	return s, nil
}

// Handler 返回挂在 POST /mcp 的处理器（GET/DELETE 由 mcp-go 处理）。
//
// 鉴权在协议层之前完成：没有有效 token 的 initialize/tools/list 也必须 401，
// 否则客户端会误以为连接成功，直到第一次工具调用才发现凭据失效。
func (s *Server) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, readErr := readAndRestoreBody(r)
		caller, authErr := s.deps.Identity.Authenticate(r.Context(), httpx.BearerToken(r.Header.Get("Authorization")))
		// 原始请求先落归档，再判鉴权：无效 token、坏 JSON 的请求也要能审计。
		s.archiveRequest(r.Context(), archiveInput{
			Caller:  caller,
			Request: r,
			Body:    body,
			ReadErr: readErr,
			AuthErr: authErr,
		})
		if authErr != nil {
			writeAuthError(w, authErr)
			return
		}
		ctx := withCaller(r.Context(), caller)
		// 工具调用由 invoke 写完整审计；initialize/tools/list/ping 等协议帧在这里
		// 只写归档文件，避免把协议心跳灌进控制台的调用日志。
		s.archiveProtocol(ctx, caller, body)
		s.handler.ServeHTTP(w, r.WithContext(ctx))
	})
}

// readAndRestoreBody 读取 POST body 后原样放回，供鉴权后的 mcp-go 继续解析。
// 超过入站上限时保留已读部分和原始错误，下游仍会得到 413，而不是被降级成坏 JSON。
func readAndRestoreBody(r *http.Request) ([]byte, error) {
	if r.Method != http.MethodPost || r.Body == nil {
		return nil, nil
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		r.Body = &replayBody{data: body, err: err}
		return body, err
	}
	r.Body = io.NopCloser(bytes.NewReader(body))
	return body, nil
}

type replayBody struct {
	data []byte
	err  error
}

func (b *replayBody) Read(p []byte) (int, error) {
	if len(b.data) > 0 {
		n := copy(p, b.data)
		b.data = b.data[n:]
		return n, nil
	}
	return 0, b.err
}

func (b *replayBody) Close() error { return nil }

// archiveInput 是一次 MCP 请求的归档入参。
type archiveInput struct {
	Caller  domain.Caller
	Request *http.Request
	Body    []byte
	ReadErr error
	AuthErr error
}

// archiveRequest 把客户端发来的原始 MCP 请求体写进审计归档。
// 只进 JSONL，不进 SQLite；鉴权失败的请求也记录，租户字段留空。
func (s *Server) archiveRequest(ctx context.Context, in archiveInput) {
	caller, r, body := in.Caller, in.Request, in.Body
	readErr, authErr := in.ReadErr, in.AuthErr
	if s.deps.Audit == nil || r.Method != http.MethodPost {
		return
	}
	input := map[string]any{
		"body":         string(body),
		"method":       r.Method,
		"path":         r.URL.Path,
		"content_type": r.Header.Get("Content-Type"),
		"user_agent":   r.Header.Get("User-Agent"),
		"body_bytes":   len(body),
	}
	if readErr != nil {
		input["truncated"] = true
		input["read_error"] = readErr.Error()
	}
	rec := domain.AuditRecord{
		Tool:       "mcp.request",
		Source:     domain.AuditSourceMCP,
		RemoteIP:   httpx.ClientIPFrom(ctx),
		OK:         authErr == nil && readErr == nil,
		HTTPStatus: http.StatusOK,
		Input:      input,
	}
	if authErr == nil {
		rec.TenantID = caller.TenantID
		rec.UserID = caller.UserID
	}
	switch {
	case authErr != nil:
		if errors.Is(authErr, domain.ErrUnauthorized) || errors.Is(authErr, domain.ErrForbidden) {
			rec.HTTPStatus = http.StatusUnauthorized
		} else {
			rec.HTTPStatus = http.StatusServiceUnavailable
		}
		rec.Err = errorName(authErr)
	case readErr != nil:
		rec.HTTPStatus = http.StatusRequestEntityTooLarge
		rec.Err = "body_too_large"
	}
	s.deps.Audit.RecordArchive(ctx, rec)
}

// archiveProtocol 把非 tools/call 的 JSON-RPC 方法追加到审计归档。
// body 已由 Handler 读取并还原，这里只解析，不再读请求体；批量请求逐个方法处理。
func (s *Server) archiveProtocol(ctx context.Context, caller domain.Caller, body []byte) {
	if s.deps.Audit == nil || len(bytes.TrimSpace(body)) == 0 {
		return
	}
	var one struct {
		Method string `json:"method"`
		Params any    `json:"params"`
	}
	if err := json.Unmarshal(body, &one); err == nil && one.Method != "" {
		s.recordProtocol(ctx, caller, one.Method, one.Params)
		return
	}
	var batch []struct {
		Method string `json:"method"`
		Params any    `json:"params"`
	}
	if err := json.Unmarshal(body, &batch); err != nil {
		return
	}
	for _, item := range batch {
		s.recordProtocol(ctx, caller, item.Method, item.Params)
	}
}

func (s *Server) recordProtocol(ctx context.Context, caller domain.Caller, method string, params any) {
	if method == "" || method == "tools/call" {
		return
	}
	input := map[string]any{"method": method}
	if params != nil {
		input["params"] = params
	}
	s.deps.Audit.RecordArchive(ctx, domain.AuditRecord{
		Tool:       "mcp." + method,
		Source:     domain.AuditSourceMCP,
		TenantID:   caller.TenantID,
		UserID:     caller.UserID,
		OK:         true,
		HTTPStatus: http.StatusOK,
		Input:      input,
	})
}

// writeAuthError 把凭据错误与存储故障分开：前者 401（与线上一致），
// 后者 503，避免把后端故障伪装成 token 无效。
func writeAuthError(w http.ResponseWriter, err error) {
	if errors.Is(err, domain.ErrUnauthorized) || errors.Is(err, domain.ErrForbidden) {
		w.Header().Set("WWW-Authenticate", `Bearer error="invalid_token", error_description="`+invalidTokenDescription+`"`)
		httpx.WriteJSON(w, http.StatusUnauthorized, map[string]string{
			"error":             "invalid_token",
			"error_description": invalidTokenDescription,
		})
		return
	}
	httpx.WriteJSON(w, http.StatusServiceUnavailable, map[string]string{
		"error":             "temporarily_unavailable",
		"error_description": "Authentication is temporarily unavailable. Retry later.",
	})
}

type callerKey struct{}

func withCaller(ctx context.Context, c domain.Caller) context.Context {
	return context.WithValue(ctx, callerKey{}, c)
}

func callerFrom(ctx context.Context) (domain.Caller, bool) {
	c, ok := ctx.Value(callerKey{}).(domain.Caller)
	return c, ok
}

// caller 优先复用 HTTP 层已认证的调用方；直接调用工具 handler 的测试没有该上下文，
// 仍按 Authorization 头解析一次。
func (s *Server) caller(ctx context.Context) (domain.Caller, error) {
	if c, ok := callerFrom(ctx); ok {
		return c, nil
	}
	return s.deps.Identity.Authenticate(ctx, token(ctx))
}

// MCPServer 暴露底层服务，供 golden 测试读取工具清单。
func (s *Server) MCPServer() *server.MCPServer { return s.mcp }

type authorizationKey struct{}

func withAuthorization(ctx context.Context, header string) context.Context {
	return context.WithValue(ctx, authorizationKey{}, header)
}

// token 从 HTTP 上下文里取 Bearer token；工具参数不参与身份判定。
func token(ctx context.Context) string {
	header, _ := ctx.Value(authorizationKey{}).(string)
	return httpx.BearerToken(header)
}
