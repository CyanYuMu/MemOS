package memory

import (
	"context"
	"errors"
	"log/slog"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type activityReader struct {
	records []domain.AuditRecord
	filters []domain.AuditFilter
}

func (r *activityReader) List(_ context.Context, f domain.AuditFilter) ([]domain.AuditRecord, int, error) {
	r.filters = append(r.filters, f)
	var rows []domain.AuditRecord
	for i := range r.records {
		row := &r.records[i]
		if row.TenantID == f.TenantID && (f.CallID == "" || row.CallID == f.CallID) && row.Tool == f.Tool && (f.Since.IsZero() || !row.TS.Before(f.Since)) {
			metadata := *row
			if metadata.ID == "" {
				metadata.ID = metadata.TenantID + "/" + metadata.CallID
			}
			metadata.Output = ""
			rows = append(rows, metadata)
		}
	}
	return rows, len(rows), nil
}

func (r *activityReader) Get(_ context.Context, id string) (domain.AuditRecord, error) {
	for i := range r.records {
		row := r.records[i]
		if row.ID == "" {
			row.ID = row.TenantID + "/" + row.CallID
		}
		if row.ID == id {
			return row, nil
		}
	}
	return domain.AuditRecord{}, domain.NotFound("audit", "missing")
}

func TestActivityReturnsOnlyConfirmedOwnedMemories(t *testing.T) {
	t.Parallel()
	up := &fakeUpstream{owned: map[string]bool{"own": true}}
	svc := New(up, nil, Options{}, slog.Default())
	r := &activityReader{records: []domain.AuditRecord{{TenantID: "p1", CallID: "call-1", Tool: domain.ToolAddMessageBackground, OK: true, Output: `{"added_memory_ids":["own","foreign","own"],"snapshot":{"schema_version":1,"tenant_id":"p1","user_id":"u1","captured_at":"2026-10-31T01:02:03Z","truncated":true,"memories":[{"ID":"own","TenantID":"p1","UserID":"u1","Content":"自己的记忆"}]}}`}, {TenantID: "p2", CallID: "secret", Tool: domain.ToolAddMessageBackground, OK: true, Output: `{"added_memory_ids":["own"]}`}}}
	got, err := svc.Activity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, []string{"call-1", "secret"}, r)
	if err != nil || len(got) != 2 || got[0].Status != "succeeded" || got[0].Total != 2 || len(got[0].Memories) != 1 || got[0].Memories[0].ID != "own" || got[1].Status != "not_found" {
		t.Fatalf("results=%+v err=%v", got, err)
	}
}

func TestActivityKeepsLegacyAndPendingSeparate(t *testing.T) {
	t.Parallel()
	svc := New(&fakeUpstream{}, nil, Options{}, slog.Default())
	r := &activityReader{records: []domain.AuditRecord{{TenantID: "p1", CallID: "old", Tool: domain.ToolAddMessageBackground, OK: true}, {TenantID: "p1", CallID: "running", Tool: domain.ToolAddMessage, Status: domain.AuditStatusRunning}, {TenantID: "p1", CallID: "failed", Tool: domain.ToolAddMessageBackground, OK: false}}}
	got, err := svc.Activity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, []string{"old", "running", "failed"}, r)
	if err != nil || got[0].Status != "unavailable" || got[1].Status != "pending" || got[2].Status != "failed" {
		t.Fatalf("results=%+v err=%v", got, err)
	}
	if _, err := svc.Activity(context.Background(), domain.Caller{TenantID: "p1"}, make([]string, 21), r); err == nil {
		t.Fatal("oversized query accepted")
	}
}

func TestRecentActivityUsesRealTimeCursor(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 10, 31, 1, 2, 3, 0, time.UTC)
	svc := New(&fakeUpstream{owned: map[string]bool{"m1": true}}, nil, Options{}, slog.Default())
	r := &activityReader{records: []domain.AuditRecord{
		{TenantID: "p1", CallID: "older", Tool: domain.ToolAddMessageBackground, TS: at, OK: true, Output: `{"added_memory_ids":["m1"]}`},
		{TenantID: "p1", CallID: "newer", Tool: domain.ToolAddMessageBackground, TS: at.Add(time.Second), OK: true, Output: `{"added_memory_ids":["m1"]}`},
		{TenantID: "p2", CallID: "private", Tool: domain.ToolAddMessageBackground, TS: at.Add(time.Second), OK: true, Output: `{"added_memory_ids":["m1"]}`},
	}}
	got, err := svc.RecentActivity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, at.Format(time.RFC3339Nano), r)
	if err != nil || len(got) != 1 || got[0].CallID != "newer" || got[0].Total != 1 {
		t.Fatalf("results=%+v err=%v", got, err)
	}
}

