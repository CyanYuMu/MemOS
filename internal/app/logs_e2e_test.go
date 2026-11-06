package app

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func (r result) logs() []map[string]any {
	items, _ := r.obj()["logs"].([]any)
	out := make([]map[string]any, 0, len(items))
	for _, item := range items {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func (e *e2eEnv) seedAudit(rec domain.AuditRecord) {
	e.t.Helper()
	if err := e.app.store.InsertAudit(e.t.Context(), rec); err != nil {
		e.t.Fatalf("insert audit %s: %v", rec.ID, err)
	}
}

// 日志与用量按项目授权，只统计对外的 5 个工具，日期按北京时间分桶。
func TestE2ELogsAndUsageAreScopedByProject(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	at := func(s string) time.Time {
		ts, err := time.Parse(time.RFC3339, s)
		if err != nil {
			t.Fatalf("parse %s: %v", s, err)
		}
		return ts
	}
	e.seedAudit(domain.AuditRecord{
		ID: "l-search", TS: at("2026-10-20T10:00:00Z"), Tool: "search_memory", TenantID: "cube-a", OK: true, Ms: 12,
		SessionID: "sess-1", Input: map[string]any{"query": "green tea"}, Output: `{"ok":true}`,
	})
	e.seedAudit(domain.AuditRecord{ID: "l-add", TS: at("2026-10-20T17:00:00Z"), Tool: "add_message", TenantID: "cube-a", OK: true, Ms: 3})
	e.seedAudit(domain.AuditRecord{ID: "l-fail", TS: at("2026-10-21T02:00:00Z"), Tool: "add_message", TenantID: "cube-a", OK: false, Err: "invalid_input"})
	e.seedAudit(domain.AuditRecord{
		ID: "l-other", TS: at("2026-10-20T10:00:00Z"), Tool: "search_memory", TenantID: "cube-b", OK: true,
		Input: map[string]any{"query": "secret-b"},
	})
	e.seedAudit(domain.AuditRecord{ID: "l-dash", TS: at("2026-10-20T10:00:00Z"), Tool: "dashboard.keys.create", TenantID: "cube-a", OK: true})
	e.seedAudit(domain.AuditRecord{ID: "l-bg", TS: at("2026-10-20T10:00:00Z"), Tool: "add_message.background", TenantID: "cube-a", OK: true})

	alice := e.loginAs("alice")
	all := alice.post("/api/dashboard/log/list", `{"projectId":"cube-a","pageNum":1,"pageSize":10}`)
	expect(t, "list", all, http.StatusOK)
	logs := all.logs()
	if len(logs) != 4 || logs[0]["id"] != "l-fail" || logs[0]["createTime"] != "2026-10-21 10:00:00" ||
		logs[0]["status"] != "FAILED" || logs[0]["eventType"] != "addMessage" {
		t.Fatalf("默认应返回 cube-a 的网关调用与控制台操作共 4 条（后台终态行合并），按时间倒序: %s", all.raw)
	}
	if got := alice.post("/api/dashboard/log/list", `{"projectId":"cube-a","scope":"gateway"}`).logs(); len(got) != 3 {
		t.Fatalf("scope=gateway 只应有 3 条对外调用: %v", got)
	}
	console := alice.post("/api/dashboard/log/list", `{"projectId":"cube-a","scope":"console"}`)
	if got := console.logs(); len(got) != 1 || got[0]["eventType"] != "dashboard.keys.create" {
		t.Fatalf("scope=console 只应有控制台操作: %s", console.raw)
	}
	for label, body := range map[string]string{
		"status":    `{"projectId":"cube-a","status":"FAILED"}`,
		"eventType": `{"projectId":"cube-a","eventType":"searchMemory"}`,
		"query":     `{"projectId":"cube-a","query":"GREEN"}`,
		"range":     `{"projectId":"cube-a","scope":"gateway","startTime":"2026-10-20T18:00","endTime":"2026-10-20T18:00"}`,
	} {
		if got := alice.post("/api/dashboard/log/list", body).logs(); len(got) != 1 {
			t.Fatalf("%s 过滤应只剩 1 条: %v", label, got)
		}
	}
	if got := alice.post("/api/dashboard/log/list", `{"projectId":"cube-a","status":"RUNNING"}`).logs(); len(got) != 0 {
		t.Fatalf("没有进行中的行: %v", got)
	}

	// 异步 add：请求行 deferred，终态行按 call_id 合并；中间没有终态行时为 RUNNING。
	// 时间放在用量区间之外，避免影响下面按天分桶的断言。
	runAt := at("2026-10-23T03:00:00Z")
	e.seedAudit(domain.AuditRecord{
		ID: "l-run", CallID: "c-run", TS: runAt, Tool: "add_message", TenantID: "cube-a", OK: true, Deferred: true,
		Input: map[string]any{"messages": []any{"hi"}},
	})
	// 没有终态行、job 仍在队列里 → RUNNING；job 清理后同一行会按行内 ok 回退。
	if err := e.app.store.EnqueueJob(context.Background(), domain.Job{
		ID: "c-run", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a", Payload: []byte("{}"),
	}); err != nil {
		t.Fatalf("enqueue job c-run: %v", err)
	}
	e.seedAudit(domain.AuditRecord{
		ID: "l-run-ok", CallID: "c-ok", TS: runAt, Tool: "add_message", TenantID: "cube-a", OK: true, Deferred: true,
	})
	e.seedAudit(domain.AuditRecord{
		ID: "t-ok", CallID: "c-ok", TS: runAt.Add(time.Second), Tool: "add_message.background", TenantID: "cube-a", OK: true,
	})
	e.seedAudit(domain.AuditRecord{
		ID: "l-run-fail", CallID: "c-fail", TS: runAt, Tool: "add_message", TenantID: "cube-a", OK: true, Deferred: true,
		Input: map[string]any{"messages": []any{"hi"}},
	})
	e.seedAudit(domain.AuditRecord{
		ID: "t-fail", CallID: "c-fail", TS: runAt.Add(time.Second), Tool: "add_message.background", TenantID: "cube-a",
		Err: "upstream_rejected", Output: `{"ok":false}`,
	})

	merged := alice.post("/api/dashboard/log/list", `{"projectId":"cube-a","pageNum":1,"pageSize":10}`)
	expect(t, "merged list", merged, http.StatusOK)
	if total, _ := merged.obj()["total"].(float64); total != 7 {
		t.Fatalf("total 只计请求行与控制台操作 = %v want 7: %s", total, merged.raw)
	}
	byID := map[string]map[string]any{}
	for _, item := range merged.logs() {
		id, _ := item["id"].(string)
		byID[id] = item
	}
	for id, want := range map[string]string{
		"l-run": "RUNNING", "l-run-ok": "SUCCESS", "l-run-fail": "FAILED",
	} {
		if got := byID[id]["status"]; got != want {
			t.Fatalf("%s 状态 = %v want %s: %s", id, got, want, merged.raw)
		}
	}
	running := alice.post("/api/dashboard/log/list", `{"projectId":"cube-a","status":"RUNNING"}`)
	if got := running.logs(); len(got) != 1 || got[0]["id"] != "l-run" {
		t.Fatalf("RUNNING 过滤应只剩 l-run: %s", running.raw)
	}
	if total, _ := running.obj()["total"].(float64); total != 1 {
		t.Fatalf("RUNNING total = %v want 1: %s", total, running.raw)
	}
	if got := alice.post("/api/dashboard/log/list", `{"projectId":"cube-a","status":"FAILED"}`).logs(); len(got) != 2 {
		t.Fatalf("FAILED 过滤应有 2 条（同步失败 + 终态失败）: %v", got)
	}

	failedDetail := alice.post("/api/dashboard/log/detail", `{"logId":"l-run-fail"}`)
	expect(t, "failed detail", failedDetail, http.StatusOK)
	if failedDetail.str("status") != "FAILED" ||
		!strings.Contains(failedDetail.str("responseBody"), "upstream_rejected") ||
		failedDetail.str("requestBody") == "" {
		t.Fatalf("失败详情应与列表同口径并取终态行正文: %s", failedDetail.raw)
	}
	runningDetail := alice.post("/api/dashboard/log/detail", `{"logId":"l-run"}`)
	expect(t, "running detail", runningDetail, http.StatusOK)
	if runningDetail.str("status") != "RUNNING" {
		t.Fatalf("RUNNING 详情: %s", runningDetail.raw)
	}
	expect(t, "non-member list", alice.post("/api/dashboard/log/list", `{"projectId":"cube-b"}`), http.StatusForbidden)

	detail := alice.post("/api/dashboard/log/detail", `{"logId":"l-search"}`)
	expect(t, "detail", detail, http.StatusOK)
	if detail.str("traceId") != "sess-1" || detail.str("responseBody") != `{"ok":true}` || detail.str("userId") != "cube-a" {
		t.Fatalf("详情字段: %s", detail.raw)
	}
	for _, id := range []string{"l-other", "missing"} {
		expect(t, "detail "+id, alice.post("/api/dashboard/log/detail", `{"logId":"`+id+`"}`), http.StatusForbidden)
	}
	expect(t, "console detail", alice.post("/api/dashboard/log/detail", `{"logId":"l-dash"}`), http.StatusOK)
	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	expect(t, "root sees other project", root.post("/api/dashboard/log/detail", `{"logId":"l-other"}`), http.StatusOK)
	expect(t, "root missing", root.post("/api/dashboard/log/detail", `{"logId":"missing"}`), http.StatusNotFound)

	usage := alice.post("/api/dashboard/usage/query", `{"projectId":"cube-a","startDate":"2026-10-20","endDate":"2026-10-22"}`)
	expect(t, "usage", usage, http.StatusOK)
	stats, _ := usage.obj()["dailyStats"].([]any)
	want := []struct {
		date        string
		add, search float64
	}{{"2026-10-20", 0, 1}, {"2026-10-21", 2, 0}, {"2026-10-22", 0, 0}}
	if len(stats) != len(want) {
		t.Fatalf("应逐天补齐 3 天: %s", usage.raw)
	}
	for i, w := range want {
		day, _ := stats[i].(map[string]any)
		add, _ := day["addMessage"].(map[string]any)
		search, _ := day["searchMemory"].(map[string]any)
		if day["date"] != w.date || add["calls"] != w.add || search["calls"] != w.search {
			t.Fatalf("%s 的次数不对（北京日期分桶）: %v", w.date, day)
		}
	}
}
