package app

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// TestE2ESeedAdminTakesOverPasswordlessAccount 零信任可能先用种子用户名自动建号
// （空口令）。之后配了种子口令不能让“已存在”静默跳过，否则既没有管理员也登不进去。
func TestE2ESeedAdminTakesOverPasswordlessAccount(t *testing.T) {
	t.Parallel()
	cfg := testConfig(t, config.RoleConsole)

	ctx := context.Background()
	st, err := store.Open(ctx, cfg.Store.Path)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	placeholder := domain.Account{
		ID:        domain.NewID(),
		Username:  e2eSeedUser,
		CreatedAt: time.Now(),
	}
	if err := st.CreateAccount(ctx, placeholder); err != nil {
		t.Fatalf("create placeholder: %v", err)
	}
	if err := st.Close(); err != nil {
		t.Fatalf("close store: %v", err)
	}

	cfg.Auth = config.AuthConfig{
		SeedUser:           e2eSeedUser,
		SeedPassword:       e2eSeedPass,
		SessionTTL:         12 * time.Hour,
		SessionMaxLifetime: 30 * 24 * time.Hour,
		AllowPassword:      true,
	}
	a, err := New(Options{Config: cfg, Logger: testLogger()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	srv := httptest.NewServer(a.Handlers()["console"])
	t.Cleanup(func() {
		srv.Close()
		_ = a.Close()
	})
	e := &e2eEnv{t: t, app: a, srv: srv}

	client := e.client()
	expect(t, "seed login", client.login(e2eSeedUser, e2eSeedPass), http.StatusOK)
	me := client.get("/api/auth/me")
	expect(t, "me", me, http.StatusOK)
	if admin, _ := me.obj()["platformAdmin"].(bool); !admin {
		t.Fatalf("种子账号应被提升为平台管理员: %s", me.raw)
	}
	if me.str("username") != e2eSeedUser {
		t.Fatalf("me = %s", me.raw)
	}
}
