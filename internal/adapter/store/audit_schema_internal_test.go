package store

import (
	"context"
	"database/sql"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// auditInsertColumnList 把插入列清单拆成列名。
func auditInsertColumnList() []string {
	raw := strings.Split(auditInsertColumns, ",")
	out := make([]string, len(raw))
	for i, c := range raw {
		out[i] = strings.TrimSpace(c)
	}
	return out
}

// TestAuditInsertColumnsMatchArgs 守住列清单与 auditArgs 的列数和顺序一致。
func TestAuditInsertColumnsMatchArgs(t *testing.T) {
	cols := auditInsertColumnList()
	ts := time.Date(2026, 10, 26, 12, 34, 56, 0, time.UTC)

	// 三个布尔列各占一位，否则互换后读回的 "1"/"0" 相同，错位检测不到。
	present, absent := true, false
	cases := []struct {
		name     string
		caseLike *bool
		deferred bool
		ok       bool
		wantCase string
		nullCase bool
	}{
		{"case_like", &present, false, false, "1", false},
		{"deferred", &absent, true, false, "0", false},
		{"ok", &absent, false, true, "0", false},
		{"case_like 未上报", nil, false, false, "", true},
	}

	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := openAuditStore(t)
			rec := domain.AuditRecord{
				ID:             fmt.Sprintf("audit-%d", i),
				CallID:         "call-1",
				TS:             ts,
				Tool:           domain.ToolAddMessage,
				Source:         domain.AuditSourceDashboard,
				TenantID:       "cube-a",
				UserID:         "user-1",
				SessionID:      "sess-1",
				CaseLike:       tc.caseLike,
				Deferred:       tc.deferred,
				Ms:             1234,
				OK:             tc.ok,
				Err:            domain.ErrInvalidInput.Error(),
				ErrMessage:     "参数不合法",
				HTTPStatus:     400,
				ResultBytes:    77,
				RequestID:      "req-1",
				RemoteIP:       "10.0.0.1",
				ClientVersion:  "graphmemory/0.2.12",
				HostVersion:    "hermes/0.21.5",
				AgentIdentity:  "deploy-a",
				AgentContext:   "primary",
				TurnNumber:     3,
				Input:          map[string]any{"k": "v"},
				Output:         "out-1",
				Query:          "query-1",
				RequestTokens:  11,
				ResponseTokens: 22,
				LLMCalls:       33,
			}

			args, err := auditArgs(rec)
			if err != nil {
				t.Fatalf("auditArgs: %v", err)
			}
			if len(args) != len(cols) {
				t.Fatalf("列清单 %d 列，auditArgs 返回 %d 个参数：两者必须一一对应", len(cols), len(args))
			}

			if err := s.InsertAudit(context.Background(), rec); err != nil {
				t.Fatalf("InsertAudit: %v", err)
			}

			selects := make([]string, len(cols))
			for j, c := range cols {
				selects[j] = "CAST(" + c + " AS TEXT)"
			}
			row := s.read.QueryRowContext(context.Background(),
				`SELECT `+strings.Join(selects, ", ")+` FROM audit_records WHERE id = ?`, rec.ID)
			got := make([]sql.NullString, len(cols))
			dest := make([]any, len(cols))
			for j := range got {
				dest[j] = &got[j]
			}
			if err := row.Scan(dest...); err != nil {
				t.Fatalf("读取审计行: %v", err)
			}

			want := map[string]string{
				"id":              rec.ID,
				"call_id":         rec.CallID,
				"ts":              encodeTime(ts),
				"tool":            rec.Tool,
				"source":          rec.Source,
				"tenant_id":       rec.TenantID,
				"user_id":         rec.UserID,
				"session_id":      rec.SessionID,
				"case_like":       tc.wantCase,
				"deferred":        boolText(tc.deferred),
				"ms":              "1234",
				"ok":              boolText(tc.ok),
				"err":             rec.Err,
				"err_message":     rec.ErrMessage,
				"http_status":     "400",
				"result_bytes":    "77",
				"input_json":      `{"k":"v"}`,
				"output":          rec.Output,
				"query":           rec.Query,
				"request_id":      rec.RequestID,
				"remote_ip":       rec.RemoteIP,
				"request_tokens":  "11",
				"response_tokens": "22",
				"llm_calls":       "33",
				"client_version":  rec.ClientVersion,
				"host_version":    rec.HostVersion,
				"agent_identity":  rec.AgentIdentity,
				"agent_context":   rec.AgentContext,
				"turn_number":     "3",
			}
			if len(want) != len(cols) {
				t.Fatalf("列清单是 %d 列，期望表有 %d 项：加列后请在这里补上期望值", len(cols), len(want))
			}
			for j, c := range cols {
				expected, ok := want[c]
				if !ok {
					t.Errorf("列 %q 没有期望值", c)
					continue
				}
				if got[j].String != expected {
					t.Errorf("列 %q 读回 %q，期望 %q：列顺序与 auditArgs 不一致", c, got[j].String, expected)
				}
				if c == "case_like" && got[j].Valid == tc.nullCase {
					t.Errorf("列 case_like 的 NULL 形态不对：Valid=%v，期望 Valid=%v", got[j].Valid, !tc.nullCase)
				}
			}
		})
	}
}

