package store_test

import (
	"context"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// seedAudit 插入一条审计行；offset 用来控制同租户内的排序与终态行的先后。
func seedAudit(t *testing.T, s *store.Store, rec domain.AuditRecord, at time.Time) {
	t.Helper()
	rec.TS = at
	if rec.TenantID == "" {
		rec.TenantID = "cube-a"
	}
	if err := s.InsertAudit(context.Background(), rec); err != nil {
		t.Fatalf("insert audit %s: %v", rec.ID, err)
	}
}

// enqueueJob 插入一条异步任务，用于验证 RUNNING 只认“job 仍存在”。
func enqueueJob(t *testing.T, s *store.Store, id string, status domain.JobStatus) {
	t.Helper()
	if err := s.EnqueueJob(context.Background(), domain.Job{
		ID: id, Kind: domain.JobKindMemoryAdd, Status: status, TenantID: "cube-a", Payload: []byte("{}"),
	}); err != nil {
		t.Fatalf("enqueue job %s: %v", id, err)
	}
}

func auditStatuses(t *testing.T, records []domain.AuditRecord) map[string]domain.AuditStatus {
	t.Helper()
	out := make(map[string]domain.AuditStatus, len(records))
	for i := range records {
		out[records[i].ID] = records[i].Status
	}
	return out
}

// 派生状态：非 deferred 行按自身 ok；deferred 行取同一 call_id 的终态行；
// 没有终态行时，只有 job 仍存在才是 RUNNING，job 已清理或 call_id 对不上就按
// 行内 ok 回退；call_id 为空的历史行不跨行合并。
func TestAuditListDerivesBackgroundStatus(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 23, 10, 0, 0, 0, time.UTC)

	seedAudit(t, s, domain.AuditRecord{ID: "r-ok", CallID: "call-ok", Tool: domain.ToolAddMessage, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-fail", CallID: "call-fail", Tool: domain.ToolAddMessage}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-running", CallID: "call-running", Tool: domain.ToolAddMessage, Deferred: true, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-job-running", CallID: "call-job-running", Tool: domain.ToolAddMessage, Deferred: true, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-job-gone", CallID: "call-job-gone", Tool: domain.ToolAddMessage, Deferred: true, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-done-ok", CallID: "call-done-ok", Tool: domain.ToolAddMessage, Deferred: true, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-done-fail", CallID: "call-done-fail", Tool: domain.ToolAddMessage, Deferred: true, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-legacy", Tool: domain.ToolAddMessage, Deferred: true, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "r-legacy-fail", CallID: "call-legacy-fail", Tool: domain.ToolAddMessage, Deferred: true}, base)

	// 仍在队列里 / 仍在执行的 job 存在 → RUNNING；job 已清理（这里不插）→ 按行内 ok。
	enqueueJob(t, s, "call-running", domain.JobStatusPending)
	enqueueJob(t, s, "call-job-running", domain.JobStatusRunning)

	seedAudit(t, s, domain.AuditRecord{ID: "t-ok", CallID: "call-done-ok", Tool: domain.ToolAddMessageBackground, OK: true}, base.Add(time.Second))
	seedAudit(t, s, domain.AuditRecord{ID: "t-fail", CallID: "call-done-fail", Tool: domain.ToolAddMessageBackground, Err: "upstream_rejected"}, base.Add(time.Second))
	seedAudit(t, s, domain.AuditRecord{ID: "t-legacy", Tool: domain.ToolAddMessageBackground, OK: false}, base.Add(time.Second))

	records, total, err := s.ListAudit(ctx, domain.AuditFilter{
		TenantID: "cube-a", Tools: domain.GatewayTools(), Limit: 50,
	})
	if err != nil {
		t.Fatalf("ListAudit: %v", err)
	}
	if total != 9 || len(records) != 9 {
		t.Fatalf("total=%d rows=%d want 9/9（终态行不计入逻辑调用）", total, len(records))
	}
	want := map[string]domain.AuditStatus{
		"r-ok":          domain.AuditStatusSuccess,
		"r-fail":        domain.AuditStatusFailed,
		"r-running":     domain.AuditStatusRunning,
		"r-job-running": domain.AuditStatusRunning,
		"r-job-gone":    domain.AuditStatusSuccess,
		"r-done-ok":     domain.AuditStatusSuccess,
		"r-done-fail":   domain.AuditStatusFailed,
		"r-legacy":      domain.AuditStatusSuccess,
		"r-legacy-fail": domain.AuditStatusFailed,
	}
	got := auditStatuses(t, records)
	for id, status := range want {
		if got[id] != status {
			t.Fatalf("%s 状态 = %q want %q（全部: %v）", id, got[id], status, got)
		}
	}

	filtered := []struct {
		status domain.AuditStatus
		ids    []string
	}{
		{domain.AuditStatusSuccess, []string{"r-ok", "r-job-gone", "r-done-ok", "r-legacy"}},
		{domain.AuditStatusFailed, []string{"r-fail", "r-done-fail", "r-legacy-fail"}},
		{domain.AuditStatusRunning, []string{"r-running", "r-job-running"}},
	}
	for _, tc := range filtered {
		t.Run(string(tc.status), func(t *testing.T) {
			t.Parallel()
			rows, n, err := s.ListAudit(ctx, domain.AuditFilter{
				TenantID: "cube-a", Tools: domain.GatewayTools(), Status: tc.status, Limit: 50,
			})
			if err != nil {
				t.Fatalf("ListAudit(%s): %v", tc.status, err)
			}
			if n != len(tc.ids) || len(rows) != len(tc.ids) {
				t.Fatalf("%s total=%d rows=%d want %d", tc.status, n, len(rows), len(tc.ids))
			}
			seen := auditStatuses(t, rows)
			for _, id := range tc.ids {
				if _, ok := seen[id]; !ok {
					t.Fatalf("%s 过滤缺少 %s: %v", tc.status, id, seen)
				}
			}
		})
	}
}

// 状态过滤、计数与分页都在 SQL 里完成：第 2 页只含该状态的第 4-6 条。
func TestAuditListStatusFilterPaginatesInDatabase(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 23, 10, 0, 0, 0, time.UTC)

	// 7 条终态成功 + 1 条仍在运行；按 ts 倒序，id 从 c7 到 c1。
	for i, id := range []string{"c1", "c2", "c3", "c4", "c5", "c6", "c7"} {
		at := base.Add(time.Duration(i) * time.Minute)
		seedAudit(t, s, domain.AuditRecord{ID: id, CallID: "call-" + id, Tool: domain.ToolAddMessage, Deferred: true, OK: true}, at)
		seedAudit(t, s, domain.AuditRecord{ID: "t-" + id, CallID: "call-" + id, Tool: domain.ToolAddMessageBackground, OK: true}, at.Add(time.Second))
	}
	seedAudit(t, s, domain.AuditRecord{ID: "running", CallID: "call-running", Tool: domain.ToolAddMessage, Deferred: true, OK: true}, base.Add(10*time.Minute))
	enqueueJob(t, s, "call-running", domain.JobStatusRunning)

	rows, total, err := s.ListAudit(ctx, domain.AuditFilter{
		TenantID: "cube-a", Tools: domain.GatewayTools(), Status: domain.AuditStatusSuccess,
		Limit: 3, Offset: 3,
	})
	if err != nil {
		t.Fatalf("ListAudit: %v", err)
	}
	if total != 7 {
		t.Fatalf("total=%d want 7（只计请求行）", total)
	}
	want := []string{"c4", "c3", "c2"}
	if len(rows) != len(want) {
		t.Fatalf("第 2 页行数=%d want %d: %+v", len(rows), len(want), rows)
	}
	for i, id := range want {
		if rows[i].ID != id {
			t.Fatalf("第 2 页第 %d 行 = %s want %s", i+1, rows[i].ID, id)
		}
	}
}

// 详情与列表同口径；失败时错误/正文取终态行，入参仍取请求行。
func TestAuditGetMergesTerminalFailure(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 23, 10, 0, 0, 0, time.UTC)

	seedAudit(t, s, domain.AuditRecord{
		ID: "req", CallID: "call-1", Tool: domain.ToolAddMessage, Deferred: true, OK: true,
		Input: map[string]any{"messages": []any{"hi"}},
	}, base)
	seedAudit(t, s, domain.AuditRecord{
		ID: "term", CallID: "call-1", Tool: domain.ToolAddMessageBackground,
		Err: "upstream_rejected", Output: `{"ok":false}`, ResultBytes: 12,
	}, base.Add(time.Second))

	rec, err := s.GetAudit(ctx, "req")
	if err != nil {
		t.Fatalf("GetAudit: %v", err)
	}
	if rec.Status != domain.AuditStatusFailed {
		t.Fatalf("status=%q want FAILED", rec.Status)
	}
	if rec.Err != "upstream_rejected" || rec.Output != `{"ok":false}` {
		t.Fatalf("失败正文应取终态行: %+v", rec)
	}
	if rec.Input["messages"] == nil {
		t.Fatalf("入参应仍取请求行: %+v", rec.Input)
	}
}

