package audit

import (
	"bytes"
	"encoding/json"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// fileRecord 是归档 JSONL 的一行，字段名与旧网关 calls-*.jsonl 对齐，
// 新增 id/err_message/http_status/request_id/remote_ip/query 便于追溯。
// input/output/query 是完整原文，不经过 capPayload。
type fileRecord struct {
	ID             string          `json:"id"`
	CallID         string          `json:"call_id"`
	TS             string          `json:"ts"`
	Tool           string          `json:"tool"`
	Source         string          `json:"source,omitempty"`
	Deferred       bool            `json:"deferred"`
	Tenant         string          `json:"tenant,omitempty"`
	UserID         string          `json:"user_id,omitempty"`
	SessionID      string          `json:"session_id,omitempty"`
	CaseLike       *bool           `json:"case_like,omitempty"`
	Ms             int64           `json:"ms"`
	OK             bool            `json:"ok"`
	Err            string          `json:"err,omitempty"`
	ErrMessage     string          `json:"err_message,omitempty"`
	HTTPStatus     int             `json:"http_status,omitempty"`
	ResultBytes    int             `json:"result_bytes,omitempty"`
	RequestID      string          `json:"request_id,omitempty"`
	RemoteIP       string          `json:"remote_ip,omitempty"`
	Input          map[string]any  `json:"input,omitempty"`
	Output         json.RawMessage `json:"output,omitempty"`
	Query          string          `json:"query,omitempty"`
	RequestTokens  int             `json:"request_tokens"`
	ResponseTokens int             `json:"response_tokens"`
	LLMCalls       int             `json:"llm_calls"`
	ClientVersion  string          `json:"client_version,omitempty"`
	HostVersion    string          `json:"host_version,omitempty"`
	AgentIdentity  string          `json:"agent_identity,omitempty"`
	AgentContext   string          `json:"agent_context,omitempty"`
	TurnNumber     int             `json:"turn_number,omitempty"`
}

func encodeFileRecord(rec domain.AuditRecord) ([]byte, error) {
	output, err := encodeOutput(rec.Output)
	if err != nil {
		return nil, err
	}
	line := fileRecord{
		ID:             rec.ID,
		CallID:         rec.CallID,
		TS:             rec.TS.UTC().Format(time.RFC3339Nano),
		Tool:           rec.Tool,
		Source:         rec.Source,
		Deferred:       rec.Deferred,
		Tenant:         rec.TenantID,
		UserID:         rec.UserID,
		SessionID:      rec.SessionID,
		CaseLike:       rec.CaseLike,
		Ms:             rec.Ms,
		OK:             rec.OK,
		Err:            rec.Err,
		ErrMessage:     rec.ErrMessage,
		HTTPStatus:     rec.HTTPStatus,
		ResultBytes:    rec.ResultBytes,
		RequestID:      rec.RequestID,
		RemoteIP:       rec.RemoteIP,
		Input:          rec.Input,
		Output:         output,
		Query:          rec.Query,
		RequestTokens:  rec.RequestTokens,
		ResponseTokens: rec.ResponseTokens,
		LLMCalls:       rec.LLMCalls,
		ClientVersion:  rec.ClientVersion,
		HostVersion:    rec.HostVersion,
		AgentIdentity:  rec.AgentIdentity,
		AgentContext:   rec.AgentContext,
		TurnNumber:     rec.TurnNumber,
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	// 不转义 & < >，否则大正文会膨胀数倍；JSONL 只要求合法 JSON。
	enc.SetEscapeHTML(false)
	if err := enc.Encode(line); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// encodeOutput 尽量把 output 保持为 JSON 结构；非法 JSON 或空值退化为字符串。
// json.Compact 保证结果不含字面换行，一条记录始终只占一行。
func encodeOutput(s string) (json.RawMessage, error) {
	trimmed := strings.TrimSpace(s)
	if trimmed == "" {
		return nil, nil
	}
	if json.Valid([]byte(trimmed)) {
		var buf bytes.Buffer
		if err := json.Compact(&buf, []byte(trimmed)); err == nil {
			return json.RawMessage(buf.Bytes()), nil
		}
	}
	encoded, err := json.Marshal(s)
	if err != nil {
		return nil, err
	}
	return json.RawMessage(encoded), nil
}
