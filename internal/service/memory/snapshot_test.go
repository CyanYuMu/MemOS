package memory

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type snapshotUpstream struct {
	fakeUpstream
	ids      []string
	memories map[string]domain.Memory
	readErr  error
	reads    int
}

func (u *snapshotUpstream) Add(ctx context.Context, req domain.AddPayload) (domain.AddResult, error) {
	result, err := u.fakeUpstream.Add(ctx, req)
	result.MemoryIDs = u.ids
	return result, err
}

func (u *snapshotUpstream) OwnedMemory(ctx context.Context, tenant, user, id string) (domain.Memory, bool, error) {
	u.reads++
	if ctx.Err() != nil {
		return domain.Memory{}, false, ctx.Err()
	}
	m, ok := u.memories[id]
	if tenant != "p1" || user != "u1" {
		return domain.Memory{}, false, errors.New("wrong scope")
	}
	return m, ok, u.readErr
}

func snapshotJob(t *testing.T) []byte {
	t.Helper()
	raw, err := json.Marshal(addJobPayload{Request: domain.AddPayload{TenantID: "p1", UserID: "u1", SessionID: "session-1", Messages: []domain.Message{{Role: "user", Content: "请记住我喜欢茶"}}}})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestAddSnapshotSurvivesUpdateDeletionAndSerialization(t *testing.T) {
	t.Parallel()
	up := &snapshotUpstream{ids: []string{"m1", "m1", ""}, memories: map[string]domain.Memory{"m1": {
		ID: "m1", TenantID: "p1", Key: "饮食偏好", Content: "喜欢茶", Tags: []string{"茶"},
		NodeType: "UserMemory", Status: "activated", Version: 1, UpdatedAt: domain.UTCTime{Time: time.Now().UTC()},
	}}}
	svc := New(up, nil, Options{}, nil)
	result, err := svc.RunAddJob(context.Background(), snapshotJob(t))
	if err != nil || result.Snapshot == nil || result.Snapshot.Truncated || up.addCalls != 1 || up.reads != 1 {
		t.Fatalf("capture: %+v, err=%v writes=%d reads=%d", result.Snapshot, err, up.addCalls, up.reads)
	}
	body, err := json.Marshal(domain.MemoryWriteOutput{MemoryIDs: result.MemoryIDs, Snapshot: result.Snapshot})
	if err != nil {
		t.Fatal(err)
	}
	delete(up.memories, "m1")
	up.readErr = errors.New("deleted")
	reader := &activityReader{records: []domain.AuditRecord{{TenantID: "p1", CallID: "c1", Tool: domain.ToolAddMessageBackground, OK: true, Output: string(body)}}}
	for range 2 {
		rows, readErr := svc.Activity(context.Background(), domain.Caller{TenantID: "p1", UserID: "u1"}, []string{"c1"}, reader)
		if readErr != nil || len(rows) != 1 || len(rows[0].Memories) != 1 || rows[0].Memories[0].Content != "喜欢茶" || rows[0].Memories[0].Version != 1 || up.reads != 1 {
			t.Fatalf("snapshot changed after deletion/replay: %+v %v reads=%d", rows, readErr, up.reads)
		}
	}
	public, err := json.Marshal(result)
	if err != nil || strings.Contains(string(public), "喜欢茶") || strings.Contains(string(public), "snapshot") {
		t.Fatalf("internal snapshot leaked into tool result: %s %v", public, err)
	}
}

func TestSnapshotFailuresNeverRetrySuccessfulExtraction(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		mem  domain.Memory
		err  error
	}{
		{"read-error", domain.Memory{ID: "m1"}, errors.New("read unavailable")},
		{"wrong-id", domain.Memory{ID: "different"}, nil},
		{"foreign", domain.Memory{ID: "m1", TenantID: "other"}, nil},
		{"foreign-user", domain.Memory{ID: "m1", TenantID: "p1", UserID: "other"}, nil},
		{"oversize", domain.Memory{ID: "m1", Content: strings.Repeat("大", maxSnapshotBytes)}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			up := &snapshotUpstream{ids: []string{"m1"}, memories: map[string]domain.Memory{"m1": tc.mem}, readErr: tc.err}
			result, err := New(up, nil, Options{}, nil).RunAddJob(context.Background(), snapshotJob(t))
			if err != nil || result.Status != "success" || up.addCalls != 1 || result.Snapshot == nil || !result.Snapshot.Truncated || len(result.Snapshot.Memories) != 0 {
				t.Fatalf("snapshot error changed write outcome: %+v %v writes=%d", result, err, up.addCalls)
			}
		})
	}
}

func TestSnapshotLimitsReadCountAndKeepsSmallEntitiesComplete(t *testing.T) {
	t.Parallel()
	up := &snapshotUpstream{memories: map[string]domain.Memory{}}
	for i := range maxSnapshotMemories + 1 {
		id := fmt.Sprintf("m%d", i)
		up.ids = append(up.ids, id)
		up.memories[id] = domain.Memory{ID: id, Content: "完整内容"}
	}
	result, err := New(up, nil, Options{}, nil).RunAddJob(context.Background(), snapshotJob(t))
	if err != nil || result.Snapshot == nil || !result.Snapshot.Truncated || up.reads != maxSnapshotMemories || len(result.Snapshot.Memories) != maxSnapshotMemories {
		t.Fatalf("unbounded snapshot: %+v %v reads=%d", result.Snapshot, err, up.reads)
	}
}

func TestSnapshotRespectsExistingJobDeadline(t *testing.T) {
	t.Parallel()
	up := &snapshotUpstream{ids: []string{"m1"}, memories: map[string]domain.Memory{"m1": {ID: "m1"}}}
	ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	// 假上游模拟已在期限边界完成的写入；快照不再延长任务。
	result, err := New(up, nil, Options{}, nil).RunAddJob(ctx, snapshotJob(t))
	if err != nil || up.addCalls != 1 || up.reads != 0 || result.Snapshot == nil || !result.Snapshot.Truncated {
		t.Fatalf("snapshot exceeded job budget: %+v %v writes=%d reads=%d", result.Snapshot, err, up.addCalls, up.reads)
	}
}

func TestOversizedSnapshotEnvelopeKeepsConfirmedIDsParseable(t *testing.T) {
	t.Parallel()
	up := &snapshotUpstream{ids: []string{"m1"}, memories: map[string]domain.Memory{"m1": {ID: "m1"}}}
	var job addJobPayload
	if err := json.Unmarshal(snapshotJob(t), &job); err != nil {
		t.Fatal(err)
	}
	job.Request.SessionID = strings.Repeat("s", 300<<10)
	payload, err := json.Marshal(job)
	if err != nil {
		t.Fatal(err)
	}
	result, err := New(up, nil, Options{}, nil).RunAddJob(context.Background(), payload)
	if err != nil || result.Snapshot != nil || up.addCalls != 1 || up.reads != 0 {
		t.Fatalf("oversize envelope not discarded: %+v %v", result.Snapshot, err)
	}
	body, err := json.Marshal(domain.MemoryWriteOutput{MemoryIDs: result.MemoryIDs, Snapshot: result.Snapshot})
	var stored domain.MemoryWriteOutput
	if err != nil || len(body) >= 256<<10 || json.Unmarshal(body, &stored) != nil || len(stored.MemoryIDs) != 1 || stored.MemoryIDs[0] != "m1" {
		t.Fatalf("confirmation became unparseable: bytes=%d %v", len(body), err)
	}
}
