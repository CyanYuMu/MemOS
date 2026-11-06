package onboarding

import (
	"context"
	"errors"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type memoryStore struct{ progress domain.OnboardingProgress }

func (m *memoryStore) GetOnboarding(context.Context, string, string) (domain.OnboardingProgress, error) {
	return m.progress, nil
}

func (m *memoryStore) SaveOnboarding(_ context.Context, _, _ string, p domain.OnboardingProgress) error {
	p.Revision++
	m.progress = p
	return nil
}

func TestGuideTransitions(t *testing.T) {
	t.Parallel()
	m := &memoryStore{progress: domain.OnboardingProgress{Status: "pending", Version: 1}}
	s := New(m)
	ctx := context.Background()
	p := domain.Principal{Kind: domain.PrincipalAccount, AccountID: "a", Roles: map[string]domain.Role{"p": domain.RoleOwner}}
	if _, err := s.Update(ctx, p, "p", "advance", 0); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("advance before start: %v", err)
	}
	if _, err := s.Update(ctx, p, "p", "start", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Update(ctx, p, "p", "advance", 0); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("stale: %v", err)
	}
	for range 6 {
		if _, err := s.Update(ctx, p, "p", "advance", m.progress.Revision); err != nil {
			t.Fatal(err)
		}
	}
	if m.progress.Status != "completed" || m.progress.Step != 6 {
		t.Fatalf("finish: %+v", m.progress)
	}
	if _, err := s.Update(ctx, p, "p", "replay", m.progress.Revision); err != nil {
		t.Fatal(err)
	}
	if m.progress.Status != "active" || m.progress.Step != 0 {
		t.Fatalf("replay: %+v", m.progress)
	}
	if _, err := s.Get(ctx, p, "other"); !errors.Is(err, domain.ErrForbidden) {
		t.Fatalf("foreign project: %v", err)
	}
	p.Kind = domain.PrincipalAdminToken
	if _, err := s.Get(ctx, p, "p"); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("token identity: %v", err)
	}
}

func (m *memoryStore) GetChatConversation(context.Context, string, string, string) (domain.ChatConversation, error) {
	return domain.ChatConversation{}, nil
}

func TestBackPreservesChatCheckpoint(t *testing.T) {
	t.Parallel()
	m := &memoryStore{progress: domain.OnboardingProgress{Status: "active", Step: 3, Revision: 7, Version: 1, Chat: &domain.OnboardingCheckpoint{Stage: "first", First: "conversation"}}}
	s := New(m)
	p := domain.Principal{Kind: domain.PrincipalAccount, AccountID: "a", Roles: map[string]domain.Role{"p": domain.RoleOwner}}
	got, err := s.Update(context.Background(), p, "p", "back", 7)
	if err != nil {
		t.Fatal(err)
	}
	if got.Step != 2 || got.Revision != 8 || got.Chat == nil || got.Chat.First != "conversation" {
		t.Fatalf("back: %+v", got)
	}
	if _, err = s.Update(context.Background(), p, "p", "back", 7); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("stale back: %v", err)
	}
	m.progress.Step = 0
	if _, err = s.Update(context.Background(), p, "p", "back", m.progress.Revision); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("before first: %v", err)
	}
}
