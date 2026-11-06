package app

import (
	"context"
	"encoding/json"
	"log/slog"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/memory"
)

type activityAuditReader struct{ *store.Store }

func (r activityAuditReader) List(ctx context.Context, f domain.AuditFilter) ([]domain.AuditRecord, int, error) {
	return r.ListAudit(ctx, f)
}

func (r activityAuditReader) Get(ctx context.Context, id string) (domain.AuditRecord, error) {
	return r.GetAudit(ctx, id)
}

type activityMemoryUpstream struct {
	memory.Upstream
	reads int
}

func (*activityMemoryUpstream) Add(context.Context, domain.AddPayload) (domain.AddResult, error) {
	return domain.AddResult{Status: "success", MemoryIDs: []string{"memory-1"}}, nil
}

func (u *activityMemoryUpstream) OwnedMemory(_ context.Context, tenant, _, id string) (domain.Memory, bool, error) {
	u.reads++
	return domain.Memory{ID: id, TenantID: tenant, Content: "验收偏好", Key: "饮食偏好", NodeType: "UserMemory"}, id == "memory-1" && tenant == "cube-a", nil
}

func TestActivityReadsPersistedOutputAfterMetadataOnlyList(t *testing.T) {
	t.Parallel()
	s, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	up := &activityMemoryUpstream{}
	svc := memory.New(up, nil, memory.Options{}, slog.Default())
	result, err := svc.RunAddJob(context.Background(), []byte(`{"request":{"TenantID":"cube-a","UserID":"alice","SessionID":"chat-a","Messages":[{"role":"user","content":"请记住我喜欢茶"}]}}`))
	if err != nil {
		t.Fatal(err)
	}
	output, err := json.Marshal(domain.MemoryWriteOutput{MemoryIDs: result.MemoryIDs, Snapshot: result.Snapshot})
	if err != nil {
		t.Fatal(err)
	}
	recorder, err := audit.New(s, slog.Default(), audit.Options{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = recorder.Close() })
	if err := recorder.RecordNow(context.Background(), domain.AuditRecord{ID: "completed", CallID: "call-activity", TenantID: "cube-a", Tool: domain.ToolAddMessageBackground, OK: true, Output: string(output), TS: time.Now()}); err != nil {
		t.Fatal(err)
	}
	rows, _, err := s.ListAudit(context.Background(), domain.AuditFilter{TenantID: "cube-a", CallID: "call-activity", Limit: 1})
	if err != nil || len(rows) != 1 || rows[0].Output != "" {
		t.Fatalf("list result=%+v err=%v", rows, err)
	}
	got, err := svc.Activity(context.Background(), domain.Caller{TenantID: "cube-a", UserID: "alice"}, []string{"call-activity"}, activityAuditReader{s})
	if err != nil || len(got) != 1 || got[0].Status != "succeeded" || got[0].Total != 1 || len(got[0].Memories) != 1 || up.reads != 1 || got[0].Memories[0].Key != "饮食偏好" {
		t.Fatalf("activity=%+v err=%v", got, err)
	}
}
