package store

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func openInternalStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(context.Background(), filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

// 时间必须固定宽度（毫秒）且落在 UTC：整秒与带小数的输出长度一致，
// 否则 SQLite 的字典序比较会在整秒边界漏行或错序。
func TestEncodeTimeFixedWidthMillisUTC(t *testing.T) {
	t.Parallel()

	beijing := time.FixedZone("UTC+8", 8*60*60)
	cases := []struct {
		name string
		in   time.Time
		want string
	}{
		{"整秒补足毫秒", time.Date(2026, 10, 23, 0, 0, 0, 0, time.UTC), "2026-10-23T00:00:00.000Z"},
		{"毫秒", time.Date(2026, 10, 23, 0, 0, 0, 123_000_000, time.UTC), "2026-10-23T00:00:00.123Z"},
		{"亚毫秒截断", time.Date(2026, 10, 23, 0, 0, 0, 123_456_789, time.UTC), "2026-10-23T00:00:00.123Z"},
		{"非 UTC 换算成 UTC", time.Date(2026, 10, 23, 8, 0, 0, 0, beijing), "2026-10-23T00:00:00.000Z"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got := encodeTime(tc.in)
			if got != tc.want {
				t.Fatalf("encodeTime(%v) = %q, want %q", tc.in, got, tc.want)
			}
			// 时间本体 23 字符（到毫秒），UTC 下再加一个 'Z'。
			if len(got) != 24 || got[len(got)-1] != 'Z' {
				t.Fatalf("encodeTime 输出应固定为 24 字符且以 Z 结尾: %q", got)
			}
		})
	}
}

// decodeTime 只接受 encodeTime 写出的固定宽度格式；变长写法一律报错，
// 免得“字符串序 = 时间序”的前提被悄悄放掉。
func TestDecodeTimeRequiresFixedWidth(t *testing.T) {
	t.Parallel()

	base := time.Date(2026, 10, 22, 16, 0, 0, 0, time.UTC)
	cases := []struct {
		name string
		raw  string
		want time.Time
	}{
		{"固定宽度毫秒", "2026-10-22T16:00:00.123Z", base.Add(123 * time.Millisecond)},
		{"固定宽度整秒", "2026-10-22T16:00:00.000Z", base},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got, err := decodeTime(tc.raw)
			if err != nil {
				t.Fatalf("decodeTime(%q): %v", tc.raw, err)
			}
			if !got.Equal(tc.want) {
				t.Fatalf("decodeTime(%q) = %v, want %v", tc.raw, got, tc.want)
			}
		})
	}
	for _, raw := range []string{
		"2026-10-22T16:00:00Z",           // 缺小数位
		"2026-10-22T16:00:00.123456789Z", // 纳秒
		"2026-10-22 16:00:00",            // 空格分隔
	} {
		if _, err := decodeTime(raw); err == nil {
			t.Fatalf("decodeTime(%q) 应拒绝非固定宽度格式", raw)
		}
	}
	if got, err := decodeTime(""); err != nil || !got.IsZero() {
		t.Fatalf("decodeTime(\"\") = %v, %v", got, err)
	}
}

