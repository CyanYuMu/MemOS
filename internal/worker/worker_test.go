package worker

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestMaybePurgeUsesRetentionAndRunsHourly(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 22, 0, 0, 0, 0, time.UTC)
	jobs := &fakeJobs{purged: 2}
	svc := New(jobs, &fakeRunner{}, slog.Default(), Options{RetentionDays: 7})
	svc.now = func() time.Time { return now }

	svc.maybePurge(context.Background())
	svc.maybePurge(context.Background())
	calls := jobs.purgeCalls()
	if len(calls) != 1 {
		t.Fatalf("purge calls=%d want 1 within an hour", len(calls))
	}
	if want := now.AddDate(0, 0, -7); !calls[0].Equal(want) {
		t.Fatalf("cutoff=%s want %s", calls[0], want)
	}

	now = now.Add(time.Hour)
	svc.maybePurge(context.Background())
	if len(jobs.purgeCalls()) != 2 {
		t.Fatal("purge should run again after an hour")
	}
}

func TestMaybePurgeMaintainsLocalSyncHourly(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 22, 0, 0, 0, 0, time.UTC)
	maintainer := &fakeMaintainer{}
	svc := New(&fakeJobs{}, &fakeRunner{}, slog.Default(), Options{
		RetentionDays: 7,
		LocalSync:     maintainer,
	})
	svc.now = func() time.Time { return now }

	svc.maybePurge(context.Background())
	svc.maybePurge(context.Background())
	if got := maintainer.calls(); got != 1 {
		t.Fatalf("maintain calls=%d want 1 within an hour", got)
	}

	now = now.Add(time.Hour)
	svc.maybePurge(context.Background())
	if got := maintainer.calls(); got != 2 {
		t.Fatalf("maintain calls=%d want 2 after an hour", got)
	}
}

type fakeMaintainer struct {
	mu    sync.Mutex
	count int
	err   error
}

func (f *fakeMaintainer) MaintainLocalSync(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.count++
	return f.err
}

func (f *fakeMaintainer) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.count
}

func TestRunSucceeds(t *testing.T) {
	jobs := &fakeJobs{pending: []domain.Job{{
		ID: "job-1", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a", UserID: "alice", Payload: []byte("{}"),
	}}}
	runner := &fakeRunner{}
	recorder := &fakeRecorder{}
	svc := New(jobs, runner, slog.Default(), Options{PollInterval: time.Millisecond, Audit: recorder})
	stop := runUntil(t, svc, func() bool { return jobs.finishedCount() == 1 })

	stop()
	if runner.calls() != 1 {
		t.Fatalf("runner calls=%d", runner.calls())
	}
	if got := jobs.lastStatus(); got != domain.JobStatusSucceeded {
		t.Fatalf("status=%s", got)
	}
	records := recorder.all()
	if len(records) != 1 || !records[0].OK || records[0].Tool != auditTool ||
		records[0].TenantID != "cube-a" || records[0].CallID != "job-1" {
		t.Fatalf("完成行 = %+v", records)
	}
	outcomes := runner.reported()
	if len(outcomes) != 1 || outcomes[0] != nil {
		t.Fatalf("终态回调 = %v want 1 success", outcomes)
	}
}

