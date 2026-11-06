package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 独立序号不依赖可被VACUUM或清理重用的rowid，重放只能保留首次快照。
func (s *Store) AppendMemoryEvent(ctx context.Context, event domain.MemoryEvent) error {
	if event.TenantID == "" || event.ID == "" || event.Key == "" {
		return errors.New("store: memory event scope or identity missing")
	}
	body, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("store: encode memory event: %w", err)
	}
	if len(body) > 256<<10 {
		return errors.New("store: memory event exceeds size limit")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: begin memory event: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	var exists int
	err = tx.QueryRowContext(ctx, `SELECT 1 FROM memory_events WHERE tenant_id=? AND event_key=?`, event.TenantID, event.Key).Scan(&exists)
	if err == nil {
		return nil
	}
	if !isNotFound(err) {
		return fmt.Errorf("store: read memory event identity: %w", err)
	}
	var seq int64
	if err := tx.QueryRowContext(ctx, `UPDATE memory_event_sequence SET value=value+1 WHERE id=1 RETURNING value`).Scan(&seq); err != nil {
		return fmt.Errorf("store: allocate memory event sequence: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO memory_events(sequence,id,tenant_id,event_key,payload) VALUES(?,?,?,?,?)`, seq, event.ID, event.TenantID, event.Key, string(body)); err != nil {
		return fmt.Errorf("store: append memory event: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit memory event: %w", err)
	}
	return nil
}

// 首次取最近一页，增量取最早未读页；相同时间戳也不丢事件。
func (s *Store) ListMemoryEvents(ctx context.Context, tenant string, after int64, limit int, latest bool) ([]domain.MemoryEvent, error) {
	if tenant == "" || after < 0 {
		return nil, errors.New("store: invalid memory event scope")
	}
	limit = max(1, min(limit, 51))
	order := "ASC"
	if latest {
		order = "DESC"
	}
	return s.readMemoryEvents(ctx, tenant, `SELECT sequence,payload FROM memory_events WHERE tenant_id=? AND sequence>? ORDER BY sequence `+order+` LIMIT ?`, tenant, after, limit)
}

// 持久化调用ID定位历史快照，不依赖最近事件窗口或当前实体。
func (s *Store) MemoryEventsByCalls(ctx context.Context, tenant string, calls []string) ([]domain.MemoryEvent, error) {
	if tenant == "" || len(calls) == 0 || len(calls) > 20 {
		return nil, errors.New("store: invalid memory event lookup scope")
	}
	args := []any{tenant}
	for _, id := range calls {
		args = append(args, id)
	}
	query := `SELECT sequence,payload FROM memory_events WHERE tenant_id=? AND json_extract(payload,'$.CallID') IN (` + strings.TrimSuffix(strings.Repeat("?,", len(calls)), ",") + `) ORDER BY sequence DESC`
	return s.readMemoryEvents(ctx, tenant, query, args...)
}

func (s *Store) readMemoryEvents(ctx context.Context, tenant, query string, args ...any) ([]domain.MemoryEvent, error) {
	rows, err := s.read.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("store: list memory events: %w", err)
	}
	defer func() { _ = rows.Close() }()
	out := []domain.MemoryEvent{}
	for rows.Next() {
		var seq int64
		var body string
		if err := rows.Scan(&seq, &body); err != nil {
			return nil, fmt.Errorf("store: scan memory event: %w", err)
		}
		var event domain.MemoryEvent
		if err := json.Unmarshal([]byte(body), &event); err != nil {
			return nil, fmt.Errorf("store: decode memory event: %w", err)
		}
		if event.TenantID != tenant {
			return nil, errors.New("store: memory event scope mismatch")
		}
		event.Sequence = seq
		out = append(out, event)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("store: iterate memory events: %w", err)
	}
	return out, nil
}
