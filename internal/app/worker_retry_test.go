package app

import (
	"context"
	"log/slog"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/adapter/store"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/worker"
)

// 用真实 SQLite 验证 worker 的退避重试：失败任务复用同一行、
// 到期前不可认领，尝试次数用尽后落 failed 且不再被认领。
// 放在 app 包里是因为只有组合根能同时 import adapter 与 worker（depguard）。
func TestWorkerBackoffAgainstStore(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	s, err := store.Open(ctx, filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("store.Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })

	now := time.Now()
	if err := s.EnqueueJob(ctx, domain.Job{
		ID: "job-1", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a",
		Payload: []byte(`{}`), CreatedAt: now, NextAttemptAt: now,
	}); err != nil {
		t.Fatalf("EnqueueJob: %v", err)
	}

	runner := &failingRunner{err: domain.Wrap(domain.ErrUpstreamUnavailable, "add", "上游不可用", nil)}
	svc := worker.New(s, runner, slog.Default(), worker.Options{
		PollInterval: 2 * time.Millisecond,
		MaxAttempts:  3,
		Concurrency:  1,
		RetryBase:    20 * time.Millisecond,
		RetryMax:     200 * time.Millisecond,
	})

	began := time.Now()
	done := make(chan struct{})
	go func() {
		_ = svc.Run(ctx)
		close(done)
	}()
	deadline := time.After(4 * time.Second)
	for runner.count() < 3 {
		select {
		case <-deadline:
			t.Fatalf("三次尝试未完成，calls=%d", runner.count())
		case <-time.After(time.Millisecond):
		}
	}
	cancel()
	<-done

	if elapsed := time.Since(began); elapsed < 55*time.Millisecond {
		t.Fatalf("三次尝试只用了 %s，说明没有真正退避", elapsed)
	}
	if job, ok, err := s.ClaimJob(context.Background(), time.Now().Add(time.Hour)); err != nil || ok {
		t.Fatalf("永久失败后不应再被认领: %+v, %v, %v", job, ok, err)
	}
	pending, running, failed, _, err := s.JobStats(context.Background())
	if err != nil {
		t.Fatalf("JobStats: %v", err)
	}
	if pending != 0 || running != 0 || failed != 1 {
		t.Fatalf("pending/running/failed = %d/%d/%d want 0/0/1", pending, running, failed)
	}
}

type failingRunner struct {
	mu    sync.Mutex
	calls int
	err   error
}

func (r *failingRunner) RunAddJob(context.Context, []byte) (domain.AddResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls++
	return domain.AddResult{}, r.err
}

func (r *failingRunner) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.calls
}