func TestRunRetriesUntilLimit(t *testing.T) {
	jobs := &fakeJobs{pending: []domain.Job{{ID: "job-1", Kind: domain.JobKindMemoryAdd, Payload: []byte("{}")}}}
	runner := &fakeRunner{err: domain.Wrap(domain.ErrUpstreamUnavailable, "add", "上游不可用", nil)}
	recorder := &fakeRecorder{}
	observer := &fakeObserver{}
	svc := New(jobs, runner, slog.Default(), Options{
		PollInterval: time.Millisecond, MaxAttempts: 3,
		RetryBase: time.Millisecond, RetryMax: 4 * time.Millisecond,
		Audit: recorder, Observer: observer,
	})
	stop := runUntil(t, svc, func() bool { return runner.calls() >= 3 })

	stop()
	if got := runner.calls(); got != 3 {
		t.Fatalf("runner calls=%d want=3", got)
	}
	reschedules := jobs.rescheduled()
	if len(reschedules) != 2 {
		t.Fatalf("重排次数=%d want=2", len(reschedules))
	}
	for i, r := range reschedules {
		if r.id != "job-1" || r.err == "" {
			t.Fatalf("第 %d 次重排 = %+v，应复用同一 job ID 并记录错误", i+1, r)
		}
		if !r.next.After(time.Now().Add(-time.Second)) {
			t.Fatalf("第 %d 次重排的下次时间不合理: %v", i+1, r.next)
		}
	}
	if reschedules[1].next.Before(reschedules[0].next) {
		t.Fatalf("退避应递增: %+v", reschedules)
	}
	if job, ok := jobs.statusOf("job-1"); !ok || job != domain.JobStatusFailed {
		t.Fatalf("最终状态 = %s, %v want failed", job, ok)
	}
	records := recorder.all()
	if len(records) != 1 {
		t.Fatalf("只有终态失败写审计行，实际 %d 条: %+v", len(records), records)
	}
	if rec := records[0]; rec.OK || rec.Err == "" || rec.CallID != "job-1" {
		t.Fatalf("终态失败行 = %+v，应共享 job ID", rec)
	}
	// 瞬时失败不回调；只有最终失败才收敛请求去重记录。
	outcomes := runner.reported()
	if len(outcomes) != 1 || outcomes[0] == nil {
		t.Fatalf("终态回调 = %v want 1 failure", outcomes)
	}
	if got := observer.exhausted(); len(got) != 1 || got[0] != "memory_add/attempts" {
		t.Fatalf("终态失败指标 = %v want [memory_add/attempts]", got)
	}
}

// 中间重试失败不写审计行，最终成功只写一条 OK=true 的终态行。
func TestRetrySuccessWritesSingleTerminalRow(t *testing.T) {
	t.Parallel()

	jobs := &fakeJobs{pending: []domain.Job{{
		ID: "job-1", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a", Payload: []byte("{}"),
	}}}
	runner := &fakeRunner{
		err:          domain.Wrap(domain.ErrUpstreamUnavailable, "add", "上游不可用", nil),
		succeedAfter: 1,
	}
	recorder := &fakeRecorder{}
	svc := New(jobs, runner, slog.Default(), Options{
		PollInterval: time.Millisecond, MaxAttempts: 3,
		RetryBase: time.Millisecond, RetryMax: 4 * time.Millisecond,
		Audit: recorder,
	})
	stop := runUntil(t, svc, func() bool { return jobs.finishedCount() == 1 })
	stop()

	if got := runner.calls(); got != 2 {
		t.Fatalf("调用次数=%d want 2（一次失败 + 一次成功）", got)
	}
	if got := len(jobs.rescheduled()); got != 1 {
		t.Fatalf("重排次数=%d want 1", got)
	}
	if got := jobs.lastStatus(); got != domain.JobStatusSucceeded {
		t.Fatalf("状态=%s want succeeded", got)
	}
	records := recorder.all()
	if len(records) != 1 {
		t.Fatalf("终态行数=%d want 1: %+v", len(records), records)
	}
	if rec := records[0]; !rec.OK || rec.Err != "" || rec.CallID != "job-1" || rec.Tool != auditTool {
		t.Fatalf("终态成功行 = %+v", rec)
	}
	if outcomes := runner.reported(); len(outcomes) != 1 || outcomes[0] != nil {
		t.Fatalf("终态回调 = %v want 1 success", outcomes)
	}
}

