package store_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func openStore(t *testing.T) *store.Store {
	t.Helper()
	s, err := store.Open(context.Background(), filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestTenants(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()

	if _, err := s.GetTenant(ctx, "missing"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("missing tenant err = %v", err)
	}
	created := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
	if err := s.UpsertTenant(ctx, domain.Tenant{
		ID: "cube-a", UserID: "alice", Name: "A", CreatedAt: created,
		Description: "旧描述", MultiView: true,
	}); err != nil {
		t.Fatalf("Upsert: %v", err)
	}
	// 再写一次只更新字段，不改变 created_at。
	if err := s.UpsertTenant(ctx, domain.Tenant{
		ID: "cube-a", UserID: "alice2", Name: "B", Disabled: true,
		Description: "新描述",
	}); err != nil {
		t.Fatalf("Upsert 2: %v", err)
	}
	got, err := s.GetTenant(ctx, "cube-a")
	if err != nil {
		t.Fatalf("GetTenant: %v", err)
	}
	if got.UserID != "alice2" || !got.Disabled || !got.CreatedAt.Equal(created) {
		t.Fatalf("tenant = %+v", got)
	}
	if got.Description != "新描述" || got.MultiView {
		t.Fatalf("描述/多视图未持久化: %+v", got)
	}

	if err := s.UpsertTenant(ctx, domain.Tenant{ID: "cube-b", UserID: "bob"}); err != nil {
		t.Fatalf("Upsert b: %v", err)
	}
	list, err := s.ListTenants(ctx)
	if err != nil || len(list) != 2 {
		t.Fatalf("ListTenants = %v, %v", list, err)
	}
}

func TestUpsertTokenKeepsRevocation(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now()
	tok := domain.Token{
		ID: "static-abc123", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: domain.HashToken("tok"), Prefix: "memos", Label: "bootstrap", CreatedAt: now,
	}
	if err := s.UpsertToken(ctx, tok); err != nil {
		t.Fatalf("UpsertToken: %v", err)
	}
	if err := s.RevokeToken(ctx, tok.ID, now); err != nil {
		t.Fatalf("RevokeToken: %v", err)
	}
	if err := s.UpsertToken(ctx, tok); err != nil {
		t.Fatalf("UpsertToken 2: %v", err)
	}
	got, err := s.GetToken(ctx, tok.ID)
	if err != nil {
		t.Fatalf("GetToken: %v", err)
	}
	if got.RevokedAt == nil {
		t.Fatal("UpsertToken 不应清除 revoked_at")
	}
}

func TestEnsureTenantPreservesDisabled(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	if err := s.EnsureTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "alice", Name: "alice"}); err != nil {
		t.Fatalf("EnsureTenant: %v", err)
	}
	if err := s.UpsertTenant(ctx, domain.Tenant{
		ID: "cube-a", UserID: "alice", Name: "alice", Disabled: true,
		Description: "控制台改过", MultiView: true,
	}); err != nil {
		t.Fatalf("UpsertTenant: %v", err)
	}
	if err := s.EnsureTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "alice", Name: "alice"}); err != nil {
		t.Fatalf("EnsureTenant 2: %v", err)
	}
	got, err := s.GetTenant(ctx, "cube-a")
	if err != nil || !got.Disabled {
		t.Fatalf("EnsureTenant 不应重置 disabled: %+v, %v", got, err)
	}
	if got.Description != "控制台改过" || !got.MultiView {
		t.Fatalf("EnsureTenant 不应覆盖控制台维护的字段: %+v", got)
	}
}

func TestTokens(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	raw := "memos_test_token_value"
	hash := domain.HashToken(raw)
	tok := domain.Token{
		ID: "tok-1", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: hash, Prefix: domain.TokenPrefix(raw), Label: "test", CreatedAt: time.Now(),
	}
	if err := s.CreateToken(ctx, tok); err != nil {
		t.Fatalf("CreateToken: %v", err)
	}
	if err := s.CreateToken(ctx, tok); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("duplicate err = %v, want conflict", err)
	}

	got, err := s.TokenByHash(ctx, hash)
	if err != nil {
		t.Fatalf("TokenByHash: %v", err)
	}
	if got.Hash != hash || got.TenantID != "cube-a" || got.RevokedAt != nil {
		t.Fatalf("token = %+v", got)
	}
	if _, err := s.TokenByHash(ctx, domain.HashToken("other")); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("unknown hash err = %v", err)
	}

	now := time.Now()
	if err := s.TouchToken(ctx, "tok-1", now); err != nil {
		t.Fatalf("Touch: %v", err)
	}
	if err := s.RevokeToken(ctx, "tok-1", now); err != nil {
		t.Fatalf("Revoke: %v", err)
	}
	got, err = s.GetToken(ctx, "tok-1")
	if err != nil {
		t.Fatalf("GetToken: %v", err)
	}
	if got.LastUsedAt == nil || got.RevokedAt == nil {
		t.Fatalf("token timestamps = %+v", got)
	}
	if err := s.RevokeToken(ctx, "missing", now); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("revoke missing err = %v", err)
	}
	list, err := s.ListTokens(ctx, "cube-a")
	if err != nil || len(list) != 1 {
		t.Fatalf("ListTokens = %v, %v", list, err)
	}
}