// 搜索必须覆盖同一 call_id 的异步终态行：详情会合并终态行的错误与正文，
// 列表若只匹配请求行，会出现“搜不到但点进去能看到”的不一致。
func TestAuditTextSearchCoversTerminalRows(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 23, 10, 0, 0, 0, time.UTC)

	seedAudit(t, s, domain.AuditRecord{
		ID: "req", CallID: "call-1", Tool: domain.ToolAddMessage, Deferred: true, OK: true,
		Input: map[string]any{"messages": []any{"正文关键词"}},
	}, base)
	seedAudit(t, s, domain.AuditRecord{
		ID: "term", CallID: "call-1", Tool: domain.ToolAddMessageBackground,
		Err: "upstream_timeout", Output: `{"ok":false,"err":"quota exhausted"}`,
	}, base.Add(time.Second))

	for _, text := range []string{"upstream_timeout", "QUOTA", "正文关键词"} {
		t.Run(text, func(t *testing.T) {
			t.Parallel()
			rows, total, err := s.ListAudit(ctx, domain.AuditFilter{
				TenantID: "cube-a", Tools: domain.GatewayTools(), Text: text, Limit: 50,
			})
			if err != nil {
				t.Fatalf("ListAudit(%q): %v", text, err)
			}
			if total != 1 || len(rows) != 1 || rows[0].ID != "req" {
				t.Fatalf("query=%q 应只命中请求行 req: total=%d rows=%+v", text, total, rows)
			}
		})
	}

	rows, total, err := s.ListAudit(ctx, domain.AuditFilter{
		TenantID: "cube-a", Tools: domain.GatewayTools(), Text: "no-such-text", Limit: 50,
	})
	if err != nil {
		t.Fatalf("ListAudit(no match): %v", err)
	}
	if total != 0 || len(rows) != 0 {
		t.Fatalf("不匹配的 query 应为空: total=%d rows=%+v", total, rows)
	}
}

