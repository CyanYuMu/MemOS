// Package rediscache 用集群 Redis 保存跨进程共享的登录失败计数。
package rediscache

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	userKeyPrefix = "u:"
	ipKeyPrefix   = "i:"

	// incrWithTTL 只在第一次写入时设置过期，保证窗口从第一次失败开始计。
	incrWithTTL = `
local n = redis.call("INCR", KEYS[1])
if n == 1 then redis.call("PEXPIRE", KEYS[1], ARGV[1]) end
return n`
)

// Client 是节流需要的 Redis 命令子集；抽出接口是为了用假实现测逻辑。
type Client interface {
	IncrWithTTL(ctx context.Context, key string, ttl time.Duration) (int64, error)
	GetMany(ctx context.Context, keys ...string) ([]string, error)
	Delete(ctx context.Context, keys ...string) (int64, error)
	ScanKeys(ctx context.Context, match string) ([]string, error)
	Ping(ctx context.Context) error
	Close() error
}

// Throttle 用 Redis 的固定窗口计数实现 throttle.Backend。
type Throttle struct {
	client Client
	limit  int
	window time.Duration
	prefix string
}

// NewThrottle 构造 Redis 节流；prefix 用于与同一 Redis 上的其它业务隔离。
func NewThrottle(client Client, limit int, window time.Duration, prefix string) *Throttle {
	return &Throttle{client: client, limit: limit, window: window, prefix: prefix}
}

func (t *Throttle) Blocked(ctx context.Context, user, ip string) (bool, error) {
	keys := []string{t.key(userKeyPrefix, user)}
	if ip != "" {
		keys = append(keys, t.key(ipKeyPrefix, ip))
	}
	values, err := t.client.GetMany(ctx, keys...)
	if err != nil {
		return false, err
	}
	for _, v := range values {
		if v == "" {
			continue
		}
		n, convErr := strconv.Atoi(v)
		if convErr != nil {
			continue
		}
		if n >= t.limit {
			return true, nil
		}
	}
	return false, nil
}

func (t *Throttle) Fail(ctx context.Context, user, ip string) error {
	if _, err := t.client.IncrWithTTL(ctx, t.key(userKeyPrefix, user), t.window); err != nil {
		return err
	}
	if ip == "" {
		return nil
	}
	_, err := t.client.IncrWithTTL(ctx, t.key(ipKeyPrefix, ip), t.window)
	return err
}

func (t *Throttle) Clear(ctx context.Context, user string) error {
	_, err := t.client.Delete(ctx, t.key(userKeyPrefix, user))
	return err
}

// Reset 删除整个 prefix 下的键。只在管理员重置口令等低频操作时调用。
func (t *Throttle) Reset(ctx context.Context) error {
	keys, err := t.client.ScanKeys(ctx, t.prefix+"*")
	if err != nil {
		return err
	}
	for start := 0; start < len(keys); start += resetBatch {
		end := min(start+resetBatch, len(keys))
		if _, err := t.client.Delete(ctx, keys[start:end]...); err != nil {
			return err
		}
	}
	return nil
}

// resetBatch 控制一次 DEL 的键数量，避免单条命令过长。
const resetBatch = 500

// key 只把 hash 写进 Redis，用户名与 IP 不进 key 明文。
func (t *Throttle) key(kind, value string) string {
	sum := sha256.Sum256([]byte(value))
	return t.prefix + kind + hex.EncodeToString(sum[:])[:32]
}

// Conn 是 REDIS 连接的实现；同时满足 Client 与 close 语义。
type Conn struct {
	rdb *redis.Client
}

// Open 解析 redis:// 或 rediss:// URL；timeout 作用于拨号与读写。
func Open(rawURL string, timeout time.Duration) (*Conn, error) {
	opts, err := redis.ParseURL(rawURL)
	if err != nil {
		return nil, fmt.Errorf("redis: parse url: %w", err)
	}
	if timeout > 0 {
		opts.DialTimeout = timeout
		opts.ReadTimeout = timeout
		opts.WriteTimeout = timeout
	}
	return &Conn{rdb: redis.NewClient(opts)}, nil
}

func (c *Conn) IncrWithTTL(ctx context.Context, key string, ttl time.Duration) (int64, error) {
	return redis.NewScript(incrWithTTL).Run(ctx, c.rdb, []string{key}, ttl.Milliseconds()).Int64()
}

func (c *Conn) GetMany(ctx context.Context, keys ...string) ([]string, error) {
	raw, err := c.rdb.MGet(ctx, keys...).Result()
	if err != nil {
		return nil, err
	}
	out := make([]string, len(raw))
	for i, v := range raw {
		if s, ok := v.(string); ok {
			out[i] = s
		}
	}
	return out, nil
}

func (c *Conn) Delete(ctx context.Context, keys ...string) (int64, error) {
	return c.rdb.Del(ctx, keys...).Result()
}

func (c *Conn) ScanKeys(ctx context.Context, match string) ([]string, error) {
	var (
		cursor uint64
		keys   []string
	)
	for {
		batch, next, err := c.rdb.Scan(ctx, cursor, match, resetBatch).Result()
		if err != nil {
			return nil, err
		}
		keys = append(keys, batch...)
		cursor = next
		if cursor == 0 {
			return keys, nil
		}
	}
}

func (c *Conn) Ping(ctx context.Context) error { return c.rdb.Ping(ctx).Err() }

func (c *Conn) Close() error { return c.rdb.Close() }