func TestTokenExpiry(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)
	expires := now.Add(72 * time.Hour)
	raw := "memos_expiring_token"
	hash := domain.HashToken(raw)
	tok := domain.Token{
		ID: "tok-exp", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: hash, Prefix: domain.TokenPrefix(raw), Label: "expiring", CreatedAt: now,
		ExpiresAt: &expires,
	}
	if err := s.CreateToken(ctx, tok); err != nil {
		t.Fatalf("CreateToken: %v", err)
	}
	got, err := s.GetToken(ctx, tok.ID)
	if err != nil {
		t.Fatalf("GetToken: %v", err)
	}
	if got.ExpiresAt == nil || !got.ExpiresAt.Equal(expires) {
		t.Fatalf("expires_at 未持久化: %+v", got.ExpiresAt)
	}
	byHash, err := s.TokenByHash(ctx, hash)
	if err != nil || byHash.ExpiresAt == nil || !byHash.ExpiresAt.Equal(expires) {
		t.Fatalf("TokenByHash expires_at = %+v, %v", byHash.ExpiresAt, err)
	}
	list, err := s.ListTokens(ctx, "cube-a")
	if err != nil || len(list) != 1 || list[0].ExpiresAt == nil || !list[0].ExpiresAt.Equal(expires) {
		t.Fatalf("ListTokens = %+v, %v", list, err)
	}

	// 重复 Upsert（静态 Token 引导）不能把已有的过期时间清成永不过期。
	withoutExpiry := tok
	withoutExpiry.ExpiresAt = nil
	if err := s.UpsertToken(ctx, withoutExpiry); err != nil {
		t.Fatalf("UpsertToken: %v", err)
	}
	got, err = s.GetToken(ctx, tok.ID)
	if err != nil {
		t.Fatalf("GetToken after upsert: %v", err)
	}
	if got.ExpiresAt == nil || !got.ExpiresAt.Equal(expires) {
		t.Fatalf("UpsertToken 不应清除 expires_at: %+v", got.ExpiresAt)
	}

	// 没有过期时间的 Token（静态 Token）保持永不过期。
	static := domain.Token{
		ID: "tok-static", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: domain.HashToken("memos_static_token"), Prefix: "memos_st", Label: "bootstrap", CreatedAt: now,
	}
	if err := s.CreateToken(ctx, static); err != nil {
		t.Fatalf("CreateToken static: %v", err)
	}
	got, err = s.GetToken(ctx, static.ID)
	if err != nil || got.ExpiresAt != nil {
		t.Fatalf("静态 Token 的 expires_at 应为空: %+v, %v", got.ExpiresAt, err)
	}
}

