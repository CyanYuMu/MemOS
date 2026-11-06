package throttle

import (
	"strconv"
	"testing"
	"time"
)

// 令牌桶用可控时钟测，不靠 sleep：突发、补充速率与等待时间都要能精确断言。
func TestBucketsBurstAndRefill(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }
	b := NewBucketsAt(2, 60, clock) // 突发 2，每秒补 1

	if ok, _ := b.Allow("cube-a"); !ok {
		t.Fatal("突发额度内应放行")
	}
	if ok, _ := b.Allow("cube-a"); !ok {
		t.Fatal("突发额度内应放行")
	}
	ok, wait := b.Allow("cube-a")
	if ok {
		t.Fatal("超出突发额度应拒绝")
	}
	if wait != time.Second {
		t.Fatalf("等待时间 = %v，期望 1s", wait)
	}

	// 过 1 秒补一个令牌；仍然只有一个。
	now = now.Add(time.Second)
	if ok, _ := b.Allow("cube-a"); !ok {
		t.Fatal("补充一个令牌后应放行")
	}
	if ok, _ := b.Allow("cube-a"); ok {
		t.Fatal("只补一个令牌，紧接着的请求应拒绝")
	}
}

func TestBucketsRefillCap(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }
	b := NewBucketsAt(2, 600, clock) // 每秒 10 个

	// 耗尽后闲置很久，也不能攒出超过突发的令牌。
	for range 2 {
		b.Allow("k")
	}
	now = now.Add(time.Hour)
	for i := range 2 {
		if ok, _ := b.Allow("k"); !ok {
			t.Fatalf("第 %d 个令牌应放行", i+1)
		}
	}
	if ok, _ := b.Allow("k"); ok {
		t.Fatal("补充不能超过突发上限")
	}
}

func TestBucketsArePerKey(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	b := NewBucketsAt(1, 1, func() time.Time { return now })

	if ok, _ := b.Allow("cube-a"); !ok {
		t.Fatal("cube-a 首个请求应放行")
	}
	if ok, _ := b.Allow("cube-a"); ok {
		t.Fatal("cube-a 应被限流")
	}
	if ok, _ := b.Allow("cube-b"); !ok {
		t.Fatal("另一个键不受影响")
	}
}

func TestBucketsZeroRefillStillDenies(t *testing.T) {
	t.Parallel()

	b := NewBuckets(0, 0)
	if ok, wait := b.Allow("k"); ok || wait <= 0 {
		t.Fatalf("零额度应拒绝并给出等待时间: ok=%v wait=%v", ok, wait)
	}
}

func TestGaugeLimitsInFlightPerKey(t *testing.T) {
	t.Parallel()

	g := NewGauge(1)
	release, ok := g.Acquire("cube-a")
	if !ok {
		t.Fatal("首个请求应放行")
	}
	if _, ok := g.Acquire("cube-a"); ok {
		t.Fatal("同一项目只允许一个在途")
	}
	if _, ok := g.Acquire("cube-b"); !ok {
		t.Fatal("不同项目互不影响")
	}

	// release 可重复调用，不能把别人的名额也放掉。
	release()
	release()
	if _, ok := g.Acquire("cube-a"); !ok {
		t.Fatal("释放后应能再次获取")
	}
}

// Take 同时扣两个桶：都够才扣，任一不够时另一个一个令牌都不能少。
func TestTakeDeductsBothOrNeither(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }
	project := NewBucketsAt(2, 1, clock)
	account := NewBucketsAt(2, 1, clock)

	if ok, _ := Take(project, account, "cube-a", "alice", 1); !ok {
		t.Fatal("两个桶都有令牌时应放行")
	}
	// 把账号桶耗尽，项目桶还剩 1 个。
	if ok, _ := account.Allow("alice"); !ok {
		t.Fatal("账号桶第二个令牌应放行")
	}
	if ok, wait := Take(project, account, "cube-a", "alice", 1); ok || wait <= 0 {
		t.Fatalf("账号桶不足应拒绝并给出等待时间: ok=%v wait=%v", ok, wait)
	}
	// 项目桶的那 1 个令牌必须还在：换个账号还能取到。
	if ok, _ := Take(project, account, "cube-a", "bob", 1); !ok {
		t.Fatal("被拒绝的请求不能扣掉项目桶的令牌")
	}
	if ok, _ := Take(project, account, "cube-a", "carol", 1); ok {
		t.Fatal("项目桶应已耗尽")
	}
}

func TestTakesCostAndClampsToBurst(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	clock := func() time.Time { return now }
	project := NewBucketsAt(3, 1, clock)
	account := NewBucketsAt(3, 1, clock)

	if ok, _ := Take(project, account, "cube-a", "alice", 2); !ok {
		t.Fatal("突发 3 时扣 2 应放行")
	}
	if ok, _ := Take(project, account, "cube-a", "alice", 2); ok {
		t.Fatal("只剩 1 个令牌时扣 2 应拒绝")
	}
	if ok, _ := Take(project, account, "cube-a", "alice", 1); !ok {
		t.Fatal("剩 1 个令牌时扣 1 应放行")
	}

	// 单次请求要的令牌数超过桶容量时按容量算：否则 compareDefault 在突发 1 时永远跑不了。
	tiny := NewBucketsAt(1, 1, clock)
	tinyAccount := NewBucketsAt(1, 1, clock)
	if ok, _ := Take(tiny, tinyAccount, "cube-a", "alice", 2); !ok {
		t.Fatal("cost 超过容量时应按容量扣")
	}
}

// 打满 4096 个键时，淘汰必须优先挑“已回满”的桶：重建它们的状态与原桶一样，
// 没有损失。若按最久未用淘汰，攻击者用大量不同的 projectId 就能把别人已经
// 耗尽的桶挤掉并重置，等于绕过限流。
func TestEvictionKeepsDrainedBuckets(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	// 突发 2、每秒补 1：victim 要 2 秒回满，随后的 filler 只要 1 秒，
	// 这样能造出“victim 未回满、filler 已回满”的中间状态。
	b := NewBucketsAt(2, 60, func() time.Time { return now })

	for range 2 {
		if ok, _ := b.Allow("victim"); !ok {
			t.Fatal("victim 的突发额度应放行")
		}
	}
	now = now.Add(500 * time.Millisecond)
	for i := range maxBucketKeys - 1 {
		b.Allow("filler-" + strconv.Itoa(i))
	}
	now = now.Add(time.Second) // filler 回满（2），victim 只有 1.5
	b.Allow("filler-overflow") // 触发一次淘汰

	// victim 若被淘汰重建，会回到满额 2：两次 Allow 都会成功。
	if ok, _ := b.Allow("victim"); !ok {
		t.Fatal("victim 未被淘汰，剩余 1.5 个令牌，第一次应放行")
	}
	if ok, _ := b.Allow("victim"); ok {
		t.Fatal("已耗尽的桶被淘汰重建后会回到满额，攻击者可以借此重置别人的限流")
	}
}
