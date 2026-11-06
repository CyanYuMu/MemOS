package worker

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Jobs 是任务存储端口，由 adapter/store 结构化实现。
type Jobs interface {
	// ClaimJob 认领最早的可执行任务；实现方需保证同一租户同时只有一个 running，
	// 且只认领 next_attempt_at 已到期的任务。
	ClaimJob(ctx context.Context, now time.Time) (domain.Job, bool, error)
	// ResetStaleJobs 回收进程崩溃遗留的 running 任务。
	ResetStaleJobs(ctx context.Context, now, staleBefore time.Time) (int64, error)
	// RescheduleJob 把失败任务放回 pending 并指定下次可认领时间；行与 attempts 不变。
	RescheduleJob(ctx context.Context, p domain.JobReschedule) error
	FinishJob(ctx context.Context, p domain.JobCompletion) error
	PurgeFinishedJobsBefore(ctx context.Context, before time.Time) (int64, error)
	PurgeIdempotencyBefore(ctx context.Context, before time.Time) (int64, error)
}

// Runner 执行任务负载，由 service/memory 实现。返回值带上游 usage，
// 供终态审计行记录 token 用量（旧网关 add-bg 日志里的同名字段）。
type Runner interface {
	RunAddJob(ctx context.Context, payload []byte) (domain.AddResult, error)
}

// OutcomeReporter 是可选接口：任务进入终态（成功或永久失败）时回调，
// 请求去重记录据此从 accepted 收敛。瞬时失败走重试，不回调。
type OutcomeReporter interface {
	ReportJobOutcome(ctx context.Context, payload []byte, jobErr error)
}

// JobContextBinder 由用例提供，恢复旧负载未保存的真实队列身份，不从文本推断关联。
type JobContextBinder interface {
	WithJobIdentity(context.Context, domain.Job) context.Context
}

// LocalPromoter 执行本地镜像转存任务，并在终态回写镜像状态；由 service/localsync 实现。
type LocalPromoter interface {
	RunPromoteJob(ctx context.Context, payload []byte) error
	ReportPromoteOutcome(ctx context.Context, payload []byte, jobErr error)
}

// LocalSyncMaintainer 回收本地同步模块的超期数据（未提交批次、镜像保留期）；
// 由 service/localsync 实现。与任务队列的清理同频执行。
type LocalSyncMaintainer interface {
	MaintainLocalSync(ctx context.Context) error
}

// auditTool 是终态行的工具名。它和请求行（add_message）分开，控制台的调用日志与用量只统计请求，
// 不会把一次异步 add 数成两次。
const auditTool = domain.ToolAddMessageBackground

// Recorder 写后台任务的完成行；由 service/audit 实现。
type Recorder interface {
	RecordNow(ctx context.Context, rec domain.AuditRecord) error
}

// Observer 记录任务执行结果；由 internal/platform/metrics 实现。nil 表示不记录。
type Observer interface {
	ObserveJob(kind string, err error, d time.Duration)
	// ObserveJobExhausted 在任务进入终态失败时调用；reason 为 permanent（不可重试）或 attempts（次数用尽）。
	ObserveJobExhausted(kind, reason string)
}

// Options 是轮询与重试参数；零值取默认。
type Options struct {
	PollInterval   time.Duration
	MaxAttempts    int
	Concurrency    int // 同时消费的任务数，默认 4；同一租户始终串行
	JobTimeout     time.Duration
	RetryBase      time.Duration // 首次重试等待，默认 5s；之后按 2 的幂递增
	RetryMax       time.Duration // 单次重试等待上限，默认 10m
	RetentionDays  int           // 终态任务保留天数，零值取 7
	IdempotencyTTL time.Duration // 请求去重记录保留时长，零值取 24h
	Audit          Recorder
	Observer       Observer
	// LocalPromoter 处理 local_promote 任务；nil 时该类型任务永久失败，不重试。
	LocalPromoter LocalPromoter
	// LocalSync 回收本地同步模块的超期数据；nil 时跳过。
	LocalSync LocalSyncMaintainer
}