func TestSessionExtendAndGet(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	acct := createTestAccount(t, s)
	sess := createTestSession(t, s, acct.ID)

	got, err := s.GetSession(ctx, sess.ID)
	if err != nil {
		t.Fatalf("GetSession: %v", err)
	}
	if got.ID != sess.ID || got.AccountID != acct.ID || got.LastSeenAt != nil {
		t.Fatalf("GetSession = %+v", got)
	}
	if _, err := s.GetSession(ctx, "missing"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("missing session err = %v", err)
	}

	// 时间列是固定宽度毫秒 UTC，写入后再读回会丢掉亚毫秒部分；
	// 这里先截断到毫秒，断言才不会受 time.Now 的纳秒精度影响。
	lastSeen := sess.CreatedAt.Add(30 * time.Minute).Truncate(time.Millisecond)
	expires := lastSeen.Add(12 * time.Hour)
	if err := s.ExtendSession(ctx, sess.ID, lastSeen, expires); err != nil {
		t.Fatalf("ExtendSession: %v", err)
	}
	got, err = s.GetSession(ctx, sess.ID)
	if err != nil {
		t.Fatalf("GetSession after extend: %v", err)
	}
	if got.LastSeenAt == nil || !got.LastSeenAt.Equal(lastSeen) {
		t.Fatalf("last_seen_at = %+v", got.LastSeenAt)
	}
	if !got.ExpiresAt.Equal(expires) {
		t.Fatalf("expires_at = %v, want %v", got.ExpiresAt, expires)
	}

	// 已吊销的会话不能被续期（即使并发吊销发生在读取之后）。
	if err := s.RevokeSession(ctx, sess.ID, lastSeen); err != nil {
		t.Fatalf("RevokeSession: %v", err)
	}
	extended := expires.Add(24 * time.Hour)
	if err := s.ExtendSession(ctx, sess.ID, lastSeen.Add(time.Minute), extended); err != nil {
		t.Fatalf("ExtendSession revoked: %v", err)
	}
	got, err = s.GetSession(ctx, sess.ID)
	if err != nil {
		t.Fatalf("GetSession revoked: %v", err)
	}
	if !got.ExpiresAt.Equal(expires) {
		t.Fatalf("吊销后的会话不应被续期: %v", got.ExpiresAt)
	}
}

func TestAudit(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Date(2026, 10, 21, 10, 0, 0, 0, time.UTC)
	caseLike := true
	for i, tool := range []string{"add_message", "search_memory", "search_memory"} {
		rec := domain.AuditRecord{
			CallID: "call-" + tool, TS: base.Add(time.Duration(i) * time.Minute),
			Tool: tool, TenantID: "cube-a", UserID: "alice", SessionID: "s1",
			CaseLike: &caseLike, Ms: int64(i), OK: true,
			Input: map[string]any{"query": "hello"}, Output: `{"status":"success"}`,
		}
		if err := s.InsertAudit(ctx, rec); err != nil {
			t.Fatalf("InsertAudit: %v", err)
		}
	}

	list, total, err := s.ListAudit(ctx, domain.AuditFilter{TenantID: "cube-a", Limit: 2})
	if err != nil {
		t.Fatalf("ListAudit: %v", err)
	}
	if total != 3 || len(list) != 2 {
		t.Fatalf("total = %d, len = %d", total, len(list))
	}
	if list[0].Tool != "search_memory" {
		t.Fatalf("order = %+v", list[0])
	}
	if list[0].Input != nil || list[0].Output != "" || list[0].CaseLike == nil || !*list[0].CaseLike {
		t.Fatalf("record fields = %+v", list[0])
	}
	detail, err := s.GetAudit(ctx, list[0].ID)
	if err != nil || detail.Input["query"] != "hello" || detail.Output != `{"status":"success"}` {
		t.Fatalf("detail = %+v, %v", detail, err)
	}

	filtered, total, err := s.ListAudit(ctx, domain.AuditFilter{Tool: "add_message", Since: base, Until: base.Add(time.Minute)})
	if err != nil || total != 1 || len(filtered) != 1 || filtered[0].Tool != "add_message" {
		t.Fatalf("filtered = %v, %d, %v", filtered, total, err)
	}

	deleted, err := s.PurgeAuditBefore(ctx, base.Add(time.Minute))
	if err != nil || deleted != 1 {
		t.Fatalf("Purge = %d, %v", deleted, err)
	}
}