// 重放请求行与原任务共用 call_id 后，两条请求行都应显示首次任务的终态。
func TestAuditReplayRequestSharesTerminalStatus(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 23, 10, 0, 0, 0, time.UTC)

	seedAudit(t, s, domain.AuditRecord{
		ID: "first", CallID: "call-shared", Tool: domain.ToolAddMessage, Deferred: true, OK: true,
	}, base)
	seedAudit(t, s, domain.AuditRecord{
		ID: "term", CallID: "call-shared", Tool: domain.ToolAddMessageBackground, OK: true,
	}, base.Add(time.Second))
	seedAudit(t, s, domain.AuditRecord{
		ID: "replay", CallID: "call-shared", Tool: domain.ToolAddMessage, Deferred: true, OK: true,
	}, base.Add(2*time.Second))

	rows, total, err := s.ListAudit(ctx, domain.AuditFilter{
		TenantID: "cube-a", Tools: domain.GatewayTools(), Limit: 50,
	})
	if err != nil {
		t.Fatalf("ListAudit: %v", err)
	}
	if total != 2 || len(rows) != 2 {
		t.Fatalf("total=%d rows=%d want 2/2（请求行两条，终态行不计）", total, len(rows))
	}
	for _, rec := range rows {
		if rec.Status != domain.AuditStatusSuccess {
			t.Fatalf("%s 状态=%q want SUCCESS（应与原任务共用终态）", rec.ID, rec.Status)
		}
	}
}