const (
	defaultPollInterval   = time.Second
	defaultMaxAttempts    = 3
	defaultConcurrency    = 4
	defaultJobTimeout     = 5 * time.Minute
	defaultRetryBase      = 5 * time.Second
	defaultRetryMax       = 10 * time.Minute
	defaultRetentionDays  = 7
	defaultIdempotencyTTL = 24 * time.Hour
	jobWriteTimeout       = 5 * time.Second
	jobPurgeInterval      = time.Hour
	jobPurgeTick          = time.Minute
	claimTimeout          = 5 * time.Second
	// 审计整体上限256KiB；给元数据留余量，不能让新增快照挤坏原确认ID JSON。
	maxSnapshotAuditBytes = 240 << 10
)

// Service 轮询并执行任务。
type Service struct {
	jobs   Jobs
	runner Runner
	log    *slog.Logger
	opts   Options
	now    func() time.Time

	lastPurge time.Time
}

// New 构造 worker。
func New(jobs Jobs, runner Runner, log *slog.Logger, opts Options) *Service {
	if opts.PollInterval <= 0 {
		opts.PollInterval = defaultPollInterval
	}
	if opts.MaxAttempts <= 0 {
		opts.MaxAttempts = defaultMaxAttempts
	}
	if opts.Concurrency <= 0 {
		opts.Concurrency = defaultConcurrency
	}
	if opts.JobTimeout <= 0 {
		opts.JobTimeout = defaultJobTimeout
	}
	if opts.RetryBase <= 0 {
		opts.RetryBase = defaultRetryBase
	}
	if opts.RetryMax <= 0 {
		opts.RetryMax = max(defaultRetryMax, opts.RetryBase)
	}
	if opts.RetryMax < opts.RetryBase {
		opts.RetryMax = opts.RetryBase
	}
	if opts.RetentionDays <= 0 {
		opts.RetentionDays = defaultRetentionDays
	}
	if opts.IdempotencyTTL <= 0 {
		opts.IdempotencyTTL = defaultIdempotencyTTL
	}
	if log == nil {
		log = slog.Default()
	}
	return &Service{jobs: jobs, runner: runner, log: log, opts: opts, now: time.Now}
}

// DrainTimeout 是 Run 返回前在途任务的最坏排空时间，供进程停机等待使用。
func (s *Service) DrainTimeout() time.Duration {
	return s.opts.JobTimeout + jobWriteTimeout
}

// Run 启动固定数量的消费者轮询并执行任务，直到 ctx 取消；正在执行的任务会跑完
// （最多 JobTimeout）再返回。不同租户可以并发执行，同一租户由存储层保证串行。
func (s *Service) Run(ctx context.Context) error {
	var wg sync.WaitGroup
	for range s.opts.Concurrency {
		wg.Go(func() {
			s.consume(ctx)
		})
	}
	wg.Go(func() {
		s.purgeLoop(ctx)
	})
	wg.Wait()
	return nil
}

// consume 是单个消费者的认领循环。清理只在 purgeLoop 里做，避免消费者重复执行。
func (s *Service) consume(ctx context.Context) {
	for {
		// 单次认领先用短超时，DB 忙时不会把消费者和整个 worker 一起挂住。
		claimCtx, cancel := context.WithTimeout(ctx, claimTimeout)
		job, ok, err := s.jobs.ClaimJob(claimCtx, s.now())
		cancel()
		if err != nil {
			if ctx.Err() != nil {
				return // ctx 取消是正常停机，不向上传递 claim 错误
			}
			s.log.WarnContext(ctx, "claim job failed", slog.String("error", err.Error()))
			if !s.wait(ctx) {
				return
			}
			continue
		}
		if !ok {
			if !s.wait(ctx) {
				return
			}
			continue
		}
		s.handle(ctx, job)
		if ctx.Err() != nil {
			return
		}
	}
}

// staleBefore 是 running 任务的失效界限：超过 JobTimeout 加记账预算仍未结束的任务
// 视为进程崩溃遗留，由 ClaimJob 重置回 pending，避免该租户被永久阻塞。
func (s *Service) staleBefore() time.Time {
	return s.now().Add(-(s.opts.JobTimeout + jobWriteTimeout))
}