func TestIdempotency(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Second)
	rec := domain.IdempotencyRecord{
		TenantID: "cube-a", Key: "req-1", Tool: domain.ToolAddMessage,
		RequestHash: "hash-1", State: domain.IdempotencyInProgress, CreatedAt: now, UpdatedAt: now,
	}

	got, claimed, err := s.ClaimIdempotency(ctx, rec)
	if err != nil || !claimed || got.Key != "req-1" {
		t.Fatalf("first claim = %+v, %v, %v", got, claimed, err)
	}
	existing, claimed, err := s.ClaimIdempotency(ctx, rec)
	if err != nil || claimed || existing.RequestHash != "hash-1" || existing.State != domain.IdempotencyInProgress {
		t.Fatalf("second claim = %+v, %v, %v", existing, claimed, err)
	}
	if err := s.CompleteIdempotency(ctx, domain.IdempotencyOutcome{TenantID: "cube-a", Key: "req-1", Response: []byte(`{"status":"success"}`), At: now.Add(time.Second)}); err != nil {
		t.Fatalf("Complete: %v", err)
	}
	existing, claimed, err = s.ClaimIdempotency(ctx, rec)
	if err != nil || claimed || existing.State != domain.IdempotencySucceeded ||
		string(existing.Response) != `{"status":"success"}` {
		t.Fatalf("replay = %+v, %v, %v", existing, claimed, err)
	}

	// 同键在不同租户互不影响。
	other := rec
	other.TenantID = "cube-b"
	if _, claimed, err := s.ClaimIdempotency(ctx, other); err != nil || !claimed {
		t.Fatalf("tenant isolation claimed=%v err=%v", claimed, err)
	}

	// 异步入队：accepted 可重放，worker 成功后收敛到 succeeded 且保留响应。
	accepted := domain.IdempotencyRecord{
		TenantID: "cube-a", Key: "req-3", Tool: domain.ToolAddMessage,
		RequestHash: "hash-3", State: domain.IdempotencyInProgress, CreatedAt: now, UpdatedAt: now,
	}
	if _, claimed, err := s.ClaimIdempotency(ctx, accepted); err != nil || !claimed {
		t.Fatalf("claim accepted = %v, %v", claimed, err)
	}
	if err := s.AcceptIdempotency(ctx, domain.IdempotencyOutcome{TenantID: "cube-a", Key: "req-3", Response: []byte(`{"status":"accepted"}`), At: now.Add(time.Second)}); err != nil {
		t.Fatalf("Accept: %v", err)
	}
	existing, claimed, err = s.ClaimIdempotency(ctx, accepted)
	if err != nil || claimed || existing.State != domain.IdempotencyAccepted ||
		string(existing.Response) != `{"status":"accepted"}` {
		t.Fatalf("accepted replay = %+v, %v, %v", existing, claimed, err)
	}
	if err := s.SucceedIdempotency(ctx, "cube-a", "req-3", now.Add(2*time.Second)); err != nil {
		t.Fatalf("Succeed: %v", err)
	}
	existing, _, err = s.ClaimIdempotency(ctx, accepted)
	if err != nil || existing.State != domain.IdempotencySucceeded ||
		string(existing.Response) != `{"status":"accepted"}` {
		t.Fatalf("succeed transition = %+v, %v", existing, err)
	}

	// 失败释放后可以重试。
	release := domain.IdempotencyRecord{
		TenantID: "cube-a", Key: "req-2", Tool: domain.ToolAddMessage,
		RequestHash: "hash-2", State: domain.IdempotencyInProgress, CreatedAt: now, UpdatedAt: now,
	}
	if _, claimed, err := s.ClaimIdempotency(ctx, release); err != nil || !claimed {
		t.Fatalf("claim release = %v, %v", claimed, err)
	}
	if err := s.ReleaseIdempotency(ctx, "cube-a", "req-2"); err != nil {
		t.Fatalf("Release: %v", err)
	}
	if _, claimed, err := s.ClaimIdempotency(ctx, release); err != nil || !claimed {
		t.Fatalf("reclaim after release = %v, %v", claimed, err)
	}

	// accepted 也能被释放，供 worker 永久失败后重试。
	releaseAccepted := domain.IdempotencyRecord{
		TenantID: "cube-a", Key: "req-4", Tool: domain.ToolAddMessage,
		RequestHash: "hash-4", State: domain.IdempotencyInProgress, CreatedAt: now, UpdatedAt: now,
	}
	if _, claimed, err := s.ClaimIdempotency(ctx, releaseAccepted); err != nil || !claimed {
		t.Fatalf("claim release accepted = %v, %v", claimed, err)
	}
	if err := s.AcceptIdempotency(ctx, domain.IdempotencyOutcome{TenantID: "cube-a", Key: "req-4", Response: []byte(`{"status":"accepted"}`), At: now.Add(time.Second)}); err != nil {
		t.Fatalf("Accept release: %v", err)
	}
	if err := s.ReleaseIdempotency(ctx, "cube-a", "req-4"); err != nil {
		t.Fatalf("Release accepted: %v", err)
	}
	if _, claimed, err := s.ClaimIdempotency(ctx, releaseAccepted); err != nil || !claimed {
		t.Fatalf("reclaim after accepted release = %v, %v", claimed, err)
	}

	// 只清理超期记录。
	n, err := s.PurgeIdempotencyBefore(ctx, now.Add(time.Minute))
	if err != nil {
		t.Fatalf("Purge: %v", err)
	}
	if n != 5 {
		t.Fatalf("purged=%d want 5", n)
	}
}

