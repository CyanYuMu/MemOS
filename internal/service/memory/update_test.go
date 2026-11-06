package memory

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type updateUpstream struct {
	*fakeUpstream
	current     domain.Memory
	got         domain.MemoryUpdate
	calls       int
	owned       bool
	stale       bool
	updateErr   error
	versionJump int
}

func (u *updateUpstream) OwnedMemory(context.Context, string, string, string) (domain.Memory, bool, error) {
	return u.current, u.owned, u.err
}

func (u *updateUpstream) UpdateMemory(_ context.Context, in domain.MemoryUpdate) error {
	u.calls++
	u.got = in
	if u.updateErr != nil {
		return u.updateErr
	}
	if !u.stale {
		if u.current.Content != in.Content {
			u.current.Version += 1 + u.versionJump
		}
		if in.ResetContentTags {
			u.current.Tags = nil
		}
		u.current.Content = in.Content
		if in.Title != nil {
			u.current.Key = *in.Title
		}
	}
	return nil
}

type serializedUpdateUpstream struct {
	*fakeUpstream
	mu               sync.Mutex
	current          domain.Memory
	writes           int
	entered, release chan struct{}
	reads            chan int
}

func (u *serializedUpdateUpstream) OwnedMemory(context.Context, string, string, string) (domain.Memory, bool, error) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.reads <- u.current.Version
	return u.current, true, nil
}

func (u *serializedUpdateUpstream) UpdateMemory(_ context.Context, in domain.MemoryUpdate) error {
	u.mu.Lock()
	u.writes++
	first := u.writes == 1
	u.mu.Unlock()
	if first {
		close(u.entered)
		<-u.release
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	u.current.Content = in.Content
	u.current.Version++
	return nil
}

func TestConcurrentUpdatesKeepActualBeforeVersions(t *testing.T) {
	t.Parallel()
	u := &serializedUpdateUpstream{fakeUpstream: &fakeUpstream{}, current: domain.Memory{ID: "m", Content: "original", Version: 1}, entered: make(chan struct{}), release: make(chan struct{}), reads: make(chan int, 8)}
	store := &fakeEventStore{}
	s := New(u, nil, Options{Events: store}, nil)
	done := make(chan error, 2)
	go func() {
		_, err := s.Update(t.Context(), caller(), domain.MemoryUpdate{ID: "m", Content: "first"})
		done <- err
	}()
	<-u.entered
	<-u.reads
	go func() {
		_, err := s.Update(t.Context(), caller(), domain.MemoryUpdate{ID: "m", Content: "second"})
		done <- err
	}()
	select {
	case <-u.reads:
		close(u.release)
		t.Fatal("second update read a stale before while first was in flight")
	case <-time.After(50 * time.Millisecond):
	}
	close(u.release)
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	if len(store.events) != 2 || store.events[0].Before[0].Version != 1 || store.events[1].Before[0].Version != 2 || store.events[1].Memories[0].Version != 3 {
		t.Fatalf("incorrect before chain: %+v", store.events)
	}
}

func TestContentCorrectionClearsCurrentTagsButPreservesBefore(t *testing.T) {
	t.Parallel()
	u := &updateUpstream{fakeUpstream: &fakeUpstream{}, current: domain.Memory{ID: "m", Content: "old tea", Version: 1, Tags: []string{"old tea"}}, owned: true}
	store := &fakeEventStore{}
	s := New(u, nil, Options{Events: store}, nil)
	r, err := s.Update(t.Context(), caller(), domain.MemoryUpdate{ID: "m", Content: "new tea"})
	if err != nil || !u.got.ResetContentTags || len(r.Memory.Tags) != 0 || store.events[0].Before[0].Tags[0] != "old tea" {
		t.Fatalf("tag invalidation: %+v %v", r, err)
	}
	u.versionJump = 1
	if _, err := s.Update(t.Context(), caller(), domain.MemoryUpdate{ID: "m", Content: "external collision"}); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("version gap accepted: %v", err)
	}
	if len(store.events) != 2 || store.events[1].Status != "failed" || len(store.events[1].Memories) != 0 {
		t.Fatal("uncertain before recorded as success or failure lost")
	}
}

func TestCanceledUpdateDoesNotWaitForAnotherWriter(t *testing.T) {
	t.Parallel()
	u := &serializedUpdateUpstream{fakeUpstream: &fakeUpstream{}, current: domain.Memory{ID: "m", Content: "original", Version: 1}, entered: make(chan struct{}), release: make(chan struct{}), reads: make(chan int, 8)}
	s := New(u, nil, Options{}, nil)
	first := make(chan error, 1)
	go func() {
		_, err := s.Update(t.Context(), caller(), domain.MemoryUpdate{ID: "m", Content: "first"})
		first <- err
	}()
	<-u.entered
	<-u.reads
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() {
		_, err := s.Update(ctx, caller(), domain.MemoryUpdate{ID: "m", Content: "canceled"})
		done <- err
	}()
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Error(err)
		}
	case <-time.After(time.Second):
		t.Error("canceled writer kept waiting for lock")
	}
	close(u.release)
	if err := <-first; err != nil {
		t.Fatal(err)
	}
	if u.writes != 1 || len(u.reads) != 1 {
		t.Fatalf("canceled update read/wrote: writes=%d reads=%d", u.writes, len(u.reads))
	}
}

func TestUpdateConfirmsOwnershipContentAndVersion(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name         string
		owned, stale bool
		content      string
		fail, want   error
		calls        int
	}{
		{name: "updated", owned: true, content: "new", calls: 1},
		{name: "no-op", owned: true, content: "old"},
		{name: "foreign", content: "new", want: domain.ErrNotFound},
		{name: "empty", owned: true, content: "  ", want: domain.ErrInvalidInput},
		{name: "upstream failure", owned: true, content: "new", fail: domain.ErrUpstreamUnavailable, want: domain.ErrUpstreamUnavailable, calls: 1},
		{name: "false success", owned: true, stale: true, content: "new", want: domain.ErrConflict, calls: 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			up := &updateUpstream{fakeUpstream: &fakeUpstream{}, current: domain.Memory{ID: "m", Content: "old", Version: 1}, owned: tc.owned, stale: tc.stale, updateErr: tc.fail}
			s := New(up, nil, Options{}, nil)
			result, err := s.Update(context.Background(), caller(), domain.MemoryUpdate{ID: "m", Content: tc.content, TenantID: "forged", UserID: "forged"})
			if !errors.Is(err, tc.want) || up.calls != tc.calls {
				t.Fatalf("err=%v calls=%d", err, up.calls)
			}
			if err == nil && result.Changed != (tc.calls == 1) {
				t.Fatalf("result=%+v", result)
			}
			if up.calls > 0 && (up.got.TenantID != caller().TenantID || up.got.UserID != caller().UserID) {
				t.Fatal("caller scope was overridden")
			}
		})
	}
}

func TestFeedbackRejectsForeignTargetBeforeCallingUpstream(t *testing.T) {
	t.Parallel()
	up := &fakeUpstream{owned: map[string]bool{"mine": true}}
	s := New(up, nil, Options{}, nil)
	_, err := s.Feedback(context.Background(), caller(), domain.FeedbackRequest{Content: "correct", RetrievedMemoryIDs: []string{"mine", "foreign"}})
	if !errors.Is(err, domain.ErrNotFound) || up.feedback.Content != "" {
		t.Fatalf("feedback escaped scope: %v", err)
	}
}