// purgeLoop 定期回收 stale 任务并清理超期数据；启动时先跑一次。
func (s *Service) purgeLoop(ctx context.Context) {
	s.resetStale(ctx)
	s.maybePurge(ctx)
	ticker := time.NewTicker(jobPurgeTick)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.resetStale(ctx)
			s.maybePurge(ctx)
		}
	}
}

// resetStale 把超过 JobTimeout 仍未结束的 running 任务放回队列。低频执行，
// 避免每个消费者的空轮询都产生一次写事务。
func (s *Service) resetStale(ctx context.Context) {
	n, err := s.jobs.ResetStaleJobs(ctx, s.now(), s.staleBefore())
	if err != nil {
		if ctx.Err() == nil {
			s.log.WarnContext(ctx, "reset stale jobs failed", slog.String("error", err.Error()))
		}
		return
	}
	if n > 0 {
		s.log.InfoContext(ctx, "reset stale jobs", slog.Int64("count", n))
	}
}

// maybePurge 每小时清理一次超期的终态任务，避免 jobs 表无限增长。
func (s *Service) maybePurge(ctx context.Context) {
	now := s.now()
	if now.Sub(s.lastPurge) < jobPurgeInterval {
		return
	}
	s.lastPurge = now
	n, err := s.jobs.PurgeFinishedJobsBefore(ctx, now.AddDate(0, 0, -s.opts.RetentionDays))
	if err != nil {
		if ctx.Err() == nil {
			s.log.WarnContext(ctx, "purge finished jobs failed", slog.String("error", err.Error()))
		}
		return
	}
	if n > 0 {
		s.log.InfoContext(ctx, "purged finished jobs", slog.Int64("count", n))
	}
	if purged, err := s.jobs.PurgeIdempotencyBefore(ctx, now.Add(-s.opts.IdempotencyTTL)); err != nil {
		if ctx.Err() == nil {
			s.log.WarnContext(ctx, "purge idempotency failed", slog.String("error", err.Error()))
		}
	} else if purged > 0 {
		s.log.InfoContext(ctx, "purged idempotency keys", slog.Int64("count", purged))
	}
	if s.opts.LocalSync != nil {
		if err := s.opts.LocalSync.MaintainLocalSync(ctx); err != nil && ctx.Err() == nil {
			s.log.WarnContext(ctx, "maintain local sync failed", slog.String("error", err.Error()))
		}
	}
}

func (s *Service) handle(parent context.Context, job domain.Job) {
	// 在途任务不跟随进程停机信号立即取消：停机语义是“排空”，
	// 记账也必须用不带取消的 ctx，否则任务会永久停在 running。
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), s.opts.JobTimeout)
	defer cancel()

	started := s.now()
	began := time.Now()
	result, err := s.run(ctx, job)
	if s.opts.Observer != nil {
		s.opts.Observer.ObserveJob(string(job.Kind), err, time.Since(began))
	}
	writeCtx, writeCancel := context.WithTimeout(context.WithoutCancel(parent), jobWriteTimeout)
	defer writeCancel()
	// 记账单独计时：审计写满预算时不能连带丢掉状态迁移。
	recordCtx, recordCancel := context.WithTimeout(context.WithoutCancel(parent), jobWriteTimeout)
	defer recordCancel()
	if err == nil {
		s.finish(writeCtx, job.ID, domain.JobStatusSucceeded, "")
		// 审计行只描述 add 的终态；重试中的失败行会被控制台误判成最终失败，不写。
		if job.Kind == domain.JobKindMemoryAdd {
			s.record(recordCtx, jobRecord{Job: job, Started: started, Result: result})
		}
		s.reportOutcome(parent, job, nil)
		s.reportPromote(parent, job, nil)
		return
	}
	s.log.WarnContext(ctx, "job failed",
		slog.String("job_id", job.ID),
		slog.String("kind", string(job.Kind)),
		slog.Int("attempts", job.Attempts),
		slog.String("error", err.Error()),
	)
	if !retryable(err) || job.Attempts >= s.opts.MaxAttempts {
		s.finish(writeCtx, job.ID, domain.JobStatusFailed, jobError(err))
		if job.Kind == domain.JobKindMemoryAdd {
			s.record(recordCtx, jobRecord{Job: job, Started: started, RunErr: err})
		}
		s.reportOutcome(parent, job, err)
		s.reportPromote(parent, job, err)
		if s.opts.Observer != nil {
			reason := "attempts"
			if !retryable(err) {
				reason = "permanent"
			}
			s.opts.Observer.ObserveJobExhausted(string(job.Kind), reason)
		}
		return
	}
	// 重试不落终态也不写审计行：任务保持 running 直到 RescheduleJob 把它放回 pending，
	// 这样退避期间同租户的其它任务不会被这条失败记录卡住；每轮失败只留结构化日志。
	delay := s.backoff(job.Attempts)
	next := s.now().Add(delay)
	retryCtx, retryCancel := context.WithTimeout(context.WithoutCancel(parent), jobWriteTimeout)
	defer retryCancel()
	s.log.InfoContext(ctx, "job rescheduled",
		slog.String("job_id", job.ID),
		slog.Int("attempts", job.Attempts),
		slog.Duration("delay", delay),
	)
	s.reschedule(retryCtx, job.ID, next, jobError(err))
}