func TestPurgeFinishedJobs(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Now().UTC().Truncate(time.Second)
	old := base.Add(-time.Hour)
	fresh := base.Add(time.Hour)
	enqueue := func(id, tenant string, status domain.JobStatus, at time.Time) {
		t.Helper()
		err := s.EnqueueJob(ctx, domain.Job{
			ID: id, Kind: domain.JobKindMemoryAdd, Status: status, TenantID: tenant,
			Payload: []byte(`{}`), CreatedAt: at, UpdatedAt: at,
		})
		if err != nil {
			t.Fatalf("enqueue %s: %v", id, err)
		}
	}
	enqueue("old-ok", "cube-a", domain.JobStatusSucceeded, old)
	enqueue("old-fail", "cube-a", domain.JobStatusFailed, old)
	enqueue("old-pending", "cube-b", domain.JobStatusPending, old)
	enqueue("old-running", "cube-a", domain.JobStatusRunning, old)
	enqueue("new-ok", "cube-a", domain.JobStatusSucceeded, fresh)

	n, err := s.PurgeFinishedJobsBefore(ctx, base)
	if err != nil {
		t.Fatalf("PurgeFinishedJobsBefore: %v", err)
	}
	if n != 2 {
		t.Fatalf("purged=%d want 2", n)
	}
	// pending/running 与新任务必须保留。
	if _, ok, err := s.ClaimJob(ctx, base); err != nil || !ok {
		t.Fatalf("pending job should survive: ok=%v err=%v", ok, err)
	}
}

func TestJobs(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now()
	for i, id := range []string{"job-1", "job-2"} {
		job := domain.Job{
			ID: id, Kind: domain.JobKindMemoryAdd, TenantID: "cube-a", UserID: "alice",
			Payload: []byte(`{"n":1}`), CreatedAt: now.Add(time.Duration(i) * time.Second),
			NextAttemptAt: now,
		}
		if err := s.EnqueueJob(ctx, job); err != nil {
			t.Fatalf("EnqueueJob: %v", err)
		}
	}

	job, ok, err := s.ClaimJob(ctx, now)
	if err != nil || !ok || job.ID != "job-1" || job.Status != domain.JobStatusRunning || job.Attempts != 1 {
		t.Fatalf("ClaimJob = %+v, %v, %v", job, ok, err)
	}
	if err := s.FinishJob(ctx, domain.JobCompletion{ID: job.ID, Status: domain.JobStatusSucceeded, LastErr: "", Now: now}); err != nil {
		t.Fatalf("FinishJob: %v", err)
	}
	job, ok, err = s.ClaimJob(ctx, now)
	if err != nil || !ok || job.ID != "job-2" {
		t.Fatalf("ClaimJob 2 = %+v, %v, %v", job, ok, err)
	}
	if err := s.FinishJob(ctx, domain.JobCompletion{ID: job.ID, Status: domain.JobStatusFailed, LastErr: "boom", Now: now}); err != nil {
		t.Fatalf("FinishJob failed: %v", err)
	}
	if _, ok, err = s.ClaimJob(ctx, now); err != nil || ok {
		t.Fatalf("no job expected, ok=%v err=%v", ok, err)
	}
}

// 同一租户同时只允许一个 running，不同租户可以并行认领。
func TestClaimJobSerializesPerTenant(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Second)
	for i, tc := range []struct{ id, tenant string }{
		{"a-1", "cube-a"},
		{"a-2", "cube-a"},
		{"b-1", "cube-b"},
	} {
		if err := s.EnqueueJob(ctx, domain.Job{
			ID: tc.id, Kind: domain.JobKindMemoryAdd, TenantID: tc.tenant,
			Payload: []byte(`{}`), CreatedAt: now.Add(time.Duration(i) * time.Second),
			NextAttemptAt: now,
		}); err != nil {
			t.Fatalf("EnqueueJob %s: %v", tc.id, err)
		}
	}

	job, ok, err := s.ClaimJob(ctx, now)
	if err != nil || !ok || job.ID != "a-1" {
		t.Fatalf("first claim = %+v, %v, %v", job, ok, err)
	}
	// cube-a 已有 running：跳过 a-2，认领 cube-b。
	job, ok, err = s.ClaimJob(ctx, now)
	if err != nil || !ok || job.ID != "b-1" {
		t.Fatalf("second claim = %+v, %v, %v", job, ok, err)
	}
	// 两个租户都在跑，a-2 不能越过同租户的 a-1。
	if _, ok, err = s.ClaimJob(ctx, now); err != nil || ok {
		t.Fatalf("same-tenant claim should wait: ok=%v err=%v", ok, err)
	}
	if err := s.FinishJob(ctx, domain.JobCompletion{ID: "a-1", Status: domain.JobStatusSucceeded, LastErr: "", Now: now}); err != nil {
		t.Fatalf("FinishJob: %v", err)
	}
	job, ok, err = s.ClaimJob(ctx, now)
	if err != nil || !ok || job.ID != "a-2" {
		t.Fatalf("claim after finish = %+v, %v, %v", job, ok, err)
	}
}

