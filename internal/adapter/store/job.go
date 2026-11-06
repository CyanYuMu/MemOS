package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// jobColumns 是任务元数据的列顺序；ListJobs 与 RequeueJob 共用，避免两处
// 顺序不一致时扫错字段。
const jobColumns = `id, kind, status, tenant_id, user_id, attempts, last_error,
		next_attempt_at, created_at, updated_at`

// scanJob 读一行任务元数据（不含 payload：记忆正文不进入运维查询结果）。
func scanJob(row scanner) (domain.Job, error) {
	var (
		j                                           domain.Job
		kind, status, nextAttempt, created, updated string
	)
	if err := row.Scan(&j.ID, &kind, &status, &j.TenantID, &j.UserID, &j.Attempts,
		&j.LastError, &nextAttempt, &created, &updated); err != nil {
		return domain.Job{}, err
	}
	j.Kind = domain.JobKind(kind)
	j.Status = domain.JobStatus(status)
	var err error
	if j.NextAttemptAt, err = decodeTime(nextAttempt); err != nil {
		return domain.Job{}, err
	}
	if j.CreatedAt, err = decodeTime(created); err != nil {
		return domain.Job{}, err
	}
	if j.UpdatedAt, err = decodeTime(updated); err != nil {
		return domain.Job{}, err
	}
	return j, nil
}

// EnqueueJob 入队一个异步任务。
func (s *Store) EnqueueJob(ctx context.Context, j domain.Job) error {
	if j.ID == "" || j.Kind == "" {
		return domain.Invalid("enqueue_job", "任务 id 与 kind 不能为空")
	}
	now := time.Now()
	if j.CreatedAt.IsZero() {
		j.CreatedAt = now
	}
	if j.UpdatedAt.IsZero() {
		j.UpdatedAt = j.CreatedAt
	}
	if j.NextAttemptAt.IsZero() {
		j.NextAttemptAt = j.CreatedAt
	}
	status := j.Status
	if status == "" {
		status = domain.JobStatusPending
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO jobs (id, kind, status, tenant_id, user_id, payload, attempts, last_error, next_attempt_at, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		j.ID, string(j.Kind), string(status), j.TenantID, j.UserID, j.Payload,
		j.Attempts, j.LastError, encodeTime(j.NextAttemptAt), encodeTime(j.CreatedAt), encodeTime(j.UpdatedAt))
	if err != nil {
		return fmt.Errorf("store: enqueue job: %w", err)
	}
	return nil
}

// ClaimJob 原子地认领最早的可执行任务；没有可认领任务返回 ok=false。
//
// 可执行指 status=pending 且 next_attempt_at 不晚于 now：处在退避期的重试任务
// 仍是 pending 但不可认领，因此不会阻塞同租户的其它任务。
// 同一租户同时只允许一个 running 任务：已有 running 的租户会被跳过，
// 不同租户之间可以并发执行。SQLite 单连接保证事务串行，因此这个判断没有竞态。
// stale 任务的回收由 ResetStaleJobs 低频执行，不在每次认领时写库。
func (s *Store) ClaimJob(ctx context.Context, now time.Time) (domain.Job, bool, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Job{}, false, fmt.Errorf("store: claim job begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var (
		j                                           domain.Job
		kind, status, nextAttempt, created, updated string
		payload                                     []byte
	)
	row := tx.QueryRowContext(ctx, `
		SELECT j.id, j.kind, j.status, j.tenant_id, j.user_id, j.payload, j.attempts, j.last_error,
		       j.next_attempt_at, j.created_at, j.updated_at
		FROM jobs j
		WHERE j.status = ? AND j.next_attempt_at <= ? AND NOT EXISTS (
			SELECT 1 FROM jobs r WHERE r.status = ? AND r.tenant_id = j.tenant_id
		)
		ORDER BY j.created_at ASC LIMIT 1`,
		string(domain.JobStatusPending), encodeTime(now), string(domain.JobStatusRunning))
	err = row.Scan(&j.ID, &kind, &status, &j.TenantID, &j.UserID, &payload, &j.Attempts,
		&j.LastError, &nextAttempt, &created, &updated)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return domain.Job{}, false, nil
		}
		return domain.Job{}, false, fmt.Errorf("store: claim job select: %w", err)
	}
	j.Kind = domain.JobKind(kind)
	j.Status = domain.JobStatusRunning
	j.Payload = payload
	j.Attempts++
	if j.NextAttemptAt, err = decodeTime(nextAttempt); err != nil {
		return domain.Job{}, false, err
	}
	if j.CreatedAt, err = decodeTime(created); err != nil {
		return domain.Job{}, false, err
	}
	if j.UpdatedAt, err = decodeTime(updated); err != nil {
		return domain.Job{}, false, err
	}

	if _, err := tx.ExecContext(ctx,
		`UPDATE jobs SET status = ?, attempts = ?, updated_at = ? WHERE id = ?`,
		string(domain.JobStatusRunning), j.Attempts, encodeTime(now), j.ID); err != nil {
		return domain.Job{}, false, fmt.Errorf("store: claim job update: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return domain.Job{}, false, fmt.Errorf("store: claim job commit: %w", err)
	}
	return j, true, nil
}

