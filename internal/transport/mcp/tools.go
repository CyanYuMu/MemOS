package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	mcpgo "github.com/mark3labs/mcp-go/mcp"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/response"
)

var (
	addMessageSchema = json.RawMessage(`{
  "type": "object",
  "properties": {
    "conversation_first_message": {
      "type": "string"
    },
    "messages": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "role": {"type": "string", "enum": ["user", "assistant"]},
          "content": {"type": "string", "minLength": 1}
        },
        "required": ["role", "content"],
        "additionalProperties": false
      }
    },
    "tags": {
      "anyOf": [
        {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "session_id": {
      "default": "",
      "description": "Optional conversation identifier. If provided, it takes precedence over conversation_first_message; use the same value within one conversation.",
      "type": "string"
    }
  },
  "required": [
    "conversation_first_message",
    "messages"
  ]
}`)

	searchMemorySchema = json.RawMessage(`{
  "type": "object",
  "properties": {
    "query": {
      "type": "string"
    },
    "filter": {
      "anyOf": [
        {
          "type": "object"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "top_k": {
      "default": 10,
      "type": "integer"
    },
    "conversation_first_message": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "session_id": {
      "default": "",
      "description": "Optional conversation identifier. If provided, it takes precedence over conversation_first_message; use the same value within one conversation.",
      "type": "string"
    }
  },
  "required": [
    "query"
  ]
}`)

	deleteMemorySchema = json.RawMessage(`{
  "type": "object",
  "properties": {
    "memory_ids": {
      "items": {
        "type": "string"
      },
      "type": "array"
    }
  },
  "required": [
    "memory_ids"
  ]
}`)

	addFeedbackSchema = json.RawMessage(`{
  "type": "object",
  "properties": {
    "feedback_content": {
      "type": "string"
    },
    "conversation_first_message": {
      "default": "",
      "type": "string"
    },
    "retrieved_memory_ids": {
      "anyOf": [
        {
          "items": {
            "type": "string"
          },
          "type": "array"
        },
        {
          "type": "null"
        }
      ],
      "default": null
    },
    "session_id": {
      "default": "",
      "description": "Optional conversation identifier. If provided, it takes precedence over conversation_first_message; use the same value within one conversation.",
      "type": "string"
    }
  },
  "required": [
    "feedback_content"
  ]
}`)

	getUserProfileSchema = json.RawMessage(`{
  "type": "object",
  "properties": {
    "include_preference": {
      "default": true,
      "type": "boolean"
    },
    "include_tool_memory": {
      "default": false,
      "type": "boolean"
    },
    "current": {
      "default": 1,
      "type": "integer"
    },
    "size": {
      "default": 20,
      "type": "integer"
    }
  }
}`)
)

// searchSchemaDefaultTopK 是 schema 里写死的 top_k 默认值；配置改了默认值时按配置改写，
// 否则 MCP 对外声明与实际行为不一致。
const searchSchemaDefaultTopK = 10

// searchSchemaWithDefaultTopK 按配置改写 top_k 默认值，失败时退回原始 schema。
func searchSchemaWithDefaultTopK(topK int) json.RawMessage {
	if topK <= 0 || topK == searchSchemaDefaultTopK {
		return searchMemorySchema
	}
	var doc map[string]any
	if err := json.Unmarshal(searchMemorySchema, &doc); err != nil {
		return searchMemorySchema
	}
	props, ok := doc["properties"].(map[string]any)
	if !ok {
		return searchMemorySchema
	}
	target, ok := props["top_k"].(map[string]any)
	if !ok {
		return searchMemorySchema
	}
	target["default"] = topK
	out, err := json.Marshal(doc)
	if err != nil {
		return searchMemorySchema
	}
	return out
}

func (s *Server) registerTools() {
	searchDescription := searchMemoryDescription
	if s.deps.Limits.DreamTopK > 0 {
		searchDescription = searchMemoryDreamDescription
	}
	searchSchema := searchSchemaWithDefaultTopK(s.deps.Limits.DefaultTopK)
	s.mcp.AddTool(
		mcpgo.NewToolWithRawSchema(domain.ToolAddMessage, addMessageDescription, addMessageSchema),
		s.handleAddMessage,
	)
	s.mcp.AddTool(
		mcpgo.NewToolWithRawSchema(domain.ToolSearchMemory, searchDescription, searchSchema),
		s.handleSearchMemory,
	)
	s.mcp.AddTool(
		mcpgo.NewToolWithRawSchema(domain.ToolUpdateMemory, updateMemoryDescription, updateMemorySchema),
		s.handleUpdateMemory,
	)
	s.mcp.AddTool(
		mcpgo.NewToolWithRawSchema(domain.ToolDeleteMemory, deleteMemoryDescription, deleteMemorySchema),
		s.handleDeleteMemory,
	)
	s.mcp.AddTool(
		mcpgo.NewToolWithRawSchema(domain.ToolAddFeedback, addFeedbackDescription, addFeedbackSchema),
		s.handleAddFeedback,
	)
	s.mcp.AddTool(
		mcpgo.NewToolWithRawSchema(domain.ToolGetUserProfile, getUserProfileDescription, getUserProfileSchema),
		s.handleGetUserProfile,
	)
}

