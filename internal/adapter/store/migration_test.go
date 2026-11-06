package store_test

import (
	"context"
	"database/sql"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/migrations"
)

// 存量库（schema 到 0010）升级到 0011 时，老任务行必须被回填成“立即可认领”。
func TestMigrationBackfillsNextAttemptForExistingJobs(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "legacy.db")
	db, err := sql.Open("sqlite", "file:"+path)
	if err != nil {
		t.Fatalf("open raw sqlite: %v", err)
	}
	applyLegacySchema(t, db)

	created := time.Now().UTC().Add(-time.Hour).Truncate(time.Second)
	if _, err := db.ExecContext(context.Background(), `
		INSERT INTO jobs (id, kind, status, tenant_id, user_id, payload, attempts, last_error, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		"legacy-job", string(domain.JobKindMemoryAdd), string(domain.JobStatusPending),
		"cube-a", "alice", []byte(`{}`), 0, "",
		created.Format(time.RFC3339Nano), created.Format(time.RFC3339Nano),
	); err != nil {
		t.Fatalf("insert legacy job: %v", err)
	}
	if err := db.Close(); err != nil {
		t.Fatalf("close raw db: %v", err)
	}

	s, err := store.Open(context.Background(), path)
	if err != nil {
		t.Fatalf("store.Open with legacy db: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })

	job, ok, err := s.ClaimJob(context.Background(), time.Now())
	if err != nil || !ok || job.ID != "legacy-job" {
		t.Fatalf("老任务应可认领: %+v, %v, %v", job, ok, err)
	}
	if !job.NextAttemptAt.Equal(created) {
		t.Fatalf("next_attempt_at = %v, want 回填为 created_at %v", job.NextAttemptAt, created)
	}
}

// 0018 幂等补建 idx_audit_call_tool：旧编号库（新 0014 被跳过）也能在升级后
// 拿到这个索引，避免 call_id 终态子查询退化成扫表。
func TestMigrationRepairsAuditCallIndex(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "index.db")
	s, err := store.Open(context.Background(), path)
	if err != nil {
		t.Fatalf("Open fresh: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	// 模拟“版本 18 未记录且索引缺失”的库：重新 Open 时 0018 必须把它建回来。
	db, err := sql.Open("sqlite", "file:"+path)
	if err != nil {
		t.Fatalf("open raw sqlite: %v", err)
	}
	if _, err := db.ExecContext(context.Background(), `DROP INDEX idx_audit_call_tool`); err != nil {
		t.Fatalf("drop index: %v", err)
	}
	if _, err := db.ExecContext(context.Background(), `DELETE FROM schema_migrations WHERE version = 18`); err != nil {
		t.Fatalf("delete version 18: %v", err)
	}
	if err := db.Close(); err != nil {
		t.Fatalf("close raw db: %v", err)
	}

	s, err = store.Open(context.Background(), path)
	if err != nil {
		t.Fatalf("Open repair: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatalf("Close repair: %v", err)
	}

	db, err = sql.Open("sqlite", "file:"+path+"?_pragma=query_only(1)")
	if err != nil {
		t.Fatalf("open raw sqlite: %v", err)
	}
	defer func() { _ = db.Close() }()
	var n int
	if err := db.QueryRowContext(context.Background(), `SELECT COUNT(1) FROM pragma_index_list('audit_records') WHERE name = 'idx_audit_call_tool'`).Scan(&n); err != nil {
		t.Fatalf("query index: %v", err)
	}
	if n != 1 {
		t.Fatalf("idx_audit_call_tool 应被 0018 补建, count = %d", n)
	}
}

// 0015 把存量时间文本统一成固定宽度毫秒 UTC，0025 把“没有值”统一成空串。这里先在
// 0013 的库上写入整秒、纳秒与空串/NULL 三种历史形态，再走完整的 Open 迁移，逐列断言落库结果。
func TestMigrationNormalizesTimestampsToFixedWidth(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "legacy-times.db")
	db, err := sql.Open("sqlite", "file:"+path)
	if err != nil {
		t.Fatalf("open raw sqlite: %v", err)
	}
	applyLegacySchemaThrough(t, db, 13)
	seedLegacyTimestamps(t, db)
	if err := db.Close(); err != nil {
		t.Fatalf("close raw db: %v", err)
	}

	s, err := store.Open(context.Background(), path)
	if err != nil {
		t.Fatalf("store.Open with legacy timestamps: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })

	read, err := sql.Open("sqlite", "file:"+path+"?_pragma=query_only(1)")
	if err != nil {
		t.Fatalf("open read connection: %v", err)
	}
	t.Cleanup(func() { _ = read.Close() })

	whole := time.Date(2026, 10, 22, 16, 0, 0, 0, time.UTC)
	millis := time.Date(2026, 10, 22, 16, 0, 0, 123_000_000, time.UTC)
	fixedWhole := sql.NullString{String: "2026-10-22T16:00:00.000Z", Valid: true}
	fixedMillis := sql.NullString{String: "2026-10-22T16:00:00.123Z", Valid: true}

	cases := []struct {
		name     string
		query    string
		want     sql.NullString
		wantTime time.Time // 非零时校验时间值等价（纳秒被截断到毫秒）
	}{
		{"tenants.created_at 整秒", `SELECT created_at FROM tenants WHERE id = 't-whole'`, fixedWhole, whole},
		{"tenants.created_at 纳秒", `SELECT created_at FROM tenants WHERE id = 't-frac'`, fixedMillis, millis},
		{"tokens.created_at", `SELECT created_at FROM tokens WHERE id = 'tok-whole'`, fixedWhole, whole},
		{"tokens.last_used_at", `SELECT last_used_at FROM tokens WHERE id = 'tok-whole'`, fixedMillis, millis},
		{"tokens.revoked_at 空串保留", `SELECT revoked_at FROM tokens WHERE id = 'tok-whole'`, sql.NullString{String: "", Valid: true}, time.Time{}},
		{"tokens.revoked_at NULL 归一成空串", `SELECT revoked_at FROM tokens WHERE id = 'tok-null'`, sql.NullString{String: "", Valid: true}, time.Time{}},
		{"audit_records.ts 整秒", `SELECT ts FROM audit_records WHERE id = 'audit-whole'`, fixedWhole, whole},
		{"audit_records.ts 纳秒", `SELECT ts FROM audit_records WHERE id = 'audit-frac'`, fixedMillis, millis},
		{"jobs.created_at", `SELECT created_at FROM jobs WHERE id = 'job-1'`, fixedWhole, whole},
		{"jobs.updated_at", `SELECT updated_at FROM jobs WHERE id = 'job-1'`, fixedMillis, millis},
		{"jobs.next_attempt_at 空串保留", `SELECT next_attempt_at FROM jobs WHERE id = 'job-1'`, sql.NullString{String: "", Valid: true}, time.Time{}},
		{"accounts.created_at", `SELECT created_at FROM accounts WHERE id = 'acct-1'`, fixedWhole, whole},
		{"accounts.last_login_at", `SELECT last_login_at FROM accounts WHERE id = 'acct-1'`, fixedMillis, millis},
		{"project_members.created_at", `SELECT created_at FROM project_members WHERE account_id = 'acct-1'`, fixedWhole, whole},
		{"sessions.created_at", `SELECT created_at FROM sessions WHERE id = 'sess-1'`, fixedWhole, whole},
		{"sessions.expires_at", `SELECT expires_at FROM sessions WHERE id = 'sess-1'`, fixedMillis, millis},
		{"sessions.last_seen_at NULL 归一成空串", `SELECT last_seen_at FROM sessions WHERE id = 'sess-1'`, sql.NullString{String: "", Valid: true}, time.Time{}},
		{"sessions.revoked_at 空串保留", `SELECT revoked_at FROM sessions WHERE id = 'sess-1'`, sql.NullString{String: "", Valid: true}, time.Time{}},
		{"idempotency_keys.created_at", `SELECT created_at FROM idempotency_keys WHERE idem_key = 'k1'`, fixedWhole, whole},
		{"idempotency_keys.updated_at", `SELECT updated_at FROM idempotency_keys WHERE idem_key = 'k1'`, fixedMillis, millis},
		{"project_invites.created_at", `SELECT created_at FROM project_invites WHERE id = 'inv-1'`, fixedWhole, whole},
		{"project_invites.expires_at", `SELECT expires_at FROM project_invites WHERE id = 'inv-1'`, fixedMillis, millis},
		{"project_invites.used_at 空串保留", `SELECT used_at FROM project_invites WHERE id = 'inv-1'`, sql.NullString{String: "", Valid: true}, time.Time{}},
		{"project_invites.revoked_at NULL 归一成空串", `SELECT revoked_at FROM project_invites WHERE id = 'inv-1'`, sql.NullString{String: "", Valid: true}, time.Time{}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var got sql.NullString
			if err := read.QueryRowContext(context.Background(), tc.query).Scan(&got); err != nil {
				t.Fatalf("query %s: %v", tc.name, err)
			}
			if got != tc.want {
				t.Fatalf("%s = %+v, want %+v", tc.name, got, tc.want)
			}
			if !tc.wantTime.IsZero() {
				parsed, err := time.Parse(time.RFC3339Nano, got.String)
				if err != nil {
					t.Fatalf("parse %q: %v", got.String, err)
				}
				if !parsed.Equal(tc.wantTime) {
					t.Fatalf("%s 时间值 = %v, want %v", tc.name, parsed, tc.wantTime)
				}
			}
		})
	}

	// schema_migrations.applied_at 也必须变成固定宽度。
	var applied string
	if err := read.QueryRowContext(context.Background(),
		`SELECT applied_at FROM schema_migrations WHERE version = 1`).Scan(&applied); err != nil {
		t.Fatalf("query schema_migrations: %v", err)
	}
	if len(applied) != 24 || applied[19] != '.' || applied[23] != 'Z' {
		t.Fatalf("schema_migrations.applied_at = %q, want 固定宽度毫秒 UTC", applied)
	}
}

// seedLegacyTimestamps 写入 0015 迁移之前的时间文本：整秒没有小数、纳秒变长，
// 以及需要原样保留的空串与 NULL。
func seedLegacyTimestamps(t *testing.T, db *sql.DB) {
	t.Helper()
	stmts := []string{
		`INSERT INTO tenants (id, user_id, name, created_at) VALUES ('t-whole', 'u-whole', 'whole', '2026-10-22T16:00:00Z')`,
		`INSERT INTO tenants (id, user_id, name, created_at) VALUES ('t-frac', 'u-frac', 'frac', '2026-10-22T16:00:00.123456789Z')`,
		`INSERT INTO tokens (id, kind, tenant_id, user_id, hash, prefix, created_at, last_used_at, revoked_at)
		 VALUES ('tok-whole', 'tenant', 't-whole', 'u-whole', X'01', 'memos', '2026-10-22T16:00:00Z', '2026-10-22T16:00:00.123456789Z', '')`,
		`INSERT INTO tokens (id, kind, tenant_id, user_id, hash, prefix, created_at, revoked_at)
		 VALUES ('tok-null', 'tenant', 't-whole', 'u-whole', X'02', 'memos', '2026-10-22T16:00:00Z', NULL)`,
		`INSERT INTO audit_records (id, ts, tool, ok) VALUES ('audit-whole', '2026-10-22T16:00:00Z', 'add_message', 1)`,
		`INSERT INTO audit_records (id, ts, tool, ok) VALUES ('audit-frac', '2026-10-22T16:00:00.123456789Z', 'add_message', 1)`,
		`INSERT INTO jobs (id, kind, status, payload, created_at, updated_at, next_attempt_at)
		 VALUES ('job-1', 'memory_add', 'pending', X'7B7D', '2026-10-22T16:00:00Z', '2026-10-22T16:00:00.123456789Z', '')`,
		`INSERT INTO accounts (id, username, password_hash, created_at, last_login_at)
		 VALUES ('acct-1', 'alice', 'hash', '2026-10-22T16:00:00Z', '2026-10-22T16:00:00.123456789Z')`,
		`INSERT INTO project_members (account_id, tenant_id, role, created_at)
		 VALUES ('acct-1', 't-whole', 'owner', '2026-10-22T16:00:00Z')`,
		`INSERT INTO sessions (id, hash, account_id, created_at, expires_at, last_seen_at, revoked_at)
		 VALUES ('sess-1', X'04', 'acct-1', '2026-10-22T16:00:00Z', '2026-10-22T16:00:00.123456789Z', NULL, '')`,
		`INSERT INTO idempotency_keys (tenant_id, idem_key, tool, request_hash, state, created_at, updated_at)
		 VALUES ('t-whole', 'k1', 'add_message', 'h1', 'in_progress', '2026-10-22T16:00:00Z', '2026-10-22T16:00:00.123456789Z')`,
		`INSERT INTO project_invites (id, tenant_id, role, token_hash, created_at, expires_at, used_at, revoked_at)
		 VALUES ('inv-1', 't-whole', 'viewer', X'05', '2026-10-22T16:00:00Z', '2026-10-22T16:00:00.123456789Z', '', NULL)`,
	}
	for _, stmt := range stmts {
		if _, err := db.ExecContext(context.Background(), stmt); err != nil {
			t.Fatalf("seed legacy row: %v\n%s", err, stmt)
		}
	}
}

// applyLegacySchema 把 0001..0010 的迁移按顺序执行并登记版本，模拟线上已有的库。
func applyLegacySchema(t *testing.T, db *sql.DB) {
	t.Helper()
	applyLegacySchemaThrough(t, db, 10)
}

// applyLegacySchemaThrough 执行版本号不超过 maxVersion 的迁移，供不同升级路径复用。
func applyLegacySchemaThrough(t *testing.T, db *sql.DB, maxVersion int) {
	t.Helper()
	entries, err := migrations.FS.ReadDir(".")
	if err != nil {
		t.Fatalf("read migrations: %v", err)
	}
	if _, err := db.ExecContext(context.Background(), `CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)`); err != nil {
		t.Fatalf("create schema_migrations: %v", err)
	}
	for _, entry := range entries {
		prefix, _, ok := strings.Cut(entry.Name(), "_")
		if !ok || !strings.HasSuffix(entry.Name(), ".sql") {
			continue
		}
		version, err := strconv.Atoi(prefix)
		if err != nil {
			t.Fatalf("bad migration name %s: %v", entry.Name(), err)
		}
		if version > maxVersion {
			continue
		}
		body, err := migrations.FS.ReadFile(entry.Name())
		if err != nil {
			t.Fatalf("read %s: %v", entry.Name(), err)
		}
		if _, err := db.ExecContext(context.Background(), string(body)); err != nil {
			t.Fatalf("apply %s: %v", entry.Name(), err)
		}
		if _, err := db.ExecContext(context.Background(), `INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)`,
			version, time.Now().UTC().Format(time.RFC3339)); err != nil {
			t.Fatalf("record %s: %v", entry.Name(), err)
		}
	}
}