// 非 memory_add 的任务：一次即 failed，不写 add 的审计行、不回调、不重试。
func TestUnsupportedKindFailsOnceWithoutAddArtifacts(t *testing.T) {
	jobs := &fakeJobs{pending: []domain.Job{{
		ID: "job-x", Kind: domain.JobKind("memory_feedback"), TenantID: "cube-a", Payload: []byte("{}"),
	}}}
	runner := &fakeRunner{}
	recorder := &fakeRecorder{}
	observer := &fakeObserver{}
	svc := New(jobs, runner, slog.Default(), Options{
		PollInterval: time.Millisecond, MaxAttempts: 3,
		RetryBase: time.Millisecond, RetryMax: 4 * time.Millisecond,
		Audit: recorder, Observer: observer,
	})
	stop := runUntil(t, svc, func() bool { return jobs.finishedCount() == 1 })
	stop()

	if got := runner.calls(); got != 0 {
		t.Fatalf("未知 kind 不应调用 RunAddJob，calls=%d", got)
	}
	if got := len(jobs.rescheduled()); got != 0 {
		t.Fatalf("未知 kind 不应重试，reschedules=%d", got)
	}
	if got := recorder.all(); len(got) != 0 {
		t.Fatalf("未知 kind 不应写 add 审计行: %+v", got)
	}
	if got := runner.reported(); len(got) != 0 {
		t.Fatalf("未知 kind 不应触发 add 的终态回调: %v", got)
	}
	if got := observer.exhausted(); len(got) != 1 || got[0] != "memory_feedback/permanent" {
		t.Fatalf("终态失败指标 = %v", got)
	}
	if status, ok := jobs.statusOf("job-x"); !ok || status != domain.JobStatusFailed {
		t.Fatalf("状态 = %s, %v want failed", status, ok)
	}
}

func TestPermanentErrorsDoNotRetry(t *testing.T) {
	t.Parallel()

	for _, err := range []error{
		domain.Invalid("run_add_job", "负载无法解析"),
		domain.Wrap(domain.ErrUpstreamDenied, "upstream", "记忆服务拒绝了控制面的请求", nil),
		domain.NotFound("run_add_job", "任务"),
		domain.Wrap(domain.ErrConflict, "run_add_job", "冲突", nil),
		domain.Wrap(domain.ErrDisabled, "run_add_job", "已停用", nil),
	} {
		jobs := &fakeJobs{pending: []domain.Job{{ID: "job-1", Kind: domain.JobKindMemoryAdd, Payload: []byte("{}")}}}
		runner := &fakeRunner{err: err}
		svc := New(jobs, runner, slog.Default(), Options{
			PollInterval: time.Millisecond, MaxAttempts: 3,
			RetryBase: time.Millisecond, RetryMax: 4 * time.Millisecond,
		})
		stop := runUntil(t, svc, func() bool { return jobs.finishedCount() == 1 })
		stop()

		if got := runner.calls(); got != 1 {
			t.Fatalf("%v: runner calls=%d want=1", err, got)
		}
		if got := len(jobs.rescheduled()); got != 0 {
			t.Fatalf("%v: 重排次数=%d want=0", err, got)
		}
	}
}

func TestBackoffGrowsAndCaps(t *testing.T) {
	t.Parallel()

	svc := New(&fakeJobs{}, &fakeRunner{}, slog.Default(), Options{
		RetryBase: 5 * time.Second, RetryMax: 40 * time.Second,
	})
	for _, tc := range []struct {
		attempts int
		min, max time.Duration
	}{
		{1, 5 * time.Second, 7500 * time.Millisecond},
		{2, 10 * time.Second, 15 * time.Second},
		{3, 20 * time.Second, 30 * time.Second},
		{4, 40 * time.Second, 40 * time.Second}, // 封顶后不再增长，抖动也不越过上限
		{9, 40 * time.Second, 40 * time.Second},
	} {
		got := svc.backoff(tc.attempts)
		if got < tc.min || got > tc.max {
			t.Fatalf("backoff(%d) = %s, want [%s, %s]", tc.attempts, got, tc.min, tc.max)
		}
	}
}