func (s *Server) handleAddMessage(ctx context.Context, req mcpgo.CallToolRequest) (*mcpgo.CallToolResult, error) {
	return s.invoke(ctx, domain.ToolAddMessage, req, func(
		ctx context.Context,
		caller domain.Caller,
		callID string,
	) (map[string]any, bool, string, error) {
		raw, err := json.Marshal(req.GetArguments()["messages"])
		if err != nil {
			return nil, false, "", domain.Wrap(domain.ErrInvalidInput, "add_message", "messages 无法序列化", err)
		}
		// 调用方不需要幂等键：service 按归一后的请求内容自动去重。
		result, err := s.deps.Memory.Add(ctx, caller, domain.AddRequest{
			FirstMessage: req.GetString("conversation_first_message", ""),
			SessionID:    sessionIDFor(ctx, req.GetString("session_id", "")),
			Messages:     raw,
			Tags:         stringList(req.GetArguments()["tags"]),
			Async:        true,
			CallID:       callID,
		})
		if err != nil {
			return nil, false, "", err
		}
		return response.Add(result), result.Deferred, result.CallID, nil
	})
}

func (s *Server) handleSearchMemory(ctx context.Context, req mcpgo.CallToolRequest) (*mcpgo.CallToolResult, error) {
	return s.invoke(ctx, domain.ToolSearchMemory, req, func(
		ctx context.Context,
		caller domain.Caller,
		_ string,
	) (map[string]any, bool, string, error) {
		args := req.GetArguments()
		filter, _ := args["filter"].(map[string]any)
		result, err := s.deps.Memory.Search(ctx, caller, domain.SearchQuery{
			Text:         req.GetString("query", ""),
			TopK:         req.GetInt("top_k", s.deps.Limits.DefaultTopK),
			FirstMessage: req.GetString("conversation_first_message", ""),
			SessionID:    sessionIDFor(ctx, req.GetString("session_id", "")),
			Filter:       filter,
		})
		if err != nil {
			return nil, false, "", err
		}
		return response.Search(result, s.deps.Limits.DreamTopK > 0), false, "", nil
	})
}

func (s *Server) handleDeleteMemory(ctx context.Context, req mcpgo.CallToolRequest) (*mcpgo.CallToolResult, error) {
	return s.invoke(ctx, domain.ToolDeleteMemory, req, func(
		ctx context.Context,
		caller domain.Caller,
		_ string,
	) (map[string]any, bool, string, error) {
		result, err := s.deps.Memory.Delete(ctx, caller, stringList(req.GetArguments()["memory_ids"]))
		if err != nil {
			return nil, false, "", err
		}
		return response.Delete(result), false, "", nil
	})
}

func (s *Server) handleAddFeedback(ctx context.Context, req mcpgo.CallToolRequest) (*mcpgo.CallToolResult, error) {
	return s.invoke(ctx, domain.ToolAddFeedback, req, func(
		ctx context.Context,
		caller domain.Caller,
		_ string,
	) (map[string]any, bool, string, error) {
		args := req.GetArguments()
		result, err := s.deps.Memory.Feedback(ctx, caller, domain.FeedbackRequest{
			Content:            req.GetString("feedback_content", ""),
			FirstMessage:       req.GetString("conversation_first_message", ""),
			SessionID:          sessionIDFor(ctx, req.GetString("session_id", "")),
			RetrievedMemoryIDs: stringList(args["retrieved_memory_ids"]),
		})
		if err != nil {
			return nil, false, "", err
		}
		return response.Feedback(result), false, "", nil
	})
}

func (s *Server) handleGetUserProfile(ctx context.Context, req mcpgo.CallToolRequest) (*mcpgo.CallToolResult, error) {
	return s.invoke(ctx, domain.ToolGetUserProfile, req, func(
		ctx context.Context,
		caller domain.Caller,
		_ string,
	) (map[string]any, bool, string, error) {
		result, err := s.deps.Memory.Profile(ctx, caller, domain.ProfileQuery{
			IncludePreference: req.GetBool("include_preference", true),
			IncludeToolMemory: req.GetBool("include_tool_memory", false),
			Page:              req.GetInt("current", 1),
			PageSize:          req.GetInt("size", 20),
		})
		if err != nil {
			return nil, false, "", err
		}
		return response.Profile(result), false, "", nil
	})
}

