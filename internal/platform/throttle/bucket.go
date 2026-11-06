package throttle

import (
	"math"
	"sync"
	"time"
)

// maxBucketKeys 限制进程内桶的数量：提示词工作台按项目与账号分桶，
// 账号数量由控制台成员数决定，正常远小于此值；超限时淘汰最久未用的桶。
const maxBucketKeys = 4096

// Buckets 是按 key 分桶的令牌桶。用于提示词试跑与 AI 改写：
// 生产是单副本，进程内计数即可（多副本时限额会按副本数放大，见 prompt-studio.md）。
type Buckets struct {
	mu      sync.Mutex
	burst   float64
	refill  float64 // 每秒补充的令牌数
	now     func() time.Time
	entries map[string]*bucket
}

type bucket struct {
	tokens float64
	last   time.Time
}

// NewBuckets 构造令牌桶：burst 是突发额度，refillPerMinute 是每分钟补充数。
// 两组都为 0 时拒绝一切请求，避免“没配就是不限流”的静默放行。
func NewBuckets(burst, refillPerMinute int) *Buckets {
	return newBucketsAt(float64(burst), float64(refillPerMinute)/60, time.Now)
}

func newBucketsAt(burst, refillPerSecond float64, now func() time.Time) *Buckets {
	return &Buckets{
		burst:   burst,
		refill:  refillPerSecond,
		now:     now,
		entries: make(map[string]*bucket),
	}
}

// Allow 取一个令牌。失败时返回补出下一个令牌需要的等待时间（至少 1ms）。
func (b *Buckets) Allow(key string) (bool, time.Duration) {
	b.mu.Lock()
	defer b.mu.Unlock()

	entry := b.refillLocked(key, b.now())
	return b.takeLocked(entry, 1)
}

// Take 从两个桶各取 cost 个令牌：两个桶都够才扣，任一不够时一个都不扣。
//
// 项目桶与账号桶是两道独立的闸门，先扣项目再发现账号不足会把项目额度白吃掉，
// 让同项目的其他人无辜被限流；并发下“先 peek 后 take”又会超发，所以两把锁
// 一起持有、一次判断一次扣减。调用方始终按 (项目, 账号) 的顺序传，避免死锁。
func Take(first, second *Buckets, firstKey, secondKey string, cost int) (bool, time.Duration) {
	if cost < 1 {
		cost = 1
	}
	if first == second {
		ok, wait := first.Allow(firstKey)
		return ok, wait
	}
	first.mu.Lock()
	defer first.mu.Unlock()
	second.mu.Lock()
	defer second.mu.Unlock()

	now := first.now()
	a := first.refillLocked(firstKey, now)
	b := second.refillLocked(secondKey, now)
	// 单次请求不能要求比桶容量还多的令牌，否则 compareDefault 在突发 1 时永远跑不了。
	want := math.Min(float64(cost), math.Min(first.burst, second.burst))

	if a.tokens >= want && b.tokens >= want {
		a.tokens -= want
		b.tokens -= want
		return true, 0
	}
	wait := max(waitFor(a.tokens, want, first.refill), waitFor(b.tokens, want, second.refill))
	if wait <= 0 {
		wait = time.Millisecond
	}
	return false, wait
}

// refillLocked 取（必要时创建）某个键的桶并按经过的时间补充令牌；调用方须持锁。
func (b *Buckets) refillLocked(key string, now time.Time) *bucket {
	entry, ok := b.entries[key]
	if !ok {
		if len(b.entries) >= maxBucketKeys {
			b.evictLocked(now)
		}
		// 新桶直接满额：限额是“突发 N 次”而不是“启动后累计 N 次”。
		entry = &bucket{tokens: b.burst, last: now}
		b.entries[key] = entry
	}
	if elapsed := now.Sub(entry.last).Seconds(); elapsed > 0 {
		entry.tokens = math.Min(b.burst, entry.tokens+elapsed*b.refill)
		entry.last = now
	}
	return entry
}

// takeLocked 扣 cost 个令牌；调用方须持锁。
func (b *Buckets) takeLocked(entry *bucket, cost float64) (bool, time.Duration) {
	if entry.tokens >= cost {
		entry.tokens -= cost
		return true, 0
	}
	wait := waitFor(entry.tokens, cost, b.refill)
	if wait <= 0 {
		wait = time.Millisecond
	}
	return false, wait
}

// waitFor 返回补足到 want 个令牌需要等多久；补充速率为 0 时给 1 秒兜底。
func waitFor(tokens, want, refill float64) time.Duration {
	if refill <= 0 {
		return time.Second
	}
	return time.Duration(math.Ceil((want - tokens) / refill * float64(time.Second)))
}

// evictLocked 腾出一个键位。
//
// 优先淘汰“按当前时间已回满”的桶：重建后状态与原桶完全一样，丢掉没有损失。
// 只有全部桶都没回满时才退回最久未用。若一律按最久未用淘汰，攻击者拿大量
// 不同的 projectId 就能把别人已经耗尽的桶挤出去，桶被重建后回到满额，
// 等于重置了别人的限流。扫描是 O(n)，n 上限 4096，且只在桶数打满时发生。
func (b *Buckets) evictLocked(now time.Time) {
	var (
		fullKey   string
		fullLast  time.Time
		hasFull   bool
		oldestKey string
		oldest    time.Time
		hasOldest bool
	)
	for key, entry := range b.entries {
		if !hasOldest || entry.last.Before(oldest) {
			oldestKey, oldest, hasOldest = key, entry.last, true
		}
		refilled := entry.tokens + now.Sub(entry.last).Seconds()*b.refill
		if refilled >= b.burst && (!hasFull || entry.last.Before(fullLast)) {
			fullKey, fullLast, hasFull = key, entry.last, true
		}
	}
	switch {
	case hasFull:
		delete(b.entries, fullKey)
	case hasOldest:
		delete(b.entries, oldestKey)
	}
}

// Gauge 限制同一个 key 的同时在途数。
type Gauge struct {
	mu     sync.Mutex
	limit  int
	active map[string]int
}

// NewGauge 构造在途计数；limit <= 1 时按 1 处理。
func NewGauge(limit int) *Gauge {
	return &Gauge{limit: max(limit, 1), active: make(map[string]int)}
}

// Acquire 占一个名额。成功时返回的 release 可以重复调用。
func (g *Gauge) Acquire(key string) (func(), bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.active[key] >= g.limit {
		return nil, false
	}
	g.active[key]++
	var once sync.Once
	return func() {
		once.Do(func() {
			g.mu.Lock()
			defer g.mu.Unlock()
			if g.active[key] <= 1 {
				delete(g.active, key)
				return
			}
			g.active[key]--
		})
	}, true
}

// NewBucketsAt 与 NewBuckets 相同，但用调用方给的时钟；只用于让令牌桶测试
// 不必靠 sleep 等待补充。
func NewBucketsAt(burst, refillPerMinute int, now func() time.Time) *Buckets {
	if now == nil {
		now = time.Now
	}
	return newBucketsAt(float64(burst), float64(refillPerMinute)/60, now)
}
