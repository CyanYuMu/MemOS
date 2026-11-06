package store

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func (s *Store) GetOnboarding(ctx context.Context, account, project string) (domain.OnboardingProgress, error) {
	p := domain.OnboardingProgress{Status: "pending", Version: 1}
	var checkpoint string
	err := s.read.QueryRowContext(ctx, `SELECT status,revision,COALESCE((SELECT step FROM onboarding_projects WHERE account_id=? AND project_id=?),0),COALESCE((SELECT chat_checkpoint FROM onboarding_projects WHERE account_id=? AND project_id=?),'{}') FROM onboarding_accounts WHERE account_id=?`, account, project, account, project, account).Scan(&p.Status, &p.Revision, &p.Step, &checkpoint)
	if isNotFound(err) {
		return p, nil
	}
	if err != nil {
		return p, fmt.Errorf("store: read onboarding: %w", err)
	}
	if checkpoint != "{}" {
		var c domain.OnboardingCheckpoint
		if err := json.Unmarshal([]byte(checkpoint), &c); err != nil {
			return p, fmt.Errorf("store: decode onboarding checkpoint: %w", err)
		}
		p.Chat = &c
	}
	return p, nil
}

func (s *Store) SaveOnboarding(ctx context.Context, account, project string, p domain.OnboardingProgress) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: begin onboarding: %w", err)
	}
	defer func() { _ = tx.Rollback() }()
	checkpoint, err := json.Marshal(p.Chat)
	if err != nil {
		return fmt.Errorf("store: encode onboarding checkpoint: %w", err)
	}
	if p.Chat == nil {
		checkpoint = []byte("{}")
	}
	at := encodeTime(time.Now())
	if _, err := tx.ExecContext(ctx, `INSERT OR IGNORE INTO onboarding_accounts(account_id,status,updated_at) VALUES(?,'pending',?)`, account, at); err != nil {
		return fmt.Errorf("store: initialize onboarding: %w", err)
	}
	r, err := tx.ExecContext(ctx, `UPDATE onboarding_accounts SET status=?,revision=revision+1,updated_at=? WHERE account_id=? AND revision=?`, p.Status, at, account, p.Revision)
	if err != nil {
		return fmt.Errorf("store: update onboarding: %w", err)
	}
	n, err := r.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: count onboarding: %w", err)
	}
	if n != 1 {
		return domain.Wrap(domain.ErrConflict, "onboarding", "进度已更新，请重试", nil)
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO onboarding_projects(account_id,project_id,step,chat_checkpoint) VALUES(?,?,?,?) ON CONFLICT(account_id,project_id) DO UPDATE SET step=excluded.step,chat_checkpoint=excluded.chat_checkpoint`, account, project, p.Step, string(checkpoint)); err != nil {
		return fmt.Errorf("store: save onboarding checkpoint: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit onboarding: %w", err)
	}
	return nil
}