func TestDrainWritesAfterParentCancel(t *testing.T) {
	jobs := &fakeJobs{pending: []domain.Job{{ID: "job-1", Kind: domain.JobKindMemoryAdd, Payload: []byte("{}")}}}
	started := make(chan struct{})
	release := make(chan struct{})
	runner := &blockingRunner{started: started, release: release}
	svc := New(jobs, runner, slog.Default(), Options{PollInterval: time.Millisecond, JobTimeout: 2 * time.Second})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = svc.Run(ctx)
		close(done)
	}()

	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("runner 未启动")
	}
	cancel() // 停机信号到达时任务仍在执行
	close(release)

	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("Run 未在排空后退出")
	}
	if got := jobs.lastStatus(); got != domain.JobStatusSucceeded {
		t.Fatalf("停机期间的任务未成功记账：%s", got)
	}
}

func TestRunStopsOnCancel(t *testing.T) {
	jobs := &fakeJobs{}
	svc := New(jobs, &fakeRunner{}, slog.Default(), Options{PollInterval: 50 * time.Millisecond})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = svc.Run(ctx)
		close(done)
	}()
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("Run 未在取消后退出")
	}
}

func TestRunProcessesDifferentTenantsConcurrently(t *testing.T) {
	jobs := &fakeJobs{pending: []domain.Job{
		{ID: "job-a", Kind: domain.JobKindMemoryAdd, TenantID: "cube-a", Payload: []byte("{}")},
		{ID: "job-b", Kind: domain.JobKindMemoryAdd, TenantID: "cube-b", Payload: []byte("{}")},
	}}
	runner := &concurrencyRunner{started: make(chan struct{}, 2), release: make(chan struct{})}
	svc := New(jobs, runner, slog.Default(), Options{PollInterval: time.Millisecond, Concurrency: 2})

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = svc.Run(ctx)
		close(done)
	}()

	for range 2 {
		select {
		case <-runner.started:
		case <-time.After(2 * time.Second):
			t.Fatal("不同租户的任务没有并发启动")
		}
	}
	close(runner.release)

	deadline := time.After(2 * time.Second)
	for jobs.finishedCount() < 2 {
		select {
		case <-deadline:
			t.Fatal("任务没有全部完成")
		case <-time.After(time.Millisecond):
		}
	}
	if got := runner.maxInFlight(); got != 2 {
		t.Fatalf("并发度=%d want 2", got)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("Run 未退出")
	}
}

// runUntil 启动 Run，并在条件满足后返回停止函数。
func runUntil(t *testing.T, svc *Service, done func() bool) func() {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() {
		_ = svc.Run(ctx)
		close(finished)
	}()
	deadline := time.After(2 * time.Second)
	for !done() {
		select {
		case <-deadline:
			cancel()
			t.Fatal("等待条件超时")
		case <-time.After(time.Millisecond):
		}
	}
	return func() {
		cancel()
		select {
		case <-finished:
		case <-time.After(time.Second):
			t.Fatal("Run 未退出")
		}
	}
}

type blockingRunner struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (r *blockingRunner) RunAddJob(context.Context, []byte) (domain.AddResult, error) {
	r.once.Do(func() { close(r.started) })
	<-r.release
	return domain.AddResult{}, nil
}

type concurrencyRunner struct {
	started chan struct{}
	release chan struct{}

	mu       sync.Mutex
	inFlight int
	peak     int
}

func (r *concurrencyRunner) RunAddJob(context.Context, []byte) (domain.AddResult, error) {
	r.mu.Lock()
	r.inFlight++
	r.peak = max(r.peak, r.inFlight)
	r.mu.Unlock()

	r.started <- struct{}{}
	<-r.release

	r.mu.Lock()
	r.inFlight--
	r.mu.Unlock()
	return domain.AddResult{}, nil
}

func (r *concurrencyRunner) maxInFlight() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.peak
}

type fakeJobs struct {
	mu          sync.Mutex
	pending     []domain.Job
	running     map[string]*domain.Job
	statuses    map[string]domain.JobStatus
	reschedules []fakeReschedule
	purges      []time.Time
	purged      int64
}

type fakeReschedule struct {
	id   string
	next time.Time
	err  string
}

