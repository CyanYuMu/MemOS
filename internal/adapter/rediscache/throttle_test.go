package rediscache

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"testing"
	"time"
)

type fakeClient struct {
	counts map[string]int
	err    error
	ttl    time.Duration
	closed bool
}

func (f *fakeClient) IncrWithTTL(_ context.Context, key string, ttl time.Duration) (int64, error) {
	if f.err != nil {
		return 0, f.err
	}
	f.ttl = ttl
	f.counts[key]++
	return int64(f.counts[key]), nil
}

func (f *fakeClient) GetMany(_ context.Context, keys ...string) ([]string, error) {
	if f.err != nil {
		return nil, f.err
	}
	out := make([]string, len(keys))
	for i, k := range keys {
		out[i] = strconv.Itoa(f.counts[k])
		if f.counts[k] == 0 {
			out[i] = ""
		}
	}
	return out, nil
}

func (f *fakeClient) Delete(_ context.Context, keys ...string) (int64, error) {
	if f.err != nil {
		return 0, f.err
	}
	var n int64
	for _, k := range keys {
		if _, ok := f.counts[k]; ok {
			delete(f.counts, k)
			n++
		}
	}
	return n, nil
}

func (f *fakeClient) ScanKeys(_ context.Context, match string) ([]string, error) {
	if f.err != nil {
		return nil, f.err
	}
	prefix := strings.TrimSuffix(match, "*")
	var out []string
	for k := range f.counts {
		if strings.HasPrefix(k, prefix) {
			out = append(out, k)
		}
	}
	return out, nil
}

func (f *fakeClient) Ping(context.Context) error { return f.err }

func (f *fakeClient) Close() error {
	f.closed = true
	return nil
}

func TestThrottleSharesCountsAcrossCallers(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	c := &fakeClient{counts: map[string]int{}}
	th := NewThrottle(c, 2, 10*time.Minute, "memosd:")

	if blocked, err := th.Blocked(ctx, "alice", "10.0.0.1"); err != nil || blocked {
		t.Fatalf("初始状态 = %v, %v", blocked, err)
	}
	if err := th.Fail(ctx, "alice", "10.0.0.1"); err != nil {
		t.Fatalf("Fail: %v", err)
	}
	if err := th.Fail(ctx, "alice", "10.0.0.2"); err != nil {
		t.Fatalf("Fail: %v", err)
	}
	if c.ttl != 10*time.Minute {
		t.Fatalf("ttl = %v", c.ttl)
	}
	blocked, err := th.Blocked(ctx, "alice", "10.0.0.3")
	if err != nil || !blocked {
		t.Fatalf("同一用户换 IP 应被拦：%v, %v", blocked, err)
	}

	for key := range c.counts {
		if strings.Contains(key, "alice") || strings.Contains(key, "10.0.0") {
			t.Fatalf("key 不应包含明文：%s", key)
		}
		if !strings.HasPrefix(key, "memosd:") {
			t.Fatalf("key 缺少 prefix：%s", key)
		}
	}
}

func TestThrottleClearAndReset(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	c := &fakeClient{counts: map[string]int{}}
	th := NewThrottle(c, 1, time.Minute, "memosd:")
	_ = th.Fail(ctx, "alice", "10.0.0.1")
	_ = th.Fail(ctx, "bob", "10.0.0.2")

	if err := th.Clear(ctx, "alice"); err != nil {
		t.Fatalf("Clear: %v", err)
	}
	if blocked, _ := th.Blocked(ctx, "alice", ""); blocked {
		t.Fatal("Clear 后用户名不应再被拦")
	}
	if blocked, _ := th.Blocked(ctx, "bob", ""); !blocked {
		t.Fatal("Clear 不应影响其他用户")
	}

	if err := th.Reset(ctx); err != nil {
		t.Fatalf("Reset: %v", err)
	}
	if blocked, _ := th.Blocked(ctx, "bob", "10.0.0.2"); blocked {
		t.Fatal("Reset 后不应再拦")
	}
}

func TestThrottlePropagatesErrors(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	boom := errors.New("redis down")
	th := NewThrottle(&fakeClient{counts: map[string]int{}, err: boom}, 3, time.Minute, "memosd:")

	if _, err := th.Blocked(ctx, "alice", ""); !errors.Is(err, boom) {
		t.Fatalf("Blocked err = %v", err)
	}
	if err := th.Fail(ctx, "alice", ""); !errors.Is(err, boom) {
		t.Fatalf("Fail err = %v", err)
	}
	if err := th.Reset(ctx); !errors.Is(err, boom) {
		t.Fatalf("Reset err = %v", err)
	}
}

func TestOpenValidatesURL(t *testing.T) {
	t.Parallel()
	if _, err := Open("http://127.0.0.1:6379", time.Second); err == nil {
		t.Fatal("非 redis scheme 应报错")
	}
	conn, err := Open("redis://:pw@127.0.0.1:6379/0", time.Second)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if err := conn.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
}
