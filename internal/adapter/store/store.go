package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/migrations"

	"modernc.org/sqlite" // 注册 database/sql 驱动，同时用于识别约束错误
)

// Store 是 SQLite 实现的控制面仓储。它不 import service，靠方法签名结构化满足端口。
//
// 写操作走单连接（SQLite 同一时刻只有一个写者）；文件库的读操作走独立只读连接池，
// WAL 下读不阻塞写，避免鉴权、审计查询等读请求被写事务串行化。
type Store struct {
	db   *sql.DB
	read *sql.DB

	// usageCache 缓存项目已落库用量，避免每个分块写都全表 SUM(LENGTH(payload))。
	// 所有会改动存活行的路径写完就失效；准入的最终裁决仍在 ApplyRows 事务里。
	usageMu    sync.Mutex
	usageCache map[string]usageEntry
	// usageGen 在每次失效时递增；锁外查询回来若发现代数变了就不回填，避免
	// “读事务开始于写入之前、回填发生在失效之后”把旧值重新放进缓存。
	usageGen uint64
}

const readPoolSize = 8

// Open 打开（必要时创建）SQLite 数据库并执行迁移。
func Open(ctx context.Context, path string) (*Store, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("store: db path must not be empty")
	}
	dsn := "file:" + path + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=foreign_keys(1)&_pragma=synchronous(NORMAL)"
	if path == ":memory:" {
		// 内存库同样要开外键：测试与本地演示走这条路径，缺了它级联删除的语义
		// 会和文件库不一致。
		dsn = "file::memory:?cache=shared&_pragma=busy_timeout(5000)&_pragma=foreign_keys(1)"
	} else {
		if dir := filepath.Dir(path); dir != "." && dir != "" {
			if err := os.MkdirAll(dir, 0o750); err != nil {
				return nil, fmt.Errorf("store: create data dir: %w", err)
			}
		}
	}

	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("store: open: %w", err)
	}
	// SQLite 同一时刻只允许一个写者；单连接避免写事务互相抢锁。
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	read := db
	if path != ":memory:" {
		// WAL 允许并发读；query_only 保证这条连接不会意外写入。
		read, err = sql.Open("sqlite", "file:"+path+"?_pragma=busy_timeout(5000)&_pragma=query_only(1)")
		if err != nil {
			_ = db.Close()
			return nil, fmt.Errorf("store: open read pool: %w", err)
		}
		read.SetMaxOpenConns(readPoolSize)
		read.SetMaxIdleConns(readPoolSize)
		if err := read.PingContext(ctx); err != nil {
			_ = read.Close()
			_ = db.Close()
			return nil, fmt.Errorf("store: ping read pool: %w", err)
		}
	}

	s := &Store{db: db, read: read}
	if err := s.db.PingContext(ctx); err != nil {
		_ = s.Close()
		return nil, fmt.Errorf("store: ping: %w", err)
	}
	if err := s.migrate(ctx); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

// Ping 检查数据库可用性。走读池，读不可用时同样视为未就绪。
func (s *Store) Ping(ctx context.Context) error { return s.read.PingContext(ctx) }

// Close 关闭读写连接，可重复调用。内存库的读写是同一个连接，只关一次。
func (s *Store) Close() error {
	if s.read != nil && s.read != s.db {
		_ = s.read.Close()
	}
	return s.db.Close()
}

func (s *Store) migrate(ctx context.Context) error {
	if _, err := s.db.ExecContext(ctx,
		`CREATE TABLE IF NOT EXISTS schema_migrations (
			version INTEGER PRIMARY KEY,
			applied_at TEXT NOT NULL,
			checksum TEXT NOT NULL DEFAULT ''
		)`,
	); err != nil {
		return fmt.Errorf("store: create schema_migrations: %w", err)
	}
	// 旧库的 schema_migrations 没有 checksum 列。先补列，再谈校验：
	// 已有行校验和为空表示“迁移是先于校验机制应用的”，首轮自动回填。
	if err := s.addChecksumColumn(ctx); err != nil {
		return err
	}

	entries, err := migrations.FS.ReadDir(".")
	if err != nil {
		return fmt.Errorf("store: read migrations: %w", err)
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)

	// 迁移号只按整数记录（schema_migrations 没有文件名）。迁移文件发布后不可修改，
	// 校验和会拦住“同一版本号两份内容”，因此不再需要为历史上 rebase 复用编号的库
	// 做额外识别。
	for _, name := range names {
		if err := s.applyMigrationFile(ctx, name); err != nil {
			return err
		}
	}
	return nil
}