// invoke 统一处理鉴权、计时、审计与输出编码；审计失败不影响工具结果。
func (s *Server) invoke(
	ctx context.Context,
	toolName string,
	req mcpgo.CallToolRequest,
	fn func(context.Context, domain.Caller, string) (map[string]any, bool, string, error),
) (*mcpgo.CallToolResult, error) {
	started := time.Now()
	args := req.GetArguments()
	record := domain.AuditRecord{
		ID:        domain.NewID(),
		CallID:    domain.NewID(),
		TS:        started,
		Tool:      toolName,
		Source:    domain.AuditSourceMCP,
		RemoteIP:  httpx.ClientIPFrom(ctx),
		SessionID: memory.ResolveSessionID(sessionIDFor(ctx, req.GetString("session_id", "")), req.GetString("conversation_first_message", "")),
		Input:     args,
	}
	if toolName == domain.ToolSearchMemory {
		record.Query = req.GetString("query", "")
	}
	if toolName == domain.ToolAddMessage {
		like := audit.LooksCaseSpecific(args["messages"])
		record.CaseLike = &like
	}

	caller, err := s.caller(ctx)
	var payload map[string]any
	var deferred bool
	var resultCallID string
	if err == nil {
		record.TenantID = caller.TenantID
		record.UserID = caller.UserID
		payload, deferred, resultCallID, err = fn(memory.WithEventScope(ctx, record.CallID, record.SessionID), caller, record.CallID)
	}
	// 幂等重放沿用首次请求的 call_id：service 把实际 job ID 放在结果里，
	// 覆盖后重放请求行才能对上原任务的终态行。
	if resultCallID != "" {
		record.CallID = resultCallID
	}
	record.Ms = time.Since(started).Milliseconds()
	record.Deferred = deferred
	record.OK = err == nil
	// MCP 的工具失败仍是 HTTP 200 + JSON-RPC 错误内容；HTTPStatus 记录真实传输状态，
	// 业务成败看 OK/Status，错误文案单独放 ErrMessage。
	record.HTTPStatus = http.StatusOK
	if err != nil {
		record.Err = errorName(err)
		record.ErrMessage = toolMessage(err)
		// 未认证请求不写审计（与 Dashboard/Admin 一致），避免无凭据流量放大写队列。
		if record.UserID != "" && s.deps.Audit != nil {
			s.deps.Audit.Record(ctx, record)
		}
		result := mcpgo.NewToolResultError(toolMessage(err))
		if record.UserID != "" {
			// 保留原错误文本；附加关联块供历史和侧栏合并同次失败。
			result.Content = append(result.Content, mcpgo.TextContent{Type: "text", Text: response.Encode(map[string]any{
				"status": "failed", "call_id": record.CallID,
			})})
		}
		return result, nil
	}

	payload["call_id"] = record.CallID
	output := response.Encode(payload)
	record.Output = output
	record.ResultBytes = len(output)
	if s.deps.Audit != nil {
		s.deps.Audit.Record(ctx, record)
	}
	return mcpgo.NewToolResultText(output), nil
}

// toolMessage 只把调用方可据以自我修正的错误原样返回；内部错误统一收口，
// 避免把上游地址、SQL 细节等暴露给 MCP 客户端。
func toolMessage(err error) string {
	for _, kind := range []error{
		domain.ErrInvalidInput, domain.ErrUnauthorized, domain.ErrForbidden,
		domain.ErrNotFound, domain.ErrConflict, domain.ErrUpstreamRejected,
		domain.ErrDisabled, domain.ErrRateLimited,
	} {
		if errors.Is(err, kind) {
			return err.Error()
		}
	}
	return "内部错误，请稍后重试"
}

func errorName(err error) string {
	if kind := domain.KindOf(err); kind != nil {
		return kind.Error()
	}
	return fmt.Sprintf("%T", err)
}

func stringList(value any) []string {
	switch typed := value.(type) {
	case string:
		if typed = strings.TrimSpace(typed); typed == "" {
			return nil
		}
		return []string{typed}
	case []string:
		return append([]string(nil), typed...)
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			if text, ok := item.(string); ok {
				if text = strings.TrimSpace(text); text != "" {
					out = append(out, text)
				}
			}
		}
		return out
	default:
		return nil
	}
}
