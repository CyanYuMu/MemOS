package account

import (
	"context"
	"strings"

	"github.com/alexedwards/argon2id"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
)

// GetAccount 按 ID 取账号。
func (s *Service) GetAccount(ctx context.Context, id string) (domain.Account, error) {
	return s.store.GetAccount(ctx, id)
}

// ListAccountsPage 按用户名/显示名过滤后分页，返回当页与过滤后的总数。
//
// 过滤与分页都下沉到 SQL，这里只做参数归一：页码从 1 起，页大小默认 20、上限 200。
func (s *Service) ListAccountsPage(ctx context.Context, query string, page, size int) ([]domain.Account, int, error) {
	size = num.WithDefault(size, defaultAccountPageSize, 1, maxAccountPageSize)
	page = max(page, 1)
	return s.store.ListAccountsPage(ctx, strings.TrimSpace(query), size, (page-1)*size)
}

const (
	defaultAccountPageSize = 20
	maxAccountPageSize     = 200
)

// PasswordChangeInput 是改密入参。
type PasswordChangeInput struct {
	AccountID   string
	SessionID   string
	OldPassword string
	NewPassword string
	RemoteIP    string
}

// ChangePassword 校验原口令后改口令，吊销除当前会话外的全部会话，并清掉登录失败计数。
func (s *Service) ChangePassword(ctx context.Context, in PasswordChangeInput) error {
	defer s.invalidatePrincipalCache()
	if err := validatePassword(in.NewPassword); err != nil {
		return err
	}

	acct, err := s.store.GetAccount(ctx, in.AccountID)
	if err != nil {
		return err
	}
	// 与登录共用节流：拿到会话的人也不能无限猜原口令。
	now := s.now()
	if s.throttle.Blocked(ctx, acct.Username, in.RemoteIP) {
		return domain.Wrap(domain.ErrRateLimited, "change_password", "尝试次数过多，请稍后再试", nil)
	}
	// argon2 是百毫秒级的 CPU 操作，放在锁外，避免不同账号的改密互相排队。
	match, err := argon2id.ComparePasswordAndHash(in.OldPassword, acct.PasswordHash)
	if err != nil || !match {
		s.throttle.Fail(ctx, acct.Username, in.RemoteIP)
		return domain.Wrap(domain.ErrUnauthorized, "change_password", "原口令不正确", err)
	}
	hash, err := argon2id.CreateHash(in.NewPassword, s.opts.Argon2)
	if err != nil {
		return err
	}

	s.adminMu.Lock()
	defer s.adminMu.Unlock()
	// 重新读取，避免覆盖并发发生的禁用/管理员变更。
	acct, err = s.store.GetAccount(ctx, in.AccountID)
	if err != nil {
		return err
	}
	acct.PasswordHash = hash
	if err := s.store.UpdateAccount(ctx, acct); err != nil {
		return err
	}
	if _, err := s.store.RevokeAccountSessionsExcept(ctx, in.AccountID, in.SessionID, now); err != nil {
		return err
	}
	s.throttle.Clear(ctx, acct.Username)
	return nil
}

// Sessions 返回账号当前有效的会话（未吊销且未过期），按创建时间倒序。
func (s *Service) Sessions(ctx context.Context, accountID string) ([]domain.Session, error) {
	all, err := s.store.ListSessions(ctx, accountID)
	if err != nil {
		return nil, err
	}
	now := s.now()
	out := make([]domain.Session, 0, len(all))
	for i := range all {
		if all[i].RevokedAt == nil && now.Before(all[i].ExpiresAt) {
			out = append(out, all[i])
		}
	}
	return out, nil
}

// RevokeOtherSessions 吊销账号除 keepID 外的全部会话，返回吊销条数。
func (s *Service) RevokeOtherSessions(ctx context.Context, accountID, keepID string) (int64, error) {
	defer s.invalidatePrincipalCache()
	return s.store.RevokeAccountSessionsExcept(ctx, accountID, keepID, s.now())
}