func (s *Service) run(ctx context.Context, job domain.Job) (domain.AddResult, error) {
	switch job.Kind {
	case domain.JobKindMemoryAdd:
		if binder, ok := s.runner.(JobContextBinder); ok {
			ctx = binder.WithJobIdentity(ctx, job)
		}
		return s.runner.RunAddJob(ctx, job.Payload)
	case domain.JobKindLocalPromote:
		if s.opts.LocalPromoter == nil {
			return domain.AddResult{}, domain.Wrap(domain.ErrDisabled, "worker", "本地转存能力未装配", nil)
		}
		if err := s.opts.LocalPromoter.RunPromoteJob(ctx, job.Payload); err != nil {
			return domain.AddResult{}, err
		}
		return domain.AddResult{}, nil
	default:
		// 入队侧只产生 memory_add；走到这里说明历史脏行或编程错误。
		s.log.ErrorContext(ctx, "unsupported job kind",
			slog.String("job_id", job.ID),
			slog.String("kind", string(job.Kind)),
		)
		return domain.AddResult{}, domain.Invalid("worker", "不支持的任务类型")
	}
}

// reportOutcome 用独立预算回调终态，避免 finish/retry 用满共享预算后丢回调。
// 只对 memory_add 生效：ReportJobOutcome 收敛的是 add 的请求去重记录，别的 kind 不能套用。
func (s *Service) reportOutcome(parent context.Context, job domain.Job, jobErr error) {
	if job.Kind != domain.JobKindMemoryAdd {
		return
	}
	reporter, ok := s.runner.(OutcomeReporter)
	if !ok {
		return
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), jobWriteTimeout)
	defer cancel()
	if binder, ok := s.runner.(JobContextBinder); ok {
		ctx = binder.WithJobIdentity(ctx, job)
	}
	reporter.ReportJobOutcome(ctx, job.Payload, jobErr)
}

// reportPromote 在 local_promote 进入终态时回写镜像行；瞬时失败不回调。
func (s *Service) reportPromote(parent context.Context, job domain.Job, jobErr error) {
	if job.Kind != domain.JobKindLocalPromote || s.opts.LocalPromoter == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), jobWriteTimeout)
	defer cancel()
	s.opts.LocalPromoter.ReportPromoteOutcome(ctx, job.Payload, jobErr)
}

// retryable 判断失败是否值得重试：只有能确认是永久错误的类型才放弃，
// 分类不确定时偏向重试（例如未包装的内部错误）。
func retryable(err error) bool {
	for _, kind := range []error{
		domain.ErrInvalidInput, domain.ErrUnauthorized, domain.ErrForbidden,
		domain.ErrNotFound, domain.ErrConflict, domain.ErrDisabled, domain.ErrUpstreamDenied,
	} {
		if errors.Is(err, kind) {
			return false
		}
	}
	return true
}