func (f *fakeJobs) ClaimJob(_ context.Context, now time.Time) (domain.Job, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	// 与真实 store 同语义：同一租户同时只允许一个 running，未到期的任务不可认领。
	for i := range f.pending {
		tenant := f.pending[i].TenantID
		if f.running[tenant] != nil {
			continue
		}
		if !f.pending[i].NextAttemptAt.IsZero() && f.pending[i].NextAttemptAt.After(now) {
			continue
		}
		job := f.pending[i]
		f.pending = append(f.pending[:i], f.pending[i+1:]...)
		if f.running == nil {
			f.running = map[string]*domain.Job{}
		}
		job.Attempts++
		f.running[tenant] = &job
		return job, true, nil
	}
	return domain.Job{}, false, nil
}

func (f *fakeJobs) ResetStaleJobs(context.Context, time.Time, time.Time) (int64, error) {
	return 0, nil
}

func (f *fakeJobs) RescheduleJob(_ context.Context, p domain.JobReschedule) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for tenant, job := range f.running {
		if job.ID != p.ID {
			continue
		}
		delete(f.running, tenant)
		job.Status = domain.JobStatusPending
		job.LastError = p.LastErr
		job.NextAttemptAt = p.Next
		f.pending = append(f.pending, *job)
		f.reschedules = append(f.reschedules, fakeReschedule{id: p.ID, next: p.Next, err: p.LastErr})
		return nil
	}
	return domain.NotFound("reschedule_job", "任务")
}

func (f *fakeJobs) FinishJob(_ context.Context, p domain.JobCompletion) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for tenant, job := range f.running {
		if job.ID == p.ID {
			delete(f.running, tenant)
			break
		}
	}
	if f.statuses == nil {
		f.statuses = map[string]domain.JobStatus{}
	}
	f.statuses[p.ID] = p.Status
	return nil
}

func (f *fakeJobs) PurgeFinishedJobsBefore(_ context.Context, before time.Time) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.purges = append(f.purges, before)
	return f.purged, nil
}

func (f *fakeJobs) PurgeIdempotencyBefore(context.Context, time.Time) (int64, error) {
	return 0, nil
}

func (f *fakeJobs) purgeCalls() []time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]time.Time(nil), f.purges...)
}

func (f *fakeJobs) finishedCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.statuses)
}

func (f *fakeJobs) rescheduled() []fakeReschedule {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]fakeReschedule(nil), f.reschedules...)
}

func (f *fakeJobs) lastStatus() domain.JobStatus {
	f.mu.Lock()
	defer f.mu.Unlock()
	status := domain.JobStatus("")
	for _, got := range f.statuses {
		status = got
	}
	return status
}

func (f *fakeJobs) statusOf(id string) (domain.JobStatus, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	status, ok := f.statuses[id]
	return status, ok
}

type fakeRecorder struct {
	mu   sync.Mutex
	rows []domain.AuditRecord
}

func (f *fakeRecorder) RecordNow(_ context.Context, rec domain.AuditRecord) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rows = append(f.rows, rec)
	return nil
}

func (f *fakeRecorder) all() []domain.AuditRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]domain.AuditRecord(nil), f.rows...)
}

type fakeObserver struct {
	mu         sync.Mutex
	exhaustedC []string
}

func (o *fakeObserver) ObserveJob(string, error, time.Duration) {}

func (o *fakeObserver) ObserveJobExhausted(kind, reason string) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.exhaustedC = append(o.exhaustedC, kind+"/"+reason)
}

func (o *fakeObserver) exhausted() []string {
	o.mu.Lock()
	defer o.mu.Unlock()
	return append([]string(nil), o.exhaustedC...)
}

type fakeRunner struct {
	mu           sync.Mutex
	err          error
	succeedAfter int // 前 N 次返回 err，之后成功；0 表示一直返回 err
	total        int
	outcomes     []error
}

func (f *fakeRunner) RunAddJob(context.Context, []byte) (domain.AddResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.total++
	if f.succeedAfter > 0 && f.total > f.succeedAfter {
		return domain.AddResult{}, nil
	}
	return domain.AddResult{}, f.err
}

