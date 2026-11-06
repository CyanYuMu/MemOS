package app

import (
	"context"
	"net/http"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service"
)

// TestE2EDashboardAuditsWritesOnly 控制台只审计变更类操作；自动刷新的读接口不落审计。
func TestE2EDashboardAuditsWritesOnly(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	ctx := context.Background()
	admin := e.adminClient()

	expect(t, "project list", admin.post("/api/dashboard/project/list", `{"pageNum":1,"pageSize":10}`), http.StatusOK)
	expect(t, "key create", admin.post("/api/dashboard/keys/create", `{"projectId":"cube-a","keyName":"audit"}`), http.StatusOK)

	if err := e.app.audit.Stop(ctx); err != nil {
		t.Fatalf("stop audit: %v", err)
	}
	rows, _, err := e.app.store.ListAudit(ctx, service.AuditFilter{})
	if err != nil {
		t.Fatalf("list audit: %v", err)
	}
	for _, row := range rows {
		if row.Tool == "dashboard.project.list" {
			t.Fatalf("读接口不应写审计: %+v", row)
		}
	}
	if len(rows) != 1 || rows[0].Tool != "dashboard.keys.create" {
		t.Fatalf("审计行 = %+v，期望只有 dashboard.keys.create", rows)
	}
	// 来源信息随请求落库：request_id 由中间件生成，remote_ip 取 TCP 对端。
	if rows[0].RequestID == "" {
		t.Fatalf("审计行缺少 request_id: %+v", rows[0])
	}
	if rows[0].RemoteIP == "" {
		t.Fatalf("审计行缺少 remote_ip: %+v", rows[0])
	}
}