// token 用量落在异步 add 的终态行；请求行详情合并终态行的用量，
// 用量聚合也按天带回 token。
func TestAuditUsageTokensAndExclusion(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 20, 2, 0, 0, 0, time.UTC)

	seedAudit(t, s, domain.AuditRecord{
		ID: "req", CallID: "call-1", Tool: domain.ToolAddMessage, Deferred: true, OK: true,
	}, base)
	seedAudit(t, s, domain.AuditRecord{
		ID: "term", CallID: "call-1", Tool: domain.ToolAddMessageBackground, OK: true,
		RequestTokens: 120, ResponseTokens: 80, LLMCalls: 4,
	}, base.Add(time.Second))
	seedAudit(t, s, domain.AuditRecord{ID: "dash", Tool: "dashboard.project.update", OK: true}, base)

	rec, err := s.GetAudit(ctx, "req")
	if err != nil {
		t.Fatalf("GetAudit: %v", err)
	}
	if rec.RequestTokens != 120 || rec.ResponseTokens != 80 || rec.LLMCalls != 4 {
		t.Fatalf("request row should merge terminal usage: %+v", rec)
	}

	counts, err := s.AuditHourCounts(ctx, domain.AuditFilter{
		TenantID: "cube-a", Tools: []string{domain.ToolAddMessageBackground},
	})
	if err != nil {
		t.Fatalf("AuditHourCounts: %v", err)
	}
	if len(counts) != 1 || counts[0].RequestTokens != 120 || counts[0].ResponseTokens != 80 || counts[0].LLMCalls != 4 {
		t.Fatalf("token aggregation: %+v", counts)
	}

	rows, total, err := s.ListAudit(ctx, domain.AuditFilter{
		TenantID: "cube-a", ExcludeTools: []string{domain.ToolAddMessageBackground}, Limit: 50,
	})
	if err != nil {
		t.Fatalf("ListAudit exclude: %v", err)
	}
	if total != 2 || len(rows) != 2 {
		t.Fatalf("exclude background: total=%d rows=%d", total, len(rows))
	}

	existing, err := s.AuditIDsExist(ctx, []string{"req", "missing"})
	if err != nil {
		t.Fatalf("AuditIDsExist: %v", err)
	}
	if _, ok := existing["req"]; !ok || len(existing) != 1 {
		t.Fatalf("AuditIDsExist = %v", existing)
	}
}

