// Package throttle 提供登录失败节流：进程内实现，以及 Redis 后端出错时
// 回退到进程内的组合实现。
package throttle

import "context"

// Throttler 是账号服务使用的节流端口。
type Throttler interface {
	// Blocked 报告该用户名或来源 IP 是否已经超过失败上限。
	Blocked(ctx context.Context, user, ip string) bool
	// Fail 记一次失败。
	Fail(ctx context.Context, user, ip string)
	// Clear 清掉某个用户名的失败计数；登录成功后调用。
	Clear(ctx context.Context, user string)
	// Reset 清空全部计数；管理员重置口令时调用。
	Reset(ctx context.Context)
}

// Backend 是可能失败的后端（如 Redis）。错误由调用方决定如何降级。
type Backend interface {
	Blocked(ctx context.Context, user, ip string) (bool, error)
	Fail(ctx context.Context, user, ip string) error
	Clear(ctx context.Context, user string) error
	Reset(ctx context.Context) error
}