func boolText(v bool) string {
	if v {
		return "1"
	}
	return "0"
}

// TestAuditWhereClauses 核对条件片段与参数顺序：错位在 SQL 层不会报错。
func TestAuditWhereClauses(t *testing.T) {
	okTrue := true
	since := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	until := time.Date(2026, 10, 2, 0, 0, 0, 0, time.UTC)

	cases := []struct {
		name      string
		filter    domain.AuditFilter
		wantWhere string
		wantArgs  []any
	}{
		{
			name:   "空条件不产生 WHERE",
			filter: domain.AuditFilter{},
		},
		{
			name:      "租户",
			filter:    domain.AuditFilter{TenantID: "cube-a"},
			wantWhere: " WHERE tenant_id = ?",
			wantArgs:  []any{"cube-a"},
		},
		{
			name:      "单个工具",
			filter:    domain.AuditFilter{Tool: domain.ToolAddMessage},
			wantWhere: " WHERE tool = ?",
			wantArgs:  []any{domain.ToolAddMessage},
		},
		{
			name:      "工具集合按顺序展开",
			filter:    domain.AuditFilter{Tools: []string{"a", "b"}},
			wantWhere: " WHERE tool IN (?,?)",
			wantArgs:  []any{"a", "b"},
		},
		{
			name:      "排除工具",
			filter:    domain.AuditFilter{ExcludeTools: []string{"x"}},
			wantWhere: " WHERE tool NOT IN (?)",
			wantArgs:  []any{"x"},
		},
		{
			name:      "来源",
			filter:    domain.AuditFilter{Sources: []string{domain.AuditSourceMCP, domain.AuditSourceREST}},
			wantWhere: " WHERE source IN (?,?)",
			wantArgs:  []any{domain.AuditSourceMCP, domain.AuditSourceREST},
		},
		{
			name:      "行内结果",
			filter:    domain.AuditFilter{OK: &okTrue},
			wantWhere: " WHERE ok = ?",
			wantArgs:  []any{true},
		},
		{
			name:      "派生状态",
			filter:    domain.AuditFilter{Status: domain.AuditStatusSuccess},
			wantWhere: " WHERE (" + auditStatusExpr + ") = ?",
			wantArgs:  []any{string(domain.AuditStatusSuccess)},
		},
		{
			name:      "时间区间",
			filter:    domain.AuditFilter{Since: since, Until: until},
			wantWhere: " WHERE ts >= ? AND ts < ?",
			wantArgs:  []any{encodeTime(since), encodeTime(until)},
		},
		{
			name: "多条件按固定顺序拼接",
			filter: domain.AuditFilter{
				TenantID: "cube-a",
				Tool:     domain.ToolSearchMemory,
				Tools:    []string{"t1", "t2"},
				Since:    since,
			},
			wantWhere: " WHERE tenant_id = ? AND tool = ? AND tool IN (?,?) AND ts >= ?",
			wantArgs:  []any{"cube-a", domain.ToolSearchMemory, "t1", "t2", encodeTime(since)},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			where, args := auditWhere(tc.filter)
			if where != tc.wantWhere {
				t.Fatalf("where = %q，期望 %q", where, tc.wantWhere)
			}
			if !reflect.DeepEqual(args, tc.wantArgs) {
				t.Fatalf("args = %#v，期望 %#v", args, tc.wantArgs)
			}
		})
	}
}

// TestAuditWhereTextClause 覆盖全文检索的参数顺序：模式 × 7、工具名、模式 × 2。
func TestAuditWhereTextClause(t *testing.T) {
	where, args := auditWhere(domain.AuditFilter{Text: `50%_off`})

	if !strings.HasPrefix(where, " WHERE (lower(input_json) LIKE ?") {
		t.Fatalf("where 未按文本列展开: %q", where)
	}
	if !strings.Contains(where, "EXISTS (SELECT 1 FROM audit_records t") {
		t.Fatalf("where 缺少后台终态行子查询: %q", where)
	}
	wantLen := len(auditTextColumns) + 1 + 2
	if len(args) != wantLen {
		t.Fatalf("args 有 %d 个，期望 %d 个", len(args), wantLen)
	}
	pattern, ok := args[0].(string)
	if !ok || pattern != `%50\%\_off%` {
		t.Fatalf("通配符未按字面转义: %#v", args[0])
	}
	for i := range auditTextColumns {
		if args[i] != pattern {
			t.Fatalf("第 %d 个参数 = %#v，期望与前 7 列相同的模式", i, args[i])
		}
	}
	if args[len(auditTextColumns)] != domain.ToolAddMessageBackground {
		t.Fatalf("子查询工具名 = %#v", args[len(auditTextColumns)])
	}
	if args[len(args)-1] != pattern || args[len(args)-2] != pattern {
		t.Fatalf("子查询末尾两个参数应为模式: %#v", args[len(args)-2:])
	}
}
