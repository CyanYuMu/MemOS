package throttle

import (
	"context"
	"log/slog"
	"sync/atomic"
	"time"
)

// fallbackWarnInterval 限制降级告警的密度：Redis 故障时攻击流量会持续触发。
const fallbackWarnInterval = 10 * time.Second

// Fallback 优先用 primary；任一调用出错就记一条（限频）WARN 并改用 backup。
// 这样 Redis 故障时登录不会被拒，节流在单进程内继续生效。
type Fallback struct {
	primary  Backend
	backup   Throttler
	log      *slog.Logger
	lastWarn atomic.Int64
}

// NewFallback 组合两个后端；log 为 nil 时用 slog.Default。
func NewFallback(primary Backend, backup Throttler, log *slog.Logger) *Fallback {
	if log == nil {
		log = slog.Default()
	}
	return &Fallback{primary: primary, backup: backup, log: log}
}

func (f *Fallback) Blocked(ctx context.Context, user, ip string) bool {
	if f.primary == nil {
		return f.backup.Blocked(ctx, user, ip)
	}
	blocked, err := f.primary.Blocked(ctx, user, ip)
	if err != nil {
		f.warn("blocked", err)
		return f.backup.Blocked(ctx, user, ip)
	}
	return blocked
}

func (f *Fallback) Fail(ctx context.Context, user, ip string) {
	if f.primary == nil {
		f.backup.Fail(ctx, user, ip)
		return
	}
	if err := f.primary.Fail(ctx, user, ip); err != nil {
		f.warn("fail", err)
		f.backup.Fail(ctx, user, ip)
	}
}

func (f *Fallback) Clear(ctx context.Context, user string) {
	if f.primary == nil {
		f.backup.Clear(ctx, user)
		return
	}
	if err := f.primary.Clear(ctx, user); err != nil {
		f.warn("clear", err)
		f.backup.Clear(ctx, user)
	}
}

func (f *Fallback) Reset(ctx context.Context) {
	if f.primary == nil {
		f.backup.Reset(ctx)
		return
	}
	if err := f.primary.Reset(ctx); err != nil {
		f.warn("reset", err)
		f.backup.Reset(ctx)
	}
}

func (f *Fallback) warn(op string, err error) {
	now := time.Now().UnixNano()
	last := f.lastWarn.Load()
	if now-last < int64(fallbackWarnInterval) || !f.lastWarn.CompareAndSwap(last, now) {
		return
	}
	f.log.Warn("throttle backend failed; using memory counts", slog.String("op", op), slog.Any("err", err))
}