// ResetStaleJobs 把 updated_at 早于 staleBefore 的 running 任务重置为 pending，返回条数。
// 用于回收进程崩溃遗留的任务；attempts 保留，崩溃一次算一次尝试。
// next_attempt_at 置为 now：崩溃不是上游失败，回收后应立即重试而不是等退避。
func (s *Store) ResetStaleJobs(ctx context.Context, now, staleBefore time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`UPDATE jobs SET status = ?, next_attempt_at = ?, updated_at = ? WHERE status = ? AND updated_at < ?`,
		string(domain.JobStatusPending), encodeTime(now), encodeTime(now),
		string(domain.JobStatusRunning), encodeTime(staleBefore))
	if err != nil {
		return 0, fmt.Errorf("store: reset stale jobs: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: reset stale jobs rows: %w", err)
	}
	return n, nil
}

// PurgeFinishedJobsBefore 删除 updated_at 早于 before 的终态任务，返回删除条数。
func (s *Store) PurgeFinishedJobsBefore(ctx context.Context, before time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM jobs WHERE status IN (?, ?) AND updated_at < ?`,
		string(domain.JobStatusSucceeded), string(domain.JobStatusFailed), encodeTime(before))
	if err != nil {
		return 0, fmt.Errorf("store: purge finished jobs: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: purge finished jobs rows: %w", err)
	}
	return n, nil
}

// RescheduleJob 把失败的 running 任务放回 pending，并指定下次可认领时间。
// 行、job ID、created_at 与 attempts 都不变：重试是同一条任务，不是新任务。
// 只接受 running 任务，避免把终态任务或别人的任务重新拉起。
func (s *Store) RescheduleJob(ctx context.Context, p domain.JobReschedule) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE jobs SET status = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
		WHERE id = ? AND status = ?`,
		string(domain.JobStatusPending), p.LastErr, encodeTime(p.Next), encodeTime(p.Now),
		p.ID, string(domain.JobStatusRunning))
	if err != nil {
		return fmt.Errorf("store: reschedule job: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: reschedule job rows: %w", err)
	}
	if n == 0 {
		return notFound("reschedule_job", "任务")
	}
	return nil
}

// GetJob 按 id 读取任务元数据（不含 payload），不存在返回 not found。
func (s *Store) GetJob(ctx context.Context, id string) (domain.Job, error) {
	job, err := scanJob(s.read.QueryRowContext(ctx, `SELECT `+jobColumns+` FROM jobs WHERE id = ?`, id))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return domain.Job{}, notFound("get_job", "任务")
		}
		return domain.Job{}, fmt.Errorf("store: get job: %w", err)
	}
	return job, nil
}

// RequeueJob 人工重放一条失败（死信）任务：status → pending、attempts 归零、
// next_attempt_at 置为 now、清掉 last_error，返回重放后的任务。
//
// 与 RescheduleJob 的分工：那个是 worker 的同轮重试（只接受 running，attempts 继续
// 累加）；这个是运维动作 —— 人已经处理过失败原因，应该重新给满次数。
// 只接受 failed：把 pending/running 再拉一次会重复写，把 succeeded 拉起来会写第二遍。
func (s *Store) RequeueJob(ctx context.Context, id string, now time.Time) (domain.Job, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Job{}, fmt.Errorf("store: requeue job begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	job, err := scanJob(tx.QueryRowContext(ctx, `SELECT `+jobColumns+` FROM jobs WHERE id = ?`, id))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return domain.Job{}, notFound("requeue_job", "任务")
		}
		return domain.Job{}, fmt.Errorf("store: load job for requeue: %w", err)
	}
	if job.Status != domain.JobStatusFailed {
		return domain.Job{}, conflict("requeue_job",
			fmt.Sprintf("只有失败的任务可以重放（当前 %s）", job.Status), nil)
	}
	res, err := tx.ExecContext(ctx, `
		UPDATE jobs SET status = ?, attempts = 0, last_error = '', next_attempt_at = ?, updated_at = ?
		WHERE id = ? AND status = ?`,
		string(domain.JobStatusPending), encodeTime(now), encodeTime(now),
		id, string(domain.JobStatusFailed),
	)
	if err != nil {
		return domain.Job{}, fmt.Errorf("store: requeue job: %w", err)
	}
	// 同一任务被并发重放时，后到的这条 UPDATE 命中 0 行；不检查就会谎报成功。
	if n, err := res.RowsAffected(); err == nil && n == 0 {
		return domain.Job{}, conflict("requeue_job", "任务状态已变化，请刷新后重试", nil)
	}
	if err := tx.Commit(); err != nil {
		return domain.Job{}, fmt.Errorf("store: requeue job commit: %w", err)
	}
	job.Status = domain.JobStatusPending
	job.Attempts = 0
	job.LastError = ""
	job.NextAttemptAt = now
	job.UpdatedAt = now
	return job, nil
}

