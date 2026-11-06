package memory

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type fakeEventStore struct {
	events []domain.MemoryEvent
	err    error
}

func (s *fakeEventStore) MemoryEventsByCalls(_ context.Context, tenant string, ids []string) ([]domain.MemoryEvent, error) {
	out := []domain.MemoryEvent{}
	for i := range s.events {
		e := &s.events[i]
		for _, id := range ids {
			if e.TenantID == tenant && e.CallID == id {
				out = append(out, *e)
				break
			}
		}
	}
	return out, s.err
}

func (s *fakeEventStore) AppendMemoryEvent(_ context.Context, e domain.MemoryEvent) error {
	if s.err != nil {
		return s.err
	}
	for i := range s.events {
		if s.events[i].TenantID == e.TenantID && s.events[i].Key == e.Key {
			return nil
		}
	}
	body, _ := json.Marshal(e)
	var saved domain.MemoryEvent
	_ = json.Unmarshal(body, &saved)
	saved.Sequence = int64(len(s.events) + 1)
	s.events = append(s.events, saved)
	return nil
}

func (s *fakeEventStore) ListMemoryEvents(_ context.Context, tenant string, after int64, limit int, latest bool) ([]domain.MemoryEvent, error) {
	out := []domain.MemoryEvent{}
	for i := range s.events {
		if s.events[i].TenantID == tenant && s.events[i].Sequence > after {
			out = append(out, s.events[i])
		}
	}
	if latest {
		for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
			out[i], out[j] = out[j], out[i]
		}
	}
	return out[:min(limit, len(out))], s.err
}

func TestEventsCaptureActualRecallAndRejectCrossProjectCursor(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{}
	up := &fakeUpstream{searchResult: domain.SearchResult{Facts: []domain.Memory{{ID: "m1", Content: "喜欢茶", Key: "饮食偏好", Version: 1}}}}
	svc := New(up, nil, Options{Events: store}, nil)
	ctx := WithEventScope(context.Background(), "call-search", "session-a")
	if _, err := svc.Search(ctx, caller(), domain.SearchQuery{Text: "饮食偏好"}); err != nil {
		t.Fatal(err)
	}
	if len(store.events) != 1 || store.events[0].Kind != "recall" || store.events[0].CallID != "call-search" || store.events[0].Memories[0].Content != "喜欢茶" {
		t.Fatalf("missing recall: %+v", store.events)
	}
	up.searchResult.Facts[0].Content = "later changed"
	page, err := svc.Events(context.Background(), caller(), "")
	if err != nil || len(page.Records) != 1 || page.Records[0].Memories[0].Content != "喜欢茶" {
		t.Fatalf("event changed: %+v %v", page, err)
	}
	foreign := caller()
	foreign.TenantID = "foreign"
	if _, err := svc.Events(context.Background(), foreign, page.Cursor); err == nil {
		t.Fatal("cross-project cursor accepted")
	}
}

func TestHistoricalEventsRejectMixedCursorAndOversizedIDs(t *testing.T) {
	t.Parallel()
	s := New(&fakeUpstream{}, nil, Options{Events: &fakeEventStore{events: []domain.MemoryEvent{{TenantID: caller().TenantID, CallID: "old", ID: "e1"}}}}, nil)
	page, err := s.Events(t.Context(), caller(), "", []string{"old", "old"})
	if err != nil || len(page.Records) != 1 || page.Cursor != "" {
		t.Fatalf("lookup: %+v %v", page, err)
	}
	for _, ids := range [][]string{{""}, make([]string, 21)} {
		if _, err := s.Events(t.Context(), caller(), "", ids); err == nil {
			t.Fatal("invalid lookup accepted")
		}
	}
	if _, err := s.Events(t.Context(), caller(), "cursor", []string{"old"}); err == nil {
		t.Fatal("mixed cursor accepted")
	}
}

func TestUpdateEventsKeepBeforeAfterAndSkipNoOp(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{}
	up := &updateUpstream{fakeUpstream: &fakeUpstream{}, current: domain.Memory{ID: "m1", Content: "喜欢茶", Version: 1}, owned: true}
	svc := New(up, nil, Options{Events: store}, nil)
	ctx := WithEventScope(context.Background(), "update-1", "session-a")
	for range 2 {
		if _, err := svc.Update(ctx, caller(), domain.MemoryUpdate{ID: "m1", Content: "喜欢咖啡"}); err != nil {
			t.Fatal(err)
		}
	}
	if len(store.events) != 1 || store.events[0].Kind != "update" || store.events[0].Before[0].Content != "喜欢茶" || store.events[0].Memories[0].Content != "喜欢咖啡" || store.events[0].Memories[0].Version != 2 {
		t.Fatalf("wrong update events: %+v", store.events)
	}
}