// applyMigrationFile 对单个迁移文件做“已应用校验 / 首次应用”。
func (s *Store) applyMigrationFile(ctx context.Context, name string) error {
	version, err := migrationVersion(name)
	if err != nil {
		return err
	}
	body, err := migrations.FS.ReadFile(name)
	if err != nil {
		return fmt.Errorf("store: read migration %s: %w", name, err)
	}
	sum := migrationChecksum(body)
	applied, stored, err := s.migrationStatus(ctx, version)
	if err != nil {
		return err
	}
	if applied {
		return s.verifyMigrationChecksum(ctx, migrationCheck{Name: name, Version: version, Stored: stored, Want: sum})
	}
	if err := s.applyMigration(ctx, version, string(body), sum); err != nil {
		return fmt.Errorf("store: apply migration %s: %w", name, err)
	}
	return nil
}

// migrationChecksum 是迁移文件内容的 SHA-256。迁移一旦应用就不能改：
// 改内容而不改版本号时，不同环境的表结构会悄悄分叉。
func migrationChecksum(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

func (s *Store) addChecksumColumn(ctx context.Context) error {
	exists, err := s.columnExists(ctx, "schema_migrations", "checksum")
	if err != nil {
		return err
	}
	if exists {
		return nil
	}
	if _, err := s.db.ExecContext(ctx,
		`ALTER TABLE schema_migrations ADD COLUMN checksum TEXT NOT NULL DEFAULT ''`,
	); err != nil {
		return fmt.Errorf("store: add schema_migrations.checksum: %w", err)
	}
	return nil
}

// migrationCheck 是一次校验和核对的入参。
type migrationCheck struct {
	Name    string
	Version int
	Stored  string
	Want    string
}

// verifyMigrationChecksum 校验已应用迁移的内容。空校验和是升级前的历史行，
// 首次启动时回填；非空且不一致则拒绝启动，避免同一版本号对应两份不同内容。
func (s *Store) verifyMigrationChecksum(ctx context.Context, c migrationCheck) error {
	if c.Stored == "" {
		if _, err := s.db.ExecContext(ctx,
			`UPDATE schema_migrations SET checksum = ? WHERE version = ?`, c.Want, c.Version); err != nil {
			return fmt.Errorf("store: backfill migration %d checksum: %w", c.Version, err)
		}
		return nil
	}
	if c.Stored != c.Want {
		return fmt.Errorf(`store: 迁移 %s（版本 %d）内容与已应用的校验和不一致：`+
			`已应用 %s，当前文件 %s。迁移文件发布后不可修改；请新增一个更高版本的迁移`,
			c.Name, c.Version, c.Stored[:12], c.Want[:12])
	}
	return nil
}

func migrationVersion(name string) (int, error) {
	prefix, _, ok := strings.Cut(name, "_")
	if !ok {
		return 0, fmt.Errorf("store: migration %q must start with <version>_", name)
	}
	version, err := strconv.Atoi(prefix)
	if err != nil {
		return 0, fmt.Errorf("store: migration %q has non-numeric version", name)
	}
	return version, nil
}

// migrationStatus 同时返回是否已应用与记录的校验和。
func (s *Store) migrationStatus(ctx context.Context, version int) (bool, string, error) {
	var checksum string
	err := s.db.QueryRowContext(ctx,
		`SELECT checksum FROM schema_migrations WHERE version = ?`, version).Scan(&checksum)
	if errors.Is(err, sql.ErrNoRows) {
		return false, "", nil
	}
	if err != nil {
		return false, "", fmt.Errorf("store: query migration %d: %w", version, err)
	}
	return true, checksum, nil
}

func (s *Store) columnExists(ctx context.Context, table, column string) (bool, error) {
	var n int
	if err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM pragma_table_info(?) WHERE name = ?`, table, column).Scan(&n); err != nil {
		return false, fmt.Errorf("store: inspect %s.%s: %w", table, column, err)
	}
	return n > 0, nil
}

func (s *Store) applyMigration(ctx context.Context, version int, body, checksum string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	// 迁移文件可能包含多条语句；驱动支持一次执行多语句。
	if _, err := tx.ExecContext(ctx, body); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO schema_migrations (version, applied_at, checksum) VALUES (?, ?, ?)`,
		version, encodeTime(time.Now()), checksum,
	); err != nil {
		return err
	}
	return tx.Commit()
}