func TestActivityKeepsOriginalCountAfterMemoryDeletion(t *testing.T) {
	t.Parallel()
	svc := New(&fakeUpstream{}, nil, Options{}, slog.Default())
	reader := &activityReader{records: []domain.AuditRecord{{TenantID: "p1", CallID: "write", Tool: domain.ToolAddMessageBackground, OK: true, Output: `{"added_memory_ids":["deleted-1","deleted-2"]}`}}}
	rows, err := svc.Activity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, []string{"write"}, reader)
	if err != nil || len(rows) != 1 || rows[0].Total != 2 || len(rows[0].Memories) != 0 {
		t.Fatalf("historical count changed: %+v %v", rows, err)
	}
}

func TestActivityReadsHistoricalSnapshotWithoutCurrentLookup(t *testing.T) {
	t.Parallel()
	at := time.Date(2026, 10, 31, 1, 2, 3, 0, time.UTC)
	up := &fakeUpstream{err: errors.New("current memory was deleted")}
	svc := New(up, nil, Options{}, nil)
	reader := &activityReader{records: []domain.AuditRecord{{
		TenantID: "p1", UserID: "u1", CallID: "write", Tool: domain.ToolAddMessageBackground,
		TS: at, OK: true, Output: `{"added_memory_ids":["m1"],"snapshot":{"schema_version":1,"tenant_id":"p1","user_id":"u1","session_id":"session-a","captured_at":"2026-10-31T01:02:03Z","memories":[{"ID":"m1","TenantID":"p1","UserID":"u1","Content":"过去喜欢茶","Key":"饮食偏好","NodeType":"UserMemory","Status":"activated","Tags":["茶"],"UpdatedAt":"2026-10-31T01:02:03Z","Version":1}]}}`,
	}}}
	rows, err := svc.Activity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, []string{"write"}, reader)
	if err != nil || len(rows) != 1 || rows[0].Status != "succeeded" || len(rows[0].Memories) != 1 {
		t.Fatalf("historical read: %+v %v", rows, err)
	}
	m := rows[0].Memories[0]
	if m.ID != "m1" || m.Content != "过去喜欢茶" || m.Key != "饮食偏好" || m.NodeType != "UserMemory" || m.Status != "activated" || len(m.Tags) != 1 || m.UpdatedAt.String() != at.Format(time.RFC3339) || m.Version != 1 {
		t.Fatalf("incomplete snapshot: %+v", m)
	}
}

func TestActivityDoesNotBackfillLegacyFromCurrentMemory(t *testing.T) {
	t.Parallel()
	svc := New(&fakeUpstream{err: errors.New("must not read latest memory")}, nil, Options{}, nil)
	reader := &activityReader{records: []domain.AuditRecord{{TenantID: "p1", CallID: "old", Tool: domain.ToolAddMessageBackground, OK: true, Output: `{"added_memory_ids":["m1"]}`}}}
	rows, err := svc.Activity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, []string{"old"}, reader)
	if err != nil || len(rows) != 1 || rows[0].Status != "unavailable" || rows[0].Total != 1 || len(rows[0].Memories) != 0 {
		t.Fatalf("legacy incorrectly backfilled: %+v %v", rows, err)
	}
}

func TestActivityRejectsMismatchedSnapshotScope(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct{ name, snapshot string }{
		{"tenant", `{"schema_version":1,"tenant_id":"other","user_id":"u1","captured_at":"2026-10-31T01:02:03Z","memories":[{"ID":"m1","TenantID":"p1","UserID":"u1","Content":"private"}]}`},
		{"user", `{"schema_version":1,"tenant_id":"p1","user_id":"other","captured_at":"2026-10-31T01:02:03Z","memories":[{"ID":"m1","TenantID":"p1","UserID":"u1","Content":"private"}]}`},
		{"unconfirmed", `{"schema_version":1,"tenant_id":"p1","user_id":"u1","captured_at":"2026-10-31T01:02:03Z","memories":[{"ID":"other","TenantID":"p1","UserID":"u1","Content":"private"}]}`},
		{"future-schema", `{"schema_version":2,"tenant_id":"p1","user_id":"u1","captured_at":"2026-10-31T01:02:03Z","memories":[{"ID":"m1","TenantID":"p1","UserID":"u1","Content":"private"}]}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			svc := New(&fakeUpstream{owned: map[string]bool{"m1": true}}, nil, Options{}, nil)
			reader := &activityReader{records: []domain.AuditRecord{{TenantID: "p1", CallID: "write", Tool: domain.ToolAddMessageBackground, OK: true, Output: `{"added_memory_ids":["m1"],"snapshot":` + tc.snapshot + `}`}}}
			rows, err := svc.Activity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, []string{"write"}, reader)
			if err != nil || len(rows) != 1 || len(rows[0].Memories) != 0 || rows[0].Status != "unavailable" {
				t.Fatalf("mismatched snapshot leaked: %+v %v", rows, err)
			}
		})
	}
}