func TestEventFailureCannotRepeatSuccessfulSideEffect(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{err: errors.New("event database unavailable")}
	up := &updateUpstream{fakeUpstream: &fakeUpstream{}, current: domain.Memory{ID: "m1", Content: "old", Version: 1}, owned: true}
	result, err := New(up, nil, Options{Events: store}, nil).Update(context.Background(), caller(), domain.MemoryUpdate{ID: "m1", Content: "new"})
	if err != nil || !result.Changed || up.calls != 1 {
		t.Fatalf("event failure changed operation: %+v %v calls=%d", result, err, up.calls)
	}
}

func TestDeleteEventRetainsBeforeSnapshotAfterRemoval(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{}
	up := &snapshotUpstream{memories: map[string]domain.Memory{"m1": {ID: "m1", Content: "删除前内容", Key: "偏好", Version: 3, Status: "activated"}}}
	up.owned = map[string]bool{"m1": true}
	c := domain.Caller{TenantID: "p1", UserID: "u1"}
	svc := New(up, nil, Options{Events: store}, nil)
	got, err := svc.Delete(WithEventScope(context.Background(), "delete-1", "chat-a"), c, []string{"m1"})
	if err != nil || len(got.DeletedIDs) != 1 || len(store.events) != 1 {
		t.Fatalf("delete: %+v %v events=%+v", got, err, store.events)
	}
	delete(up.memories, "m1")
	page, err := svc.Events(context.Background(), c, "")
	if err != nil || len(page.Records) != 1 || page.Records[0].Before[0].Content != "删除前内容" || page.Records[0].Before[0].Version != 3 || up.reads != 1 {
		t.Fatalf("delete snapshot changed: %+v %v reads=%d", page, err, up.reads)
	}
}

func TestAsyncAddEventUsesOriginalJobCallIDAcrossReplay(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{}
	jobs := &fakeJobs{}
	up := &snapshotUpstream{ids: []string{"m1"}, memories: map[string]domain.Memory{"m1": {ID: "m1", Content: "喜欢茶"}}}
	svc := New(up, jobs, Options{Events: store, AddAsync: true, Idem: newFakeIdem()}, nil)
	req := domain.AddRequest{Messages: json.RawMessage(`[{"role":"user","content":"请记住我喜欢茶"}]`), Async: true, CallID: "add-1", SessionID: "chat-a"}
	c := domain.Caller{TenantID: "p1", UserID: "u1"}
	accepted, err := svc.Add(context.Background(), c, req)
	if err != nil || !accepted.Deferred || len(store.events) != 0 {
		t.Fatalf("accepted treated as extraction: %+v %v events=%+v", accepted, err, store.events)
	}
	if _, err := svc.RunAddJob(context.Background(), jobs.job.Payload); err != nil {
		t.Fatal(err)
	}
	svc.ReportJobOutcome(context.Background(), jobs.job.Payload, nil)
	req.CallID = "replay-call"
	if _, err := svc.Add(context.Background(), c, req); err != nil {
		t.Fatal(err)
	}
	if up.addCalls != 1 || jobs.count != 1 || len(store.events) != 1 || store.events[0].CallID != "add-1" || store.events[0].SessionID != "chat-a" || store.events[0].Memories[0].Content != "喜欢茶" {
		t.Fatalf("replay lost association: writes=%d jobs=%d events=%+v", up.addCalls, jobs.count, store.events)
	}
}

func TestPermanentAddFailureEventHasNoInventedMemories(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{}
	svc := New(&fakeUpstream{}, nil, Options{Events: store}, nil)
	payload, _ := json.Marshal(addJobPayload{Request: domain.AddPayload{TenantID: "p1", UserID: "u1"}, CallID: "failed-1"})
	for range 2 {
		svc.ReportJobOutcome(context.Background(), payload, errors.New("extraction failed"))
	}
	if len(store.events) != 1 || store.events[0].Status != "failed" || len(store.events[0].Memories) != 0 || store.events[0].Total != 0 {
		t.Fatalf("failure fabricated data: %+v", store.events)
	}
}

