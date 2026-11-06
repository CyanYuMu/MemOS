package jobs

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type fakeStore struct {
	jobs     map[string]domain.Job
	requeued []string
	at       time.Time
}

func newFakeStore(seed ...domain.Job) *fakeStore {
	f := &fakeStore{jobs: map[string]domain.Job{}}
	for i := range seed {
		f.jobs[seed[i].ID] = seed[i]
	}
	return f
}

func (f *fakeStore) ListJobs(_ context.Context, filter domain.JobFilter) ([]domain.Job, int, error) {
	var out []domain.Job
	for id := range f.jobs {
		if filter.Status == "" || f.jobs[id].Status == filter.Status {
			out = append(out, f.jobs[id])
		}
	}
	return out, len(out), nil
}

func (f *fakeStore) GetJob(_ context.Context, id string) (domain.Job, error) {
	job, ok := f.jobs[id]
	if !ok {
		return domain.Job{}, domain.NotFound("get_job", "任务不存在")
	}
	return job, nil
}

// RequeueJob 与真实 store 一致：只接受 failed，成功后清掉 attempts 与 last_error。
func (f *fakeStore) RequeueJob(_ context.Context, id string, now time.Time) (domain.Job, error) {
	job, ok := f.jobs[id]
	if !ok {
		return domain.Job{}, domain.NotFound("requeue_job", "任务不存在")
	}
	if job.Status != domain.JobStatusFailed {
		return domain.Job{}, domain.Wrap(domain.ErrConflict, "requeue_job", "只有失败的任务可以重放", nil)
	}
	job.Status, job.Attempts, job.LastError, job.NextAttemptAt = domain.JobStatusPending, 0, "", now
	f.jobs[id] = job
	f.requeued = append(f.requeued, id)
	f.at = now
	return job, nil
}

func failed(id, lastErr string) domain.Job {
	return domain.Job{ID: id, Status: domain.JobStatusFailed, Attempts: 3, LastError: lastErr}
}

func TestList(t *testing.T) {
	t.Parallel()
	svc := New(newFakeStore(failed("a", "invalid input"), domain.Job{ID: "b", Status: domain.JobStatusPending}))

	got, total, err := svc.List(t.Context(), domain.JobFilter{Status: domain.JobStatusFailed})
	if err != nil || total != 1 || len(got) != 1 || got[0].ID != "a" {
		t.Fatalf("got=%v total=%d err=%v", got, total, err)
	}
	if _, _, err := svc.List(t.Context(), domain.JobFilter{}); err != nil {
		t.Fatalf("空状态表示不过滤: %v", err)
	}
	if _, _, err := svc.List(t.Context(), domain.JobFilter{Status: "bogus"}); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("未知状态应是参数错误，got %v", err)
	}
}

func TestRetry(t *testing.T) {
	t.Parallel()
	pending := domain.Job{ID: "pending", Status: domain.JobStatusPending}
	cases := []struct {
		name    string
		id      string
		force   bool
		want    error // nil 表示重放成功
		requeue bool
	}{
		{"缺 id", "", false, domain.ErrInvalidInput, false},
		{"安全的失败类型直接重放", "safe", false, nil, true},
		{"可能已写入上游的失败需要 force", "timeout", false, domain.ErrConflict, false},
		{"空 last_error 按不安全处理", "blank", false, domain.ErrConflict, false},
		{"未知错误类型按不安全处理", "weird", false, domain.ErrConflict, false},
		{"force 绕过安全判定", "timeout", true, nil, true},
		{"非 failed 的任务由存储拒绝", "pending", false, domain.ErrConflict, false},
		{"force 也不能重放非 failed", "pending", true, domain.ErrConflict, false},
		{"不存在", "missing", false, domain.ErrNotFound, false},
		{"force 时不存在同样是 404", "missing", true, domain.ErrNotFound, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			store := newFakeStore(
				failed("safe", domain.ErrInvalidInput.Error()),
				failed("timeout", domain.ErrUpstreamUnavailable.Error()),
				failed("blank", ""),
				failed("weird", "something else"),
				pending,
			)
			svc := New(store)
			fixed := time.Date(2026, 10, 26, 12, 0, 0, 0, time.UTC)
			svc.now = func() time.Time { return fixed }

			job, err := svc.Retry(t.Context(), tc.id, tc.force)
			if tc.want != nil {
				if !errors.Is(err, tc.want) {
					t.Fatalf("err = %v，期望 %v", err, tc.want)
				}
			} else if err != nil {
				t.Fatalf("Retry: %v", err)
			}
			if got := len(store.requeued) == 1; got != tc.requeue {
				t.Fatalf("requeued=%v，期望 %v", store.requeued, tc.requeue)
			}
			if tc.requeue {
				if job.Status != domain.JobStatusPending || job.Attempts != 0 || job.LastError != "" {
					t.Fatalf("重放结果 = %+v", job)
				}
				if !store.at.Equal(fixed) {
					t.Fatalf("now 没有传给存储: %v", store.at)
				}
			}
		})
	}
}

func TestRetryConflictMessageNamesTheFailure(t *testing.T) {
	t.Parallel()
	svc := New(newFakeStore(failed("timeout", domain.ErrUpstreamUnavailable.Error())))
	_, err := svc.Retry(t.Context(), "timeout", false)
	if msg := domain.SafeMessage(err); !strings.Contains(msg, domain.ErrUpstreamUnavailable.Error()) || !strings.Contains(msg, "force=true") {
		t.Fatalf("冲突消息应点出失败类型并提示 force: %q", msg)
	}
}