// FinishJob 把任务置为 succeeded/failed。
func (s *Store) FinishJob(ctx context.Context, p domain.JobCompletion) error {
	if p.Status != domain.JobStatusSucceeded && p.Status != domain.JobStatusFailed {
		return domain.Invalid("finish_job", "任务终态只能是 succeeded 或 failed")
	}
	res, err := s.db.ExecContext(ctx,
		`UPDATE jobs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?`,
		string(p.Status), p.LastErr, encodeTime(p.Now), p.ID)
	if err != nil {
		return fmt.Errorf("store: finish job: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: finish job rows: %w", err)
	}
	if n == 0 {
		return notFound("finish_job", "任务")
	}
	return nil
}

// ListJobs 返回死信排查用的任务列表与总数，按 created_at 倒序。
// 不读取 payload：记忆正文不进入运维查询结果。limit 缺省 50、上限 200。
func (s *Store) ListJobs(ctx context.Context, f domain.JobFilter) ([]domain.Job, int, error) {
	where := "1=1"
	args := make([]any, 0, 3)
	if f.Status != "" {
		where += " AND status = ?"
		args = append(args, string(f.Status))
	}
	if f.Kind != "" {
		where += " AND kind = ?"
		args = append(args, string(f.Kind))
	}
	if f.TenantID != "" {
		where += " AND tenant_id = ?"
		args = append(args, f.TenantID)
	}

	var total int
	if err := s.read.QueryRowContext(ctx,
		`SELECT COUNT(1) FROM jobs WHERE `+where, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("store: count jobs: %w", err)
	}

	limit := f.Limit
	if limit <= 0 {
		limit = 50
	}
	limit = min(limit, 200)
	offset := max(f.Offset, 0)
	pageArgs := append(append([]any(nil), args...), limit, offset)
	rows, err := s.read.QueryContext(ctx, `
		SELECT `+jobColumns+`
		FROM jobs WHERE `+where+`
		ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?`, pageArgs...)
	if err != nil {
		return nil, 0, fmt.Errorf("store: list jobs: %w", err)
	}
	defer func() { _ = rows.Close() }()

	jobs := make([]domain.Job, 0, limit)
	for rows.Next() {
		j, err := scanJob(rows)
		if err != nil {
			return nil, 0, fmt.Errorf("store: scan job: %w", err)
		}
		jobs = append(jobs, j)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, fmt.Errorf("store: list jobs rows: %w", err)
	}
	return jobs, total, nil
}

// JobStats 返回异步任务的积压（pending/running/failed）与最老 pending 时间，供指标采样使用。
func (s *Store) JobStats(ctx context.Context) (pending, running, failed int64, oldest time.Time, err error) {
	var created string
	err = s.read.QueryRowContext(ctx, `
		SELECT COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0),
		       COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0),
		       COALESCE(SUM(CASE WHEN status = ? THEN 1 ELSE 0 END), 0),
		       COALESCE(MIN(CASE WHEN status = ? THEN created_at END), '')
		FROM jobs`,
		string(domain.JobStatusPending), string(domain.JobStatusRunning),
		string(domain.JobStatusFailed), string(domain.JobStatusPending),
	).Scan(&pending, &running, &failed, &created)
	if err != nil {
		return 0, 0, 0, time.Time{}, fmt.Errorf("store: job stats: %w", err)
	}
	oldest, err = decodeTime(created)
	if err != nil {
		return 0, 0, 0, time.Time{}, err
	}
	return pending, running, failed, oldest, nil
}