func TestEventCursorCatchesUpEveryPageAndStaysProjectScoped(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{}
	store.events = []domain.MemoryEvent{{ID: "first", TenantID: caller().TenantID, Sequence: 1}}
	svc := New(&fakeUpstream{}, nil, Options{Events: store}, nil)
	initial, err := svc.Events(context.Background(), caller(), "")
	if err != nil {
		t.Fatal(err)
	}
	for i := range 60 {
		store.events = append(store.events, domain.MemoryEvent{ID: "next", TenantID: caller().TenantID, Sequence: int64(i + 2)})
	}
	first, err := svc.Events(context.Background(), caller(), initial.Cursor)
	if err != nil || len(first.Records) != 50 || !first.HasMore {
		t.Fatalf("first catchup: %+v %v", first, err)
	}
	second, err := svc.Events(context.Background(), caller(), first.Cursor)
	if err != nil || len(second.Records) != 10 || second.HasMore || second.Records[9].Sequence != 61 {
		t.Fatalf("second catchup: %+v %v", second, err)
	}
}

func TestOldQueuedPayloadRestoresActualIdentityAndNeverInventsAssociation(t *testing.T) {
	t.Parallel()
	store := &fakeEventStore{}
	up := &snapshotUpstream{ids: []string{"m1"}, memories: map[string]domain.Memory{"m1": {ID: "m1", Content: "旧任务实际结果"}}}
	svc := New(up, nil, Options{Events: store}, nil)
	payload := snapshotJob(t)
	ctx := svc.WithJobIdentity(context.Background(), domain.Job{ID: "actual-old-job", TenantID: "p1", UserID: "u1"})
	for range 2 {
		if _, err := svc.RunAddJob(ctx, payload); err != nil {
			t.Fatal(err)
		}
	}
	if len(store.events) != 1 || store.events[0].CallID != "actual-old-job" {
		t.Fatalf("old job association invented: %+v", store.events)
	}
	unboundStore := &fakeEventStore{}
	unbound := New(up, nil, Options{Events: unboundStore}, nil)
	if _, err := unbound.RunAddJob(context.Background(), payload); err != nil {
		t.Fatal(err)
	}
	if len(unboundStore.events) != 0 {
		t.Fatal("unbound old payload fabricated call ID")
	}
	before := up.addCalls
	bad := svc.WithJobIdentity(context.Background(), domain.Job{ID: "foreign-job", TenantID: "foreign", UserID: "u1"})
	if _, err := svc.RunAddJob(bad, payload); err == nil || up.addCalls != before {
		t.Fatalf("job scope mismatch executed: %v", err)
	}
}

type profileEventUpstream struct {
	*fakeUpstream
	result domain.ProfileResult
}

func (u *profileEventUpstream) Profile(context.Context, domain.ProfileQuery) (domain.ProfileResult, error) {
	return u.result, u.err
}

func TestProfileReadPersistsActualRecallSnapshot(t *testing.T) {
	t.Parallel()
	for _, failed := range []bool{false, true} {
		t.Run(strconv.FormatBool(failed), func(t *testing.T) {
			t.Parallel()
			store := &fakeEventStore{}
			up := &profileEventUpstream{fakeUpstream: &fakeUpstream{}, result: domain.ProfileResult{
				Facts:       []domain.Memory{{ID: "identity", Key: "称呼", Content: "小林", Version: 1}},
				Preferences: []domain.Memory{{ID: "tea", Content: "茉莉花茶", Version: 1}},
			}}
			if failed {
				up.err = errors.New("profile unavailable")
			}
			svc := New(up, nil, Options{Events: store}, nil)
			ctx := WithEventScope(context.Background(), "profile-call", "actual-session")
			_, err := svc.Profile(ctx, caller(), domain.ProfileQuery{})
			if (err != nil) != failed {
				t.Fatalf("unexpected profile error: %v", err)
			}
			if len(store.events) != 1 {
				t.Fatalf("missing profile read event: %+v", store.events)
			}
			e := store.events[0]
			if e.Kind != "recall" || e.CallID != "profile-call" || e.SessionID != "actual-session" {
				t.Fatalf("wrong scope: %+v", e)
			}
			if failed {
				if e.Status != "failed" || len(e.Memories) != 0 {
					t.Fatalf("failed read became success: %+v", e)
				}
				return
			}
			if e.Total != 2 || len(e.Memories) != 2 || e.Memories[0].Version != 1 {
				t.Fatalf("wrong snapshot: %+v", e)
			}
			up.result.Facts[0].Content = "later changed"
			if store.events[0].Memories[0].Content != "小林" {
				t.Fatal("profile event changed with current source")
			}
		})
	}
}