// timeLayout 是时间列的固定宽度格式：UTC + 毫秒，输出形如 2026-10-23T00:00:00.000Z。
//
// 不用 time.RFC3339Nano：它按需省略小数位，整秒是 2026-...00Z、带小数是
// 2026-...00.123Z，长度不一致。SQLite 的 TEXT 比较是字典序，'.'(0x2E) < 'Z'(0x5A)，
// 因此 ts >= '2026-...00Z' 会漏掉同一秒内所有带小数的行，ORDER BY ts 也会在整秒内错序。
// 固定宽度后“字符串序 = 时间序”，since/until、purge、ClaimJob 与 session 过期比较才成立。
const timeLayout = "2006-01-02T15:04:05.000Z07:00"

func encodeTime(t time.Time) string { return t.UTC().Format(timeLayout) }

// decodeTime 解析时间列，只接受 encodeTime 写出的固定宽度格式。
// 0015 已把历史行规范化成同一格式；再宽进就会把“字符串序 = 时间序”的前提悄悄放掉。
func decodeTime(raw string) (time.Time, error) {
	if raw == "" {
		return time.Time{}, nil
	}
	return time.Parse(timeLayout, raw)
}

// optionalTimeText 把可选时间写成空串或固定宽度文本。
//
// “没有值”统一用空串，不用 NULL：同一列里混着 NULL 和空串时，每个查询都得写
// (x IS NULL OR x = ”)，漏一半就少行（迁移 0025 已把存量 NULL 归一）。
func optionalTimeText(t *time.Time) string {
	if t == nil {
		return ""
	}
	return encodeTime(*t)
}

// setOptionalTime 把可选时间列写进目标：空串表示“没有值”，置 nil。
func setOptionalTime(dst **time.Time, raw string) error {
	if raw == "" {
		*dst = nil
		return nil
	}
	t, err := decodeTime(raw)
	if err != nil {
		return err
	}
	*dst = &t
	return nil
}

func isNotFound(err error) bool { return errors.Is(err, sql.ErrNoRows) }

// SQLite 扩展结果码：唯一键与主键冲突。
const (
	sqliteConstraintPrimaryKey = 1555
	sqliteConstraintUnique     = 2067
)

func isUniqueViolation(err error) bool {
	if sqliteErr, ok := errors.AsType[*sqlite.Error](err); ok {
		code := sqliteErr.Code()
		return code == sqliteConstraintUnique || code == sqliteConstraintPrimaryKey
	}
	// 驱动没给 typed error 时保留文本兜底，避免冲突退化成 500。
	return err != nil && strings.Contains(err.Error(), "UNIQUE constraint failed")
}

// uniqueOn 判断唯一冲突是否由指定列触发，用于把“同一个 id”与“同一个上游 user_id”
// 两类冲突区分成不同的可辨认错误。驱动文本形如
// "constraint failed: UNIQUE constraint failed: tenants.user_id (2067)"。
func uniqueOn(err error, columns string) bool {
	if err == nil {
		return false
	}
	return strings.Contains(strings.ToLower(err.Error()),
		"unique constraint failed: "+strings.ToLower(columns))
}

func notFound(op, resource string) error {
	return domain.Wrap(domain.ErrNotFound, op, resource+" 不存在", nil)
}

func conflict(op, message string, err error) error {
	return domain.Wrap(domain.ErrConflict, op, message, err)
}

// DBStats 暴露写连接与读连接池的统计，供指标采样使用。
func (s *Store) DBStats() (write, read sql.DBStats) {
	return s.db.Stats(), s.read.Stats()
}