// 进程崩溃遗留的 running 超过 staleBefore 后会被重置，避免该租户被永久阻塞。
func TestClaimJobResetsStaleRunning(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Second)
	old := now.Add(-2 * time.Hour)
	if err := s.EnqueueJob(ctx, domain.Job{
		ID: "stale", Kind: domain.JobKindMemoryAdd, Status: domain.JobStatusRunning,
		TenantID: "cube-a", Payload: []byte(`{}`), CreatedAt: old, UpdatedAt: old, Attempts: 1,
	}); err != nil {
		t.Fatalf("EnqueueJob: %v", err)
	}

	n, err := s.ResetStaleJobs(ctx, now, now.Add(-time.Hour))
	if err != nil || n != 1 {
		t.Fatalf("ResetStaleJobs = %d, %v want 1", n, err)
	}
	job, ok, err := s.ClaimJob(ctx, now)
	if err != nil || !ok || job.ID != "stale" || job.Attempts != 2 {
		t.Fatalf("stale claim = %+v, %v, %v", job, ok, err)
	}
}

// next_attempt_at 未到期的任务不可认领；到期后按 created_at 回到队首。
func TestClaimJobSkipsNotDue(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Second)
	if err := s.EnqueueJob(ctx, domain.Job{
		ID: "later", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a",
		Payload: []byte(`{}`), CreatedAt: now, NextAttemptAt: now.Add(time.Minute),
	}); err != nil {
		t.Fatalf("EnqueueJob: %v", err)
	}
	if _, ok, err := s.ClaimJob(ctx, now); err != nil || ok {
		t.Fatalf("未到期的任务不应被认领: ok=%v err=%v", ok, err)
	}
	job, ok, err := s.ClaimJob(ctx, now.Add(2*time.Minute))
	if err != nil || !ok || job.ID != "later" {
		t.Fatalf("到期后应可认领: %+v, %v, %v", job, ok, err)
	}
}

// 重试复用同一行：ID、created_at、attempts 延续，退避期间不可认领。
func TestRescheduleJob(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Second)
	if err := s.EnqueueJob(ctx, domain.Job{
		ID: "job-1", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a",
		Payload: []byte(`{}`), CreatedAt: now, NextAttemptAt: now,
	}); err != nil {
		t.Fatalf("EnqueueJob: %v", err)
	}
	job, ok, err := s.ClaimJob(ctx, now)
	if err != nil || !ok || job.Attempts != 1 {
		t.Fatalf("ClaimJob = %+v, %v, %v", job, ok, err)
	}
	if err := s.RescheduleJob(ctx, domain.JobReschedule{ID: job.ID, Next: now.Add(time.Minute), LastErr: "upstream unavailable", Now: now}); err != nil {
		t.Fatalf("RescheduleJob: %v", err)
	}
	if _, ok, err := s.ClaimJob(ctx, now); err != nil || ok {
		t.Fatalf("退避期间不应被认领: ok=%v err=%v", ok, err)
	}
	job, ok, err = s.ClaimJob(ctx, now.Add(2*time.Minute))
	if err != nil || !ok || job.ID != "job-1" || job.Attempts != 2 {
		t.Fatalf("退避到期后应可认领且 attempts 累加: %+v, %v, %v", job, ok, err)
	}
	if job.LastError != "upstream unavailable" {
		t.Fatalf("last_error = %q", job.LastError)
	}
	// 终态任务不能被重排。
	if err := s.FinishJob(ctx, domain.JobCompletion{ID: job.ID, Status: domain.JobStatusFailed, LastErr: "boom", Now: now}); err != nil {
		t.Fatalf("FinishJob: %v", err)
	}
	if err := s.RescheduleJob(ctx, domain.JobReschedule{ID: job.ID, Next: now, LastErr: "", Now: now}); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("已终态的任务重排应返回 not found: %v", err)
	}
}

