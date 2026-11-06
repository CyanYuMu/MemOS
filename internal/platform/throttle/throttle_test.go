package throttle

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestMemoryFixedWindow(t *testing.T) {
	t.Parallel()
	now := time.Date(2026, 10, 22, 8, 0, 0, 0, time.UTC)
	m := NewMemory(3, 10*time.Minute)
	m.now = func() time.Time { return now }
	ctx := context.Background()

	for range 3 {
		m.Fail(ctx, "alice", "10.0.0.1")
	}
	if !m.Blocked(ctx, "alice", "10.0.0.9") {
		t.Fatal("同一用户名换来源 IP 仍应被拦")
	}
	if !m.Blocked(ctx, "bob", "10.0.0.1") {
		t.Fatal("同一来源 IP 换用户名仍应被拦")
	}
	if m.Blocked(ctx, "bob", "10.0.0.2") {
		t.Fatal("用户名与来源都没超限的请求不应被拦")
	}

	m.Clear(ctx, "alice")
	if m.Blocked(ctx, "alice", "10.0.0.9") {
		t.Fatal("清零后用户名维度不应再拦")
	}
	if !m.Blocked(ctx, "carol", "10.0.0.1") {
		t.Fatal("清零只作用于用户名，来源 IP 的计数应保留")
	}
	now = now.Add(11 * time.Minute)
	if m.Blocked(ctx, "carol", "10.0.0.1") {
		t.Fatal("窗口过后应解除限制")
	}
}

func TestMemoryResetClearsBothDimensions(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	m := NewMemory(1, time.Hour)
	m.Fail(ctx, "alice", "10.0.0.1")
	if !m.Blocked(ctx, "bob", "10.0.0.1") {
		t.Fatal("ip 计数应在")
	}
	m.Reset(ctx)
	if m.Blocked(ctx, "alice", "10.0.0.1") {
		t.Fatal("Reset 之后不应再拦")
	}
}

type errBackend struct{ err error }

func (b errBackend) Blocked(context.Context, string, string) (bool, error) { return false, b.err }
func (b errBackend) Fail(context.Context, string, string) error            { return b.err }
func (b errBackend) Clear(context.Context, string) error                   { return b.err }
func (b errBackend) Reset(context.Context) error                           { return b.err }

func TestFallbackUsesMemoryWhenBackendFails(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	backup := NewMemory(2, time.Minute)
	f := NewFallback(errBackend{err: errors.New("redis down")}, backup, nil)

	f.Fail(ctx, "alice", "10.0.0.1")
	f.Fail(ctx, "alice", "10.0.0.1")
	if !f.Blocked(ctx, "alice", "10.0.0.1") {
		t.Fatal("后端失败时应回退到内存计数")
	}
	f.Reset(ctx)
	if f.Blocked(ctx, "alice", "10.0.0.1") {
		t.Fatal("Reset 也应走回退")
	}
}

type okBackend struct{ blocked bool }

func (b okBackend) Blocked(context.Context, string, string) (bool, error) { return b.blocked, nil }
func (b okBackend) Fail(context.Context, string, string) error            { return nil }
func (b okBackend) Clear(context.Context, string) error                   { return nil }
func (b okBackend) Reset(context.Context) error                           { return nil }

func TestFallbackPrefersBackend(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	memory := NewMemory(1, time.Minute)
	memory.Fail(ctx, "alice", "10.0.0.1")
	f := NewFallback(okBackend{blocked: false}, memory, nil)

	if f.Blocked(ctx, "alice", "10.0.0.1") {
		t.Fatal("后端可用时不应看内存计数")
	}
}
