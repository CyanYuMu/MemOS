package account

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"github.com/alexedwards/argon2id"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const maxThrottleKeyLen = 64

func loginFailed() error {
	return domain.Wrap(domain.ErrUnauthorized, "login", "用户名或口令错误", nil)
}

func rateLimited() error {
	return domain.Wrap(domain.ErrRateLimited, "login", "尝试次数过多，请稍后再试", nil)
}

// Login 校验口令并签发会话。账号不存在、口令错误、账号已禁用返回同一条错误。
// raw 是会话 Token 明文，只在这里返回一次。
func (s *Service) Login(ctx context.Context, username, password, remoteIP string) (string, domain.Session, error) {
	if s.opts.External.Required {
		return "", domain.Session{}, domain.Wrap(domain.ErrForbidden, "login", "已启用统一认证，请通过零信任网关登录", nil)
	}
	key := throttleKey(username)
	now := s.now()
	if s.throttle.Blocked(ctx, key, remoteIP) {
		return "", domain.Session{}, rateLimited()
	}
	acct, err := s.verifyCredentials(ctx, username, password)
	if err != nil {
		if errors.Is(err, domain.ErrUnauthorized) {
			s.throttle.Fail(ctx, key, remoteIP)
		}
		return "", domain.Session{}, err
	}
	s.throttle.Clear(ctx, key)

	raw, err := newSessionToken()
	if err != nil {
		return "", domain.Session{}, err
	}
	sess := domain.Session{
		ID:        domain.NewID(),
		Hash:      domain.HashToken(raw),
		AccountID: acct.ID,
		CreatedAt: now,
		ExpiresAt: now.Add(s.opts.SessionTTL),
	}
	if err := s.store.CreateSession(ctx, sess); err != nil {
		return "", domain.Session{}, err
	}
	if err := s.store.TouchLogin(ctx, acct.ID, now); err != nil {
		s.log.WarnContext(ctx, "touch login failed", slog.String("account_id", acct.ID), slog.Any("err", err))
	}
	s.maybePurge(ctx, now)
	return raw, sess, nil
}

// verifyCredentials 无论账号是否存在都做一次 argon2id 校验，抹平耗时差。
func (s *Service) verifyCredentials(ctx context.Context, username, password string) (domain.Account, error) {
	name, nameErr := normalizeUsername(username)
	var (
		acct domain.Account
		err  error
	)
	if nameErr == nil {
		acct, err = s.store.AccountByUsername(ctx, name)
	}
	missing := nameErr != nil || errors.Is(err, domain.ErrNotFound)
	if !missing && err != nil {
		return domain.Account{}, err
	}
	hash := acct.PasswordHash
	// 空哈希来自零信任自动建的账号：它们永远不能口令登录，但也要跑一次
	// dummy 校验，避免用响应时延区分出“这个用户名是外部账号”。
	noPassword := !missing && hash == ""
	if missing || noPassword {
		hash = s.dummyHash
	}
	match, cmpErr := argon2id.ComparePasswordAndHash(password, hash)
	hasNoUsablePassword := missing || noPassword
	passwordMismatch := cmpErr != nil || !match
	if hasNoUsablePassword || passwordMismatch || acct.Disabled {
		return domain.Account{}, loginFailed()
	}
	return acct, nil
}

func throttleKey(username string) string {
	name, err := normalizeUsername(username)
	if err != nil {
		name = username
	}
	return domain.TruncateRunes(name, maxThrottleKeyLen)
}

func (s *Service) maybePurge(ctx context.Context, now time.Time) {
	s.mu.Lock()
	due := now.Sub(s.lastPurge) >= purgeInterval
	if due {
		s.lastPurge = now
	}
	s.mu.Unlock()
	if !due {
		return
	}
	// 清理是维护动作，不能把一个可能很长的 DELETE 挂在某个用户的登录请求上。
	go func() {
		purgeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer cancel()
		if _, err := s.store.PurgeSessionsBefore(purgeCtx, now.Add(-purgeGrace)); err != nil {
			s.log.WarnContext(purgeCtx, "purge sessions failed", slog.Any("err", err))
		}
	}()
}

// Logout 吊销会话；会话不存在或已吊销也算成功。返回被登出的账号用户名；会话不存在、已吊销或已过期时
// 为空串，调用方据此决定是否写审计，伪造或重放的 cookie 不会产生审计记录。
func (s *Service) Logout(ctx context.Context, rawSession string) (string, error) {
	defer s.invalidatePrincipalCache()
	if rawSession == "" {
		return "", nil
	}
	sess, err := s.store.SessionByHash(ctx, domain.HashToken(rawSession))
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) {
			return "", nil
		}
		return "", err
	}
	s.mu.Lock()
	delete(s.touched, sess.ID)
	s.mu.Unlock()
	now := s.now()
	live := sess.RevokedAt == nil && now.Before(sess.ExpiresAt)
	if err := s.store.RevokeSession(ctx, sess.ID, now); err != nil {
		return "", err
	}
	if !live {
		return "", nil
	}
	acct, err := s.store.GetAccount(ctx, sess.AccountID)
	if err != nil {
		s.log.WarnContext(ctx, "logout account lookup failed", slog.Any("err", err))
		return "", nil
	}
	return acct.Username, nil
}