// 人工重放死信：failed → pending、attempts 归零，重放后 worker 立刻能认领。
// 与 RescheduleJob 的差别就在这里 —— 那个是同一轮重试（attempts 继续累加）。
func TestRequeueJob(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Second)
	if err := s.EnqueueJob(ctx, domain.Job{
		ID: "job-dead", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a",
		Payload: []byte(`{}`), CreatedAt: now, NextAttemptAt: now,
	}); err != nil {
		t.Fatalf("EnqueueJob: %v", err)
	}
	job, ok, err := s.ClaimJob(ctx, now)
	if err != nil || !ok {
		t.Fatalf("ClaimJob = %v, %v", ok, err)
	}
	if err := s.FinishJob(ctx, domain.JobCompletion{ID: job.ID, Status: domain.JobStatusFailed, LastErr: "upstream unavailable", Now: now}); err != nil {
		t.Fatalf("FinishJob: %v", err)
	}

	retried, err := s.RequeueJob(ctx, "job-dead", now)
	if err != nil {
		t.Fatalf("RequeueJob: %v", err)
	}
	if retried.Status != domain.JobStatusPending || retried.Attempts != 0 || retried.LastError != "" {
		t.Fatalf("重放后应 pending/attempts=0/last_error 空: %+v", retried)
	}
	// 重放不是新建任务：id 与 created_at 不变，且立刻可认领。
	claimed, ok, err := s.ClaimJob(ctx, now)
	if err != nil || !ok || claimed.ID != "job-dead" {
		t.Fatalf("重放后应能认领: %+v, %v, %v", claimed, ok, err)
	}
	if !claimed.CreatedAt.Equal(now) {
		t.Fatalf("created_at 不应变: %v want %v", claimed.CreatedAt, now)
	}

	// 只有 failed 能重放：running/succeeded 再拉一次会重复写。
	if _, err := s.RequeueJob(ctx, "job-dead", now); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("running 任务重放应冲突: %v", err)
	}
	if err := s.FinishJob(ctx, domain.JobCompletion{ID: "job-dead", Status: domain.JobStatusSucceeded, LastErr: "", Now: now}); err != nil {
		t.Fatalf("FinishJob succeeded: %v", err)
	}
	if _, err := s.RequeueJob(ctx, "job-dead", now); !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("succeeded 任务重放应冲突: %v", err)
	}
	if _, err := s.RequeueJob(ctx, "missing", now); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("不存在的任务应 not found: %v", err)
	}

	// GetJob 给重放前的安全性判断用：读得到 last_error，读不到 payload。
	got, err := s.GetJob(ctx, "job-dead")
	if err != nil || got.LastError != "" || got.Status != domain.JobStatusSucceeded {
		t.Fatalf("GetJob = %+v, %v", got, err)
	}
	if _, err := s.GetJob(ctx, "missing"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("GetJob 不存在应 not found: %v", err)
	}
}

func TestJobStats(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Now().UTC().Truncate(time.Second)
	enqueue := func(id string, status domain.JobStatus, at time.Time) {
		t.Helper()
		if err := s.EnqueueJob(ctx, domain.Job{
			ID: id, Kind: domain.JobKindMemoryAdd, Status: status, TenantID: "cube-a",
			Payload: []byte(`{}`), CreatedAt: at, UpdatedAt: at,
		}); err != nil {
			t.Fatalf("enqueue %s: %v", id, err)
		}
	}
	enqueue("old-pending", domain.JobStatusPending, base.Add(-5*time.Minute))
	enqueue("new-pending", domain.JobStatusPending, base.Add(-time.Minute))
	enqueue("running", domain.JobStatusRunning, base)
	enqueue("failed", domain.JobStatusFailed, base.Add(-time.Minute))

	pending, running, failed, oldest, err := s.JobStats(ctx)
	if err != nil {
		t.Fatalf("JobStats: %v", err)
	}
	if pending != 2 || running != 1 || failed != 1 {
		t.Fatalf("pending/running/failed = %d/%d/%d", pending, running, failed)
	}
	if !oldest.Equal(base.Add(-5 * time.Minute)) {
		t.Fatalf("oldest = %v", oldest)
	}
}

