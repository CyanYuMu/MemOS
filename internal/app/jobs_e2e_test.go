package app

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 死信查询走真实 SQLite 与真实 HTTP：只含元数据、不泄漏 payload，且只对平台管理员开放。
func TestE2EAdminJobsQuery(t *testing.T) {
	e := newE2E(t)
	for _, job := range []domain.Job{
		{
			ID: "job-dead", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusFailed,
			TenantID: "cube-a", UserID: "alice", Attempts: 3, LastError: "upstream unavailable",
			Payload:   []byte(`{"text":"不该出现的记忆正文"}`),
			CreatedAt: time.Now(), UpdatedAt: time.Now(),
		},
		{
			ID: "job-live", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusPending,
			TenantID: "cube-b", Attempts: 1, Payload: []byte(`{}`),
			CreatedAt: time.Now(), UpdatedAt: time.Now(),
		},
	} {
		if err := e.app.store.EnqueueJob(context.Background(), job); err != nil {
			t.Fatalf("seed %s: %v", job.ID, err)
		}
	}

	got := e.adminClient().get("/admin/jobs?status=failed")
	expect(t, "admin jobs", got, http.StatusOK)
	if !strings.Contains(got.raw, `"id":"job-dead"`) {
		t.Fatalf("缺少死信: %s", got.raw)
	}
	if strings.Contains(got.raw, "job-live") || strings.Contains(got.raw, "不该出现的记忆正文") {
		t.Fatalf("列表不应包含其它状态或 payload: %s", got.raw)
	}

	seedWorld(t, e)
	alice := e.loginAs("alice")
	expect(t, "viewer jobs query", alice.get("/admin/jobs"), http.StatusForbidden)
	expect(t, "invalid status", e.adminClient().get("/admin/jobs?status=bogus"), http.StatusBadRequest)
}
