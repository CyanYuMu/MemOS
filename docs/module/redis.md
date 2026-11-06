---
module: redis
packages:
  - internal/adapter/rediscache
  - internal/platform/throttle
status: implemented
---

# redis

登录失败的节流计数可以放在集群已有的 Redis 里，让多个进程/副本共享同一份计数；未配置 Redis 时仍用进程内存，行为与之前一致。会话与异步任务**不**搬过去：它们需要事务与持久化，SQLite 仍是唯一真相。

自带网关（dumemos）用 Redis 做调度器，本仓库只把它当作一个可选的共享计数器，不复用对方的 key 空间。

## 职责与边界

- 负责：登录/改密失败的固定窗口计数（按用户名与来源 IP 各一份），Redis 与进程内存两种实现，以及 Redis 出错时按单次调用回退到内存。
- 不负责：会话存储（`store` 的 `sessions` 表）、异步任务队列（`worker` + `jobs` 表）、审计落库。这三者的多副本前提是换 PG / 换 MQ，不在本模块范围内。

## 依赖

- `internal/platform/throttle`：`Throttler` 端口、`Memory` 实现、`Fallback` 组合；只依赖标准库与 slog。
- `internal/adapter/rediscache`：`Throttle` 后端与 `Open` 连接，依赖 `github.com/redis/go-redis/v9`；不 import service（分层规则）。
- 使用方：`internal/service/account` 通过 `account.Throttler`（`throttle.Throttler` 的类型别名）调用；`internal/app` 负责装配与关闭连接。

## 契约

```go
// internal/platform/throttle
type Throttler interface {
    Blocked(ctx context.Context, user, ip string) bool
    Fail(ctx context.Context, user, ip string)
    Clear(ctx context.Context, user string)
    Reset(ctx context.Context)
}

// Backend 是可能失败的后端；Fallback 出错时回退到 Memory。
type Backend interface {
    Blocked(ctx context.Context, user, ip string) (bool, error)
    Fail(ctx context.Context, user, ip string) error
    Clear(ctx context.Context, user string) error
    Reset(ctx context.Context) error
}

func NewMemory(limit int, window time.Duration) *Memory
func NewFallback(primary Backend, backup Throttler, log *slog.Logger) *Fallback

// internal/adapter/rediscache
type Client interface {
    IncrWithTTL(ctx context.Context, key string, ttl time.Duration) (int64, error)
    GetMany(ctx context.Context, keys ...string) ([]string, error)
    Delete(ctx context.Context, keys ...string) (int64, error)
    ScanKeys(ctx context.Context, match string) ([]string, error)
    Ping(ctx context.Context) error
    Close() error
}

func Open(rawURL string, timeout time.Duration) (*Conn, error)
func NewThrottle(client Client, limit int, window time.Duration, prefix string) *Throttle
```

- 窗口：从第一次失败开始计 `window`。Redis 用 `INCR` + 首次 `PEXPIRE`（Lua 脚本保证原子），内存实现用同样的固定窗口语义。
- 键：`<prefix>u:<sha256(key) 前 32 个 hex>` 与 `<prefix>i:<sha256(ip) 前 32 个 hex>`；用户名与 IP 不进 key 明文。默认 prefix `memosd:`，与调度器的 `memscheduler:*` 隔离。
- 触发：`count >= max` 即拒绝。用户名与 IP 任一超限都拒绝；`ip` 为空时只看用户名。
- `Clear(user)` 只清用户名计数（登录成功后）；`Reset()` 清空整个 prefix（管理员重置口令时，同一 IP 的计数也要清）。
- 配置（`internal/config` 的 `RedisConfig`）：`MEMOSD_REDIS_URL`（空 = 只用内存）、`MEMOSD_REDIS_PREFIX`（默认 `memosd:`）、`MEMOSD_REDIS_TIMEOUT`（默认 500ms，作用于拨号/读写）。
- 启动：配置了 URL 时先 `PING`；失败只记 WARN，不阻断启动（节流不是主路径）。
- 不新增 readiness 项：Redis 不可用时按下面的降级语义工作，`/readyz` 仍只反映存储。

## 错误与边界情况

- Redis 命令失败：`Fallback` 记一条 WARN（同一进程 10 秒内只记一条，避免攻击流量刷屏），本次调用改用内存计数。登录不会被拒，也不会因为 Redis 故障而放开到下个进程才生效。
- 内存计数随进程重启丢失，Redis 计数按 TTL 自然过期；两者都不持久化，审计不依赖它们。
- `Reset` 用 `SCAN MATCH <prefix>* COUNT 500` 分批删除，只在管理员重置口令等低频操作时调用。
- key 超过 4096 个：内存实现丢弃最旧条目；Redis 靠 TTL 收敛，不做额外清理。
- Redis 与内存的计数不合并：Redis 恢复后以 Redis 为准，故障期间写入内存的计数保留到各自过期。

## 测试与验证

- `internal/platform/throttle`：固定窗口内的计数与超限；窗口过期后重新计数；`Clear` 只清用户名；`Reset` 清空；`Fallback` 在后端报错时回退且不改变主后端语义。
- `internal/adapter/rediscache`：假 `Client` 上验证 key 派生（不含明文）、`Blocked`/`Fail`/`Clear`/`Reset` 的命令与参数、错误透传。
- `internal/config`：默认值、URL 校验（scheme/host）、非法值报错；脱敏摘要不打印 Redis URL。
- 装配：只有配置了 URL 时才在 `internal/app` 建 Redis 后端；未配置时不传选项，内存节流仍由 `account.New` 用自己的默认值创建，避免两边各定义一套默认值（传零值会把窗口设成 0，节流永久失效，e2e 的 `TestE2ELoginThrottle` 覆盖这一点）。

## 参考来源

- `docs/module/accounts.md`：登录、会话与节流在业务上的口径。
- `docs/module/config.md`：环境变量清单与校验方式。
- `deploy/dev/`、`deploy/prod/`：Redis URL 的部署位置。

## 实现记录

See the public version-control history.
