package rest

import (
	"context"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/response"
)

// 记忆插件会带 X-GraphMemory-Client（以及同值的 User-Agent），用它把插件流量与
// 直连脚本分开：两者路径相同，只看路径分不出是谁在写。不参与鉴权，只决定
// 审计里的 source —— 冒用这个头最多让自己看起来像插件，拿不到任何权限。
const (
	clientHeader = "X-GraphMemory-Client"
	// hostHeader / agentHeader / agentContextHeader / turnHeader 是插件随每个请求
	// 自报的宿主版本、部署身份、上下文（primary/subagent/...）与轮次序号，只写进审计。
	hostHeader         = "X-GraphMemory-Host"
	agentHeader        = "X-GraphMemory-Agent"
	agentContextHeader = "X-GraphMemory-Agent-Context"
	turnHeader         = "X-GraphMemory-Turn"
	pluginPrefix       = "graphmemory/"
	// metaMaxRunes 是单个自报字段的截断长度，避免客户端用长头部放大存储。
	metaMaxRunes = 64
)

// clientMeta 从请求头取插件自报的审计元数据。
func clientMeta(r *http.Request) (clientVersion, hostVersion, agentIdentity, agentContext string, turn int) {
	clientVersion = ""
	if value := metaHeader(r, clientHeader); strings.HasPrefix(value, pluginPrefix) {
		clientVersion = strings.TrimPrefix(value, pluginPrefix)
	}
	hostVersion = metaHeader(r, hostHeader)
	agentIdentity = metaHeader(r, agentHeader)
	agentContext = metaHeader(r, agentContextHeader)
	if value := metaHeader(r, turnHeader); value != "" {
		if n, err := strconv.Atoi(value); err == nil && n > 0 {
			turn = n
		}
	}
	return clientVersion, hostVersion, agentIdentity, agentContext, turn
}

// metaHeader 取单个头部并做长度与控制字符清理；空串表示没报。
func metaHeader(r *http.Request, name string) string {
	value := strings.TrimSpace(r.Header.Get(name))
	if value == "" {
		return ""
	}
	cleaned := strings.Map(func(c rune) rune {
		if unicode.IsControl(c) {
			return -1
		}
		return c
	}, value)
	if runes := []rune(cleaned); len(runes) > metaMaxRunes {
		cleaned = string(runes[:metaMaxRunes])
	}
	return cleaned
}

// requestSource 判入口：插件自报标识 → plugin，其余 REST 客户端 → rest。
func requestSource(r *http.Request) string {
	// User-Agent 兜底：中间层偶尔会剥自定义头。
	for _, value := range []string{r.Header.Get(clientHeader), r.Header.Get("User-Agent")} {
		if strings.HasPrefix(strings.TrimSpace(value), pluginPrefix) {
			return domain.AuditSourcePlugin
		}
	}
	return domain.AuditSourceREST
}

// wrap 统一处理方法、Content-Type、鉴权、审计与响应编码。
func (s *Server) wrap(toolName string, fn toolHandler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			errorBody(w, http.StatusMethodNotAllowed, "只接受 POST")
			return
		}
		started := time.Now()
		clientVersion, hostVersion, agentIdentity, agentContext, turn := clientMeta(r)
		record := domain.AuditRecord{
			ID:            domain.NewID(),
			CallID:        domain.NewID(),
			TS:            started,
			Tool:          toolName,
			Source:        requestSource(r),
			RemoteIP:      httpx.ClientIPFrom(r.Context()),
			ClientVersion: clientVersion,
			HostVersion:   hostVersion,
			AgentIdentity: agentIdentity,
			AgentContext:  agentContext,
			TurnNumber:    turn,
		}
		// 先鉴权再解析请求体：未认证流量既不解码 4MB body，也不写审计，
		// 避免无凭据请求放大内存与写连接压力（Dashboard/Admin 同样只审计有效主体）。
		caller, err := s.deps.Identity.Authenticate(r.Context(), httpx.BearerToken(r.Header.Get("Authorization")))
		if err != nil {
			status, message := errorStatus(err)
			errorBody(w, status, message)
			return
		}
		record.TenantID = caller.TenantID
		record.UserID = caller.UserID
		body, err := decodeBody(r)
		if err != nil {
			status, message := errorStatus(err)
			errorBody(w, status, message)
			return
		}
		// REST 用标准 Idempotency-Key 头；也允许 body 里直接给 idempotency_key。
		if key := strings.TrimSpace(r.Header.Get("Idempotency-Key")); key != "" {
			body["idempotency_key"] = key
		}
		record.Input = body
		record.SessionID = memory.ResolveSessionID(httpx.String(body["session_id"]), httpx.String(body["conversation_first_message"]))
		if toolName == domain.ToolSearchMemory {
			record.Query = httpx.String(body["query"])
		}
		if toolName == domain.ToolAddMessage {
			like := audit.LooksCaseSpecific(body["messages"])
			record.CaseLike = &like
		}

		result, err := fn(memory.WithEventScope(r.Context(), record.CallID, record.SessionID), caller, body, record.CallID)
		if result.callID != "" {
			record.CallID = result.callID
		}
		record.Ms = time.Since(started).Milliseconds()
		record.Deferred = result.deferred
		record.OK = err == nil
		if err != nil {
			record.Err = errorName(err)
			status, message := errorStatus(err)
			record.ErrMessage = message
			record.HTTPStatus = status
			s.record(r.Context(), record)
			errorBody(w, status, message)
			return
		}
		record.HTTPStatus = http.StatusOK
		result.payload["call_id"] = record.CallID
		output := response.Encode(result.payload)
		record.Output = output
		record.ResultBytes = len(output)
		s.record(r.Context(), record)
		httpx.WriteJSON(w, http.StatusOK, result.payload)
	}
}