// 来源（source）要能原样落库，也能按入口过滤：历史行与后台终态行的 source 为空，
// 按来源筛时自然被排除，不会被算进某条入口的账里。
func TestAuditSourceRoundTripAndFilter(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 26, 2, 0, 0, 0, time.UTC)

	seedAudit(t, s, domain.AuditRecord{ID: "rest-1", Tool: domain.ToolAddMessage, Source: domain.AuditSourceREST, OK: true}, base)
	seedAudit(t, s, domain.AuditRecord{ID: "mcp-1", Tool: domain.ToolAddMessage, Source: domain.AuditSourceMCP, OK: true}, base.Add(time.Second))
	seedAudit(t, s, domain.AuditRecord{ID: "mcp-2", Tool: domain.ToolSearchMemory, Source: domain.AuditSourceMCP, OK: true}, base.Add(2*time.Second))
	seedAudit(t, s, domain.AuditRecord{ID: "legacy", Tool: domain.ToolAddMessage, OK: true}, base.Add(3*time.Second))

	all, total, err := s.ListAudit(ctx, domain.AuditFilter{TenantID: "cube-a", Limit: 50})
	if err != nil {
		t.Fatalf("ListAudit: %v", err)
	}
	if total != 4 || len(all) != 4 {
		t.Fatalf("unfiltered: total=%d rows=%d", total, len(all))
	}
	sources := map[string]string{}
	for i := range all {
		sources[all[i].ID] = all[i].Source
	}
	if sources["rest-1"] != domain.AuditSourceREST || sources["mcp-1"] != domain.AuditSourceMCP || sources["legacy"] != "" {
		t.Fatalf("source round trip: %v", sources)
	}

	only, total, err := s.ListAudit(ctx, domain.AuditFilter{TenantID: "cube-a", Sources: []string{domain.AuditSourceREST}, Limit: 50})
	if err != nil {
		t.Fatalf("ListAudit REST: %v", err)
	}
	if total != 1 || len(only) != 1 || only[0].ID != "rest-1" {
		t.Fatalf("来源过滤只该命中 rest-1: total=%d rows=%+v", total, only)
	}

	both, total, err := s.ListAudit(ctx, domain.AuditFilter{
		TenantID: "cube-a", Sources: []string{domain.AuditSourceREST, domain.AuditSourceMCP}, Limit: 50,
	})
	if err != nil {
		t.Fatalf("ListAudit REST+MCP: %v", err)
	}
	if total != 3 || len(both) != 3 {
		t.Fatalf("REST+MCP 应为 3 行: total=%d rows=%d", total, len(both))
	}

	rec, err := s.GetAudit(ctx, "mcp-1")
	if err != nil {
		t.Fatalf("GetAudit: %v", err)
	}
	if rec.Source != domain.AuditSourceMCP {
		t.Fatalf("GetAudit source = %q", rec.Source)
	}
}

func TestAuditClientMetaRoundTrip(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 26, 2, 0, 0, 0, time.UTC)

	seedAudit(t, s, domain.AuditRecord{
		ID: "meta-1", Tool: domain.ToolSearchMemory, Source: domain.AuditSourcePlugin, OK: true,
		ClientVersion: "0.2.12", HostVersion: "hermes/0.21.5",
		AgentIdentity: "example-agent", AgentContext: "primary", TurnNumber: 7,
	}, base)

	// 列表与详情走两套列清单，都要能读回这五个字段。
	rows, _, err := s.ListAudit(ctx, domain.AuditFilter{TenantID: "cube-a", Limit: 10})
	if err != nil {
		t.Fatalf("ListAudit: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("rows=%d", len(rows))
	}
	got := rows[0]
	if got.ClientVersion != "0.2.12" || got.HostVersion != "hermes/0.21.5" ||
		got.AgentIdentity != "example-agent" || got.AgentContext != "primary" || got.TurnNumber != 7 {
		t.Fatalf("list 字段不符: %+v", got)
	}
	rec, err := s.GetAudit(ctx, "meta-1")
	if err != nil {
		t.Fatalf("GetAudit: %v", err)
	}
	if rec.HostVersion != "hermes/0.21.5" || rec.TurnNumber != 7 {
		t.Fatalf("detail 字段不符: %+v", rec)
	}
}

func TestAuditCallIDFilterIsTenantScoped(t *testing.T) {
	t.Parallel()
	s := openStore(t)
	at := time.Now()
	seedAudit(t, s, domain.AuditRecord{ID: "wanted", CallID: "call", Tool: domain.ToolAddMessageBackground, TenantID: "cube-a", OK: true}, at)
	seedAudit(t, s, domain.AuditRecord{ID: "other-cube", CallID: "call", Tool: domain.ToolAddMessageBackground, TenantID: "cube-b", OK: true}, at)
	seedAudit(t, s, domain.AuditRecord{ID: "other-call", CallID: "different", Tool: domain.ToolAddMessageBackground, TenantID: "cube-a", OK: true}, at)
	got, total, err := s.ListAudit(context.Background(), domain.AuditFilter{TenantID: "cube-a", CallID: "call", Tool: domain.ToolAddMessageBackground, Limit: 10})
	if err != nil || total != 1 || len(got) != 1 || got[0].ID != "wanted" {
		t.Fatalf("rows=%+v total=%d err=%v", got, total, err)
	}
}
