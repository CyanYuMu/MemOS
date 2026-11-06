package store

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestOnboardingMigrationDoesNotInterruptExistingAccounts(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "upgrade.db")
	s, err := Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.CreateAccount(ctx, domain.Account{ID: "existing", Username: "existing", CreatedAt: time.Now()}); err != nil {
		t.Fatal(err)
	}
	for _, sql := range []string{"DROP TABLE onboarding_projects", "DROP TABLE onboarding_accounts", "DELETE FROM schema_migrations WHERE version=33"} {
		if _, err := s.db.ExecContext(ctx, sql); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	old, err := s.GetOnboarding(ctx, "existing", "p")
	if err != nil || old.Status != "dismissed" {
		t.Fatalf("existing: %+v %v", old, err)
	}
	if err := s.CreateAccount(ctx, domain.Account{ID: "new", Username: "new", CreatedAt: time.Now()}); err != nil {
		t.Fatal(err)
	}
	fresh, err := s.GetOnboarding(ctx, "new", "p")
	if err != nil || fresh.Status != "pending" {
		t.Fatalf("new: %+v %v", fresh, err)
	}
}