func (s *Server) record(ctx context.Context, record domain.AuditRecord) {
	if s.deps.Audit != nil {
		s.deps.Audit.Record(ctx, record)
	}
}

func (s *Server) handleAdd(
	ctx context.Context,
	caller domain.Caller,
	body map[string]any,
	callID string,
) (toolResult, error) {
	messages, err := marshalValue(body["messages"])
	if err != nil {
		return toolResult{}, err
	}
	// 异步：入队即返回，抽取由 worker 继续。REST 的调用方是 agent/插件，等在
	// 上游 LLM 改写上一轮对话（几秒到几十秒）会把整轮交互拖死，而失败本来就有
	// 任务表和死信页兜底。详情见 docs/module/rest.md「异步写入」。
	result, err := s.deps.Memory.Add(ctx, caller, domain.AddRequest{
		FirstMessage:   httpx.String(body["conversation_first_message"]),
		SessionID:      httpx.String(body["session_id"]),
		Messages:       messages,
		Tags:           httpx.StringList(body["tags"]),
		Async:          true,
		IdempotencyKey: httpx.String(body["idempotency_key"]),
		CallID:         callID,
	})
	if err != nil {
		return toolResult{}, err
	}
	return toolResult{payload: response.Add(result), deferred: result.Deferred, callID: result.CallID}, nil
}

func (s *Server) handleSearch(
	ctx context.Context,
	caller domain.Caller,
	body map[string]any,
	_ string,
) (toolResult, error) {
	filter, _ := body["filter"].(map[string]any)
	result, err := s.deps.Memory.Search(ctx, caller, domain.SearchQuery{
		Text:         httpx.String(body["query"]),
		TopK:         httpx.Int(body["top_k"], s.deps.Limits.DefaultTopK),
		FirstMessage: httpx.String(body["conversation_first_message"]),
		SessionID:    httpx.String(body["session_id"]),
		Filter:       filter,
		Mode:         httpx.String(body["mode"]),
	})
	if err != nil {
		return toolResult{}, err
	}
	return toolResult{payload: response.Search(result, s.deps.Limits.DreamTopK > 0)}, nil
}

func (s *Server) handleDelete(
	ctx context.Context,
	caller domain.Caller,
	body map[string]any,
	_ string,
) (toolResult, error) {
	result, err := s.deps.Memory.Delete(ctx, caller, httpx.StringList(body["memory_ids"]))
	if err != nil {
		return toolResult{}, err
	}
	return toolResult{payload: response.Delete(result)}, nil
}

func (s *Server) handleFeedback(
	ctx context.Context,
	caller domain.Caller,
	body map[string]any,
	_ string,
) (toolResult, error) {
	result, err := s.deps.Memory.Feedback(ctx, caller, domain.FeedbackRequest{
		Content:            httpx.String(body["feedback_content"]),
		FirstMessage:       httpx.String(body["conversation_first_message"]),
		SessionID:          httpx.String(body["session_id"]),
		RetrievedMemoryIDs: httpx.StringList(body["retrieved_memory_ids"]),
	})
	if err != nil {
		return toolResult{}, err
	}
	return toolResult{payload: response.Feedback(result)}, nil
}

func (s *Server) handleProfile(
	ctx context.Context,
	caller domain.Caller,
	body map[string]any,
	_ string,
) (toolResult, error) {
	result, err := s.deps.Memory.Profile(ctx, caller, domain.ProfileQuery{
		IncludePreference: httpx.Bool(body["include_preference"], true),
		IncludeToolMemory: httpx.Bool(body["include_tool_memory"], false),
		Page:              httpx.Int(body["current"], 1),
		PageSize:          httpx.Int(body["size"], 20),
	})
	if err != nil {
		return toolResult{}, err
	}
	return toolResult{payload: response.Profile(result)}, nil
}

func errorName(err error) string {
	if kind := domain.KindOf(err); kind != nil {
		return kind.Error()
	}
	return "internal"
}