func (f *fakeRunner) ReportJobOutcome(_ context.Context, _ []byte, jobErr error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.outcomes = append(f.outcomes, jobErr)
}

func (f *fakeRunner) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.total
}

func (f *fakeRunner) reported() []error {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]error(nil), f.outcomes...)
}

func TestCompletedAddRecordsWrittenMemoryIDs(t *testing.T) {
	t.Parallel()
	recorder := &fakeRecorder{}
	svc := New(&fakeJobs{}, &fakeRunner{}, slog.Default(), Options{Audit: recorder})
	svc.record(context.Background(), jobRecord{Job: domain.Job{ID: "call-1", TenantID: "cube-a"}, Started: time.Now(), Result: domain.AddResult{MemoryIDs: []string{"m1", "m2"}}})
	rows := recorder.all()
	if len(rows) != 1 || rows[0].Output != `{"added_memory_ids":["m1","m2"]}` || rows[0].CallID != "call-1" {
		t.Fatalf("records=%+v", rows)
	}
}

func TestCompletedAddPersistsHistoricalSnapshot(t *testing.T) {
	t.Parallel()
	recorder := &fakeRecorder{}
	svc := New(&fakeJobs{}, &fakeRunner{}, slog.Default(), Options{Audit: recorder})
	snapshot := &domain.MemoryWriteSnapshot{SchemaVersion: 1, TenantID: "p1", UserID: "u1", SessionID: "chat-1", CapturedAt: domain.UTCTime{Time: time.Now().UTC()}, Memories: []domain.Memory{{ID: "m1", Content: "喜欢茶", Key: "饮食偏好", NodeType: "UserMemory", Status: "activated", Version: 1}}}
	svc.record(context.Background(), jobRecord{Job: domain.Job{ID: "call-1", TenantID: "p1", UserID: "u1"}, Started: time.Now(), Result: domain.AddResult{MemoryIDs: []string{"m1"}, Snapshot: snapshot}})
	rows := recorder.all()
	var output domain.MemoryWriteOutput
	if len(rows) != 1 || json.Unmarshal([]byte(rows[0].Output), &output) != nil || output.Snapshot == nil {
		t.Fatalf("snapshot not persisted: %+v", rows)
	}
	if rows[0].CallID != "call-1" || rows[0].SessionID != "chat-1" || output.Snapshot.Memories[0].Content != "喜欢茶" || output.Snapshot.Memories[0].Version != 1 {
		t.Fatalf("snapshot was not preserved: %+v %+v", rows[0], output.Snapshot)
	}
}

func TestLargeSnapshotCannotCorruptPreviouslyValidIDOutput(t *testing.T) {
	t.Parallel()
	recorder := &fakeRecorder{}
	svc := New(&fakeJobs{}, &fakeRunner{}, nil, Options{Audit: recorder})
	ids := make([]string, 4000)
	for i := range ids {
		ids[i] = fmt.Sprintf("%036d", i)
	}
	snapshot := &domain.MemoryWriteSnapshot{SchemaVersion: 1, TenantID: "p1", UserID: "u1", CapturedAt: domain.UTCTime{Time: time.Now().UTC()}, Memories: []domain.Memory{{ID: ids[0], Content: strings.Repeat("x", 127<<10)}}}
	svc.record(context.Background(), jobRecord{Job: domain.Job{ID: "large", TenantID: "p1"}, Started: time.Now(), Result: domain.AddResult{MemoryIDs: ids, Snapshot: snapshot}})
	rows := recorder.all()
	var stored domain.MemoryWriteOutput
	if len(rows) != 1 || len(rows[0].Output) >= 256<<10 || json.Unmarshal([]byte(rows[0].Output), &stored) != nil || stored.Snapshot != nil || len(stored.MemoryIDs) != len(ids) {
		t.Fatalf("snapshot corrupted confirmation: records=%d output_bytes=%d ids=%d", len(rows), len(rows[0].Output), len(stored.MemoryIDs))
	}
}