// 回归：since 落在整秒边界时，旧的整秒文本 '...00Z' 会把同一秒内带小数的行
// 排除在外（'.' < 'Z'）；固定宽度后用 '...00.000Z' 比较，边界内的行不会漏。
func TestAuditSinceWholeSecondIncludesFractionalRows(t *testing.T) {
	t.Parallel()

	s := openInternalStore(t)
	ctx := context.Background()
	boundary := time.Date(2026, 10, 22, 16, 0, 0, 0, time.UTC)

	// 固定宽度的存量行，以及对照用的变长边界文本。
	if _, err := s.db.ExecContext(ctx,
		`INSERT INTO audit_records (id, ts, tool, ok) VALUES (?, ?, ?, ?)`,
		"legacy-fraction", "2026-10-22T16:00:00.500Z", "add_message", 1); err != nil {
		t.Fatalf("insert audit row: %v", err)
	}

	var legacyCount int
	if err := s.read.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM audit_records WHERE ts >= ?`, "2026-10-22T16:00:00Z").Scan(&legacyCount); err != nil {
		t.Fatalf("count legacy boundary: %v", err)
	}
	if legacyCount != 0 {
		t.Fatalf("变长边界文本不应包含同一秒内带小数的行，count = %d", legacyCount)
	}

	list, total, err := s.ListAudit(ctx, domain.AuditFilter{
		Since: boundary, Until: boundary.Add(time.Minute), Limit: 10,
	})
	if err != nil {
		t.Fatalf("ListAudit: %v", err)
	}
	if total != 1 || len(list) != 1 || list[0].ID != "legacy-fraction" {
		t.Fatalf("固定宽度边界应包含该秒内带小数的行: total=%d list=%+v", total, list)
	}

	if got := encodeTime(boundary); got != "2026-10-22T16:00:00.000Z" {
		t.Fatalf("since 编码 = %q, want 固定宽度", got)
	}
}

// tenants.user_id 映射上游 user_name：非空重复必须冲突，空串允许多行。
func TestTenantUserIDUniqueIndex(t *testing.T) {
	t.Parallel()

	s := openInternalStore(t)
	ctx := context.Background()
	now := time.Now().UTC()

	if err := s.UpsertTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "alice", CreatedAt: now}); err != nil {
		t.Fatalf("UpsertTenant first: %v", err)
	}
	err := s.UpsertTenant(ctx, domain.Tenant{ID: "cube-b", UserID: "alice", CreatedAt: now})
	if !errors.Is(err, domain.ErrConflict) {
		t.Fatalf("重复 user_id 应返回 ErrConflict, got %v", err)
	}
	if got := domain.SafeMessage(err); got != upstreamUserIDTaken {
		t.Fatalf("冲突消息 = %q, want %q", got, upstreamUserIDTaken)
	}

	// CreateTenant/EnsureTenant 也要区分“同名项目”与“占用 user_id”。
	if err := s.CreateTenant(ctx, domain.Tenant{ID: "cube-c", UserID: "alice", CreatedAt: now}); !errors.Is(err, domain.ErrConflict) ||
		domain.SafeMessage(err) != upstreamUserIDTaken {
		t.Fatalf("CreateTenant 重复 user_id = %v, want %q", err, upstreamUserIDTaken)
	}
	if err := s.CreateTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "bob", CreatedAt: now}); !errors.Is(err, domain.ErrConflict) ||
		domain.SafeMessage(err) != "已存在同名项目" {
		t.Fatalf("CreateTenant 重复 id = %v, want 已存在同名项目", err)
	}
	if err := s.EnsureTenant(ctx, domain.Tenant{ID: "cube-d", UserID: "alice", CreatedAt: now}); !errors.Is(err, domain.ErrConflict) ||
		domain.SafeMessage(err) != upstreamUserIDTaken {
		t.Fatalf("EnsureTenant 重复 user_id = %v, want %q", err, upstreamUserIDTaken)
	}

	// 空 user_id 不参与部分唯一索引：直接写库验证允许多行。
	for _, id := range []string{"empty-a", "empty-b"} {
		if _, err := s.db.ExecContext(ctx,
			`INSERT INTO tenants (id, user_id, name, created_at) VALUES (?, '', '', ?)`,
			id, encodeTime(now)); err != nil {
			t.Fatalf("空 user_id 应允许多行（%s）: %v", id, err)
		}
	}
}

// CreateTenantWithOwner 的唯一冲突要和 CreateTenant 分开报：一个账号只能有一个
// 个人项目，若笼统报“同名项目”，前端会提示改名重试，但改任何名字都会失败。
func TestCreateTenantWithOwnerConflictMessages(t *testing.T) {
	t.Parallel()

	s := openInternalStore(t)
	ctx := context.Background()
	now := time.Now().UTC()

	acct := domain.Account{ID: "acct-1", Username: "alice", CreatedAt: now}
	if err := s.CreateAccount(ctx, acct); err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	personal := domain.Tenant{
		ID: "u-acct-1", UserID: "u-acct-1", Name: "u-acct-1", CreatedAt: now,
		Kind: domain.TenantKindPersonal, CreatedByAccountID: acct.ID,
	}
	if err := s.CreateTenantWithOwner(ctx, personal, acct.ID, domain.RoleOwner); err != nil {
		t.Fatalf("CreateTenantWithOwner first: %v", err)
	}

	// 同一账号的第二个个人项目 → 明确指出已有个人项目。
	second := personal
	second.ID, second.UserID, second.Name = "u-acct-1-b", "u-acct-1-b", "u-acct-1-b"
	if err := s.CreateTenantWithOwner(ctx, second, acct.ID, domain.RoleOwner); !errors.Is(err, domain.ErrConflict) ||
		domain.SafeMessage(err) != personalOwnerTaken {
		t.Fatalf("第二个个人项目 = %v, want %q", err, personalOwnerTaken)
	}

	// user_id 已被占用 → 上游用户冲突。
	occupied := domain.Tenant{ID: "cube-x", UserID: personal.UserID, Name: "cube-x", CreatedAt: now}
	if err := s.CreateTenantWithOwner(ctx, occupied, acct.ID, domain.RoleOwner); !errors.Is(err, domain.ErrConflict) ||
		domain.SafeMessage(err) != upstreamUserIDTaken {
		t.Fatalf("占用 user_id = %v, want %q", err, upstreamUserIDTaken)
	}

	// 主键冲突（项目 ID 就是项目名）→ 同名项目。
	duplicate := domain.Tenant{ID: personal.ID, UserID: "another-user", Name: personal.Name, CreatedAt: now}
	if err := s.CreateTenantWithOwner(ctx, duplicate, acct.ID, domain.RoleOwner); !errors.Is(err, domain.ErrConflict) ||
		domain.SafeMessage(err) != "已存在同名项目" {
		t.Fatalf("重复项目 ID = %v, want 已存在同名项目", err)
	}
}

// tokens.user_id 是历史冗余快照：改绑租户的上游用户后，旧 Token 行必须同步，
// 否则管理端列表会一直显示旧值。
func TestUpsertTenantSyncsTokenUserID(t *testing.T) {
	t.Parallel()

	ctx := context.Background()
	s := openInternalStore(t)
	if err := s.UpsertTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "alice"}); err != nil {
		t.Fatalf("UpsertTenant: %v", err)
	}
	tok := domain.Token{
		ID: "tok-1", Kind: domain.TokenKindTenant, TenantID: "cube-a", UserID: "alice",
		Hash: domain.HashToken("memos_test_token"), Prefix: "memos", Label: "test",
	}
	if err := s.CreateToken(ctx, tok); err != nil {
		t.Fatalf("CreateToken: %v", err)
	}
	if err := s.UpsertTenant(ctx, domain.Tenant{ID: "cube-a", UserID: "bob"}); err != nil {
		t.Fatalf("UpsertTenant rebind: %v", err)
	}
	tokens, err := s.ListTokens(ctx, "cube-a")
	if err != nil {
		t.Fatalf("ListTokens: %v", err)
	}
	if len(tokens) != 1 || tokens[0].UserID != "bob" {
		t.Fatalf("token user_id = %+v, want bob", tokens)
	}
}