// ListJobs 只返回元数据、按 created_at 倒序，并支持过滤与分页。
func TestListJobs(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	base := time.Now().UTC().Truncate(time.Second)
	enqueue := func(id string, status domain.JobStatus, tenant string, at time.Time) {
		t.Helper()
		if err := s.EnqueueJob(ctx, domain.Job{
			ID: id, Kind: domain.JobKindMemoryAdd, Status: status, TenantID: tenant,
			Payload: []byte(`{"secret":"正文"}`), CreatedAt: at, UpdatedAt: at,
		}); err != nil {
			t.Fatalf("enqueue %s: %v", id, err)
		}
	}
	enqueue("a-old", domain.JobStatusFailed, "cube-a", base.Add(-2*time.Minute))
	enqueue("a-new", domain.JobStatusFailed, "cube-a", base.Add(-time.Minute))
	enqueue("b-fail", domain.JobStatusFailed, "cube-b", base)
	enqueue("b-ok", domain.JobStatusSucceeded, "cube-b", base)

	jobs, total, err := s.ListJobs(ctx, domain.JobFilter{Status: domain.JobStatusFailed, Limit: 10})
	if err != nil || total != 3 || len(jobs) != 3 {
		t.Fatalf("ListJobs = %d/%d, %v", len(jobs), total, err)
	}
	if jobs[0].ID != "b-fail" || jobs[2].ID != "a-old" {
		t.Fatalf("应按 created_at 倒序: %v", []string{jobs[0].ID, jobs[1].ID, jobs[2].ID})
	}
	for _, job := range jobs {
		if job.Payload != nil {
			t.Fatalf("ListJobs 不应返回 payload: %s", job.ID)
		}
	}

	jobs, total, err = s.ListJobs(ctx, domain.JobFilter{Status: domain.JobStatusFailed, TenantID: "cube-a"})
	if err != nil || total != 2 || len(jobs) != 2 {
		t.Fatalf("按租户过滤 = %d/%d, %v", len(jobs), total, err)
	}
	jobs, total, err = s.ListJobs(ctx, domain.JobFilter{Status: domain.JobStatusFailed, Limit: 1, Offset: 1})
	if err != nil || total != 3 || len(jobs) != 1 || jobs[0].ID != "a-new" {
		t.Fatalf("分页 = %+v/%d, %v", jobs, total, err)
	}
}

// 迁移导入的静态 Token 带展示名；启动引导的 upsert 不能把它覆盖回 bootstrap。
func TestUpsertTokenKeepsImportedLabel(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 24, 0, 0, 0, 0, time.UTC)
	hash := domain.HashToken("legacy-token")
	tok := domain.Token{
		ID: "static-abc123def456", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "cube-a",
		Hash: hash, Prefix: domain.TokenPrefix("legacy-token"), Label: "Alpha Key", CreatedAt: now,
	}
	if err := s.UpsertToken(ctx, tok); err != nil {
		t.Fatalf("UpsertToken: %v", err)
	}
	tok.Label = "bootstrap"
	if err := s.UpsertToken(ctx, tok); err != nil {
		t.Fatalf("UpsertToken bootstrap: %v", err)
	}
	got, err := s.GetToken(ctx, tok.ID)
	if err != nil {
		t.Fatalf("GetToken: %v", err)
	}
	if got.Label != "Alpha Key" {
		t.Fatalf("label = %q, want imported name", got.Label)
	}
	if !got.CreatedAt.Equal(now) {
		t.Fatalf("created_at = %v, want %v", got.CreatedAt, now)
	}
}

// 启动引导只在占位名（空串或旧 user_id）时同步租户名，不能覆盖迁移导入的展示名。
func TestEnsureTenantKeepsCustomName(t *testing.T) {
	t.Parallel()

	s := openStore(t)
	ctx := context.Background()
	if err := s.CreateTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "cube-a", Name: "Alpha"}); err != nil {
		t.Fatalf("CreateTenant: %v", err)
	}
	if err := s.EnsureTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "cube-a", Name: "cube-a"}); err != nil {
		t.Fatalf("EnsureTenant: %v", err)
	}
	got, err := s.GetTenant(ctx, "cube-a")
	if err != nil || got.Name != "Alpha" {
		t.Fatalf("custom name overwritten: %+v %v", got, err)
	}

	// 占位名仍跟随 user_id 变化（静态配置改绑上游用户时）。
	if err := s.CreateTenant(ctx, domain.Tenant{ID: "cube-b", UserID: "old", Name: "old"}); err != nil {
		t.Fatalf("CreateTenant b: %v", err)
	}
	if err := s.EnsureTenant(ctx, domain.Tenant{ID: "cube-b", UserID: "new", Name: "new"}); err != nil {
		t.Fatalf("EnsureTenant b: %v", err)
	}
	got, err = s.GetTenant(ctx, "cube-b")
	if err != nil || got.UserID != "new" || got.Name != "new" {
		t.Fatalf("placeholder name not synced: %+v %v", got, err)
	}
}