// backoff 是第 attempts 次失败后的等待：RetryBase 起按 2 的幂递增，封顶 RetryMax，
// 再叠加 0..50% 的抖动，避免多租户同时失败后在同一时刻挤回上游。
func (s *Service) backoff(attempts int) time.Duration {
	delay := s.opts.RetryBase
	for i := 1; i < attempts; i++ {
		if delay >= s.opts.RetryMax/2 {
			delay = s.opts.RetryMax
			break
		}
		delay *= 2
	}
	if delay > s.opts.RetryMax {
		delay = s.opts.RetryMax
	}
	delay += time.Duration(rand.Int64N(int64(delay/2) + 1)) //nolint:gosec // 退避抖动只用于打散重试时刻，不需要密码学随机数
	if delay > s.opts.RetryMax {
		delay = s.opts.RetryMax
	}
	return delay
}

func (s *Service) reschedule(ctx context.Context, id string, next time.Time, lastErr string) {
	if err := s.jobs.RescheduleJob(ctx, domain.JobReschedule{
		ID:      id,
		Next:    next,
		LastErr: lastErr,
		Now:     s.now(),
	}); err != nil {
		s.log.WarnContext(ctx, "reschedule job failed",
			slog.String("job_id", id),
			slog.String("error", err.Error()),
		)
	}
}

// jobRecord 是一次任务执行结果的审计入参。
type jobRecord struct {
	Job     domain.Job
	Started time.Time
	Result  domain.AddResult
	RunErr  error
}

func (s *Service) record(ctx context.Context, in jobRecord) {
	job, started, result, runErr := in.Job, in.Started, in.Result, in.RunErr
	if s.opts.Audit == nil {
		return
	}
	rec := domain.AuditRecord{
		ID:             domain.NewID(),
		CallID:         job.ID,
		TS:             s.now(),
		Tool:           auditTool,
		TenantID:       job.TenantID,
		UserID:         job.UserID,
		Ms:             time.Since(started).Milliseconds(),
		OK:             runErr == nil,
		HTTPStatus:     http.StatusOK,
		RequestTokens:  result.Usage["prompt_tokens"],
		ResponseTokens: result.Usage["completion_tokens"],
		LLMCalls:       result.Usage["calls"],
	}
	if runErr == nil && result.MemoryIDs != nil {
		written := domain.MemoryWriteOutput{MemoryIDs: result.MemoryIDs, Snapshot: result.Snapshot}
		output, err := json.Marshal(written)
		if err != nil || len(output) > maxSnapshotAuditBytes {
			written.Snapshot = nil
			output, _ = json.Marshal(written)
		}
		rec.Output = string(output)
		if written.Snapshot != nil {
			rec.SessionID = written.Snapshot.SessionID
		}
	}
	if runErr != nil {
		rec.Err = jobError(runErr)
		// 后台任务没有对外 HTTP 响应，状态按最终结果折算：成功 200、失败 500，
		// 让详情页的 requestCode/responseCode 有真实来源，而不是固定值。
		rec.ErrMessage = auditMessage(runErr)
		rec.HTTPStatus = http.StatusInternalServerError
	}
	if err := s.opts.Audit.RecordNow(ctx, rec); err != nil {
		s.log.WarnContext(ctx, "write job audit failed", slog.String("job_id", job.ID), slog.String("error", err.Error()))
	}
}

func (s *Service) finish(ctx context.Context, id string, status domain.JobStatus, lastErr string) {
	if err := s.jobs.FinishJob(ctx, domain.JobCompletion{
		ID:      id,
		Status:  status,
		LastErr: lastErr,
		Now:     s.now(),
	}); err != nil {
		s.log.WarnContext(ctx, "finish job failed",
			slog.String("job_id", id),
			slog.String("error", err.Error()),
		)
	}
}

// wait 等待一个轮询周期；ctx 取消返回 false。
func (s *Service) wait(ctx context.Context) bool {
	timer := time.NewTimer(s.opts.PollInterval)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

// jobError 只保留安全类型信息，不记录任务负载或记忆正文。
func jobError(err error) string {
	if kind := domain.KindOf(err); kind != nil {
		return kind.Error()
	}
	return "task failed"
}

// auditMessage 只对外暴露领域错误里的安全消息；其他错误统一收口，
// 避免把上游地址、SQL 细节写进审计详情。
func auditMessage(err error) string {
	if domain.KindOf(err) != nil {
		return domain.SafeMessage(err)
	}
	return "内部错误"
}
