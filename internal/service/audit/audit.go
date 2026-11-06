package audit

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/auditfile"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/logging"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service"
)

// Store 是审计需要的存储端口。
type Store interface {
	InsertAudit(ctx context.Context, r domain.AuditRecord) error
	InsertAudits(ctx context.Context, records []domain.AuditRecord) error
	ListAudit(ctx context.Context, f service.AuditFilter) ([]domain.AuditRecord, int, error)
	GetAudit(ctx context.Context, id string) (domain.AuditRecord, error)
	AuditHourCounts(ctx context.Context, f service.AuditFilter) ([]domain.AuditHourCount, error)
	PurgeAuditBefore(ctx context.Context, before time.Time) (int64, error)
}

// Filter 与存储端口的查询条件同形。
type Filter = service.AuditFilter

// Options 是队列、清理与归档文件参数。
type Options struct {
	QueueSize     int
	RetentionDays int
	FlushInterval time.Duration
	PurgeInterval time.Duration
	// StatsInterval 是队列统计摘要与缺口汇总行的汇报周期。
	StatsInterval time.Duration
	// File 非空时把完整 input/output/query 追加到该 JSONL 文件；写入发生在
	// 入队之前，因此队列丢弃不会丢归档。MaxBackups/MaxAgeDays 为 0 表示全部保留。
	File           string
	FileMaxSizeMB  int
	FileMaxBackups int
	FileMaxAgeDays int
	FileCompress   bool
}

const (
	defaultQueueSize     = 1024
	defaultFlushInterval = time.Second
	defaultPurgeInterval = time.Hour
	defaultStatsInterval = 30 * time.Second
	batchSize            = 50
	insertTimeout        = 5 * time.Second
	// maxBatchBytes 是单批审计的估算字节上限。SQLite 只有一个写连接，50 条大记录
	// 组成的超大事务会在整个 insertTimeout 内占住它，期间 jobs 与会话写入只能排队。
	// 4 MiB 既能让常见批次的 fsync 次数保持在低位，又不会让单次事务持续过久。
	maxBatchBytes = 4 << 20
	// maxAuditPayloadBytes 是单条审计 input+output+query 的估算总量上限。
	// 审计要能还原现场，但单条正文无上限时一条记录就能拖垮写链路；256 KiB
	// 足够覆盖正常请求与响应，配合按字节切批后，单批最多约 4 MiB。
	maxAuditPayloadBytes = 256 << 10
	// 队列同时按字节限流：单条审计含完整 input/output，最大可达数 MB，
	// 只按 1024 条限流在 1GiB 的 Pod 里可能先 OOM 再触发丢弃。
	maxQueuedBytes = 64 << 20

	msgQueueFull     = "audit queue full, dropping record"
	msgOverByteQuota = "audit queue over byte budget, dropping record"
	msgStats         = "audit queue stats"
	msgStatsWrite    = "audit stats insert failed"
	// msgBatchFallback 是整批失败后逐条重试的唯一汇总日志；不逐条打日志，
	// 否则故障时一次 flush 就会刷出几十行。
	msgBatchFallback = "audit batch insert failed, retried per record"

	// toolDropSummary 是审计缺口汇总行的工具名：不是一次工具调用，
	// 只用于把“这一窗口丢了审计”持久化到审计表，便于事后追责与补录。
	toolDropSummary = "audit.dropped"
)

// dropReason 区分丢弃原因，避免两类缺口混在一个计数里。
type dropReason string

const (
	dropQueueFull  dropReason = "queue_full"
	dropByteBudget dropReason = "byte_budget"
)

// queueStats 是队列自启动以来的累计计数；汇报时取前后差值。
type queueStats struct {
	enqueued      int64
	written       int64
	writeFailed   int64
	dropped       int64
	droppedFull   int64
	droppedBudget int64
}

func (s queueStats) sub(prev queueStats) queueStats {
	return queueStats{
		enqueued:      s.enqueued - prev.enqueued,
		written:       s.written - prev.written,
		writeFailed:   s.writeFailed - prev.writeFailed,
		dropped:       s.dropped - prev.dropped,
		droppedFull:   s.droppedFull - prev.droppedFull,
		droppedBudget: s.droppedBudget - prev.droppedBudget,
	}
}

// queuedRecord 带上估算体积，出队时从预算里扣回。
type queuedRecord struct {
	rec   domain.AuditRecord
	bytes int
}

// Recorder 异步写入审计记录。
type Recorder struct {
	store         Store
	log           *slog.Logger
	opts          Options
	ch            chan queuedRecord
	maxQueueBytes int
	file          *auditfile.Writer

	mu          sync.Mutex
	started     bool
	stopped     bool
	done        chan struct{}
	finish      sync.Once
	fileClose   sync.Once
	closeErr    error
	enqueued    atomic.Int64
	written     atomic.Int64
	writeFailed atomic.Int64
	fileFailed  atomic.Int64
	// lastWrite 是最近一次成功落库的 Unix 秒，供“审计是否还在产出”告警使用。
	lastWrite   atomic.Int64
	dropped     atomic.Int64
	droppedFull atomic.Int64
	droppedSize atomic.Int64
	queuedBytes atomic.Int64
	now         func() time.Time

	fileErrMu         sync.Mutex
	fileErrLast       time.Time
	fileErrSuppressed int64
}

// New 构造审计记录器。opts 的零值会取默认值；配置了 File 但打不开时返回错误，
// 由进程启动失败暴露，而不是静默丢掉归档。
func New(store Store, log *slog.Logger, opts Options) (*Recorder, error) {
	if log == nil {
		log = slog.Default()
	}
	if opts.QueueSize <= 0 {
		opts.QueueSize = defaultQueueSize
	}
	if opts.FlushInterval <= 0 {
		opts.FlushInterval = defaultFlushInterval
	}
	if opts.PurgeInterval <= 0 {
		opts.PurgeInterval = defaultPurgeInterval
	}
	if opts.StatsInterval <= 0 {
		opts.StatsInterval = defaultStatsInterval
	}
	r := &Recorder{
		store:         store,
		log:           log,
		opts:          opts,
		ch:            make(chan queuedRecord, opts.QueueSize),
		maxQueueBytes: maxQueuedBytes,
		done:          make(chan struct{}),
		now:           time.Now,
	}
	if opts.File != "" {
		file, err := auditfile.New(auditfile.Options{
			Path:       opts.File,
			MaxSizeMB:  opts.FileMaxSizeMB,
			MaxBackups: opts.FileMaxBackups,
			MaxAgeDays: opts.FileMaxAgeDays,
			Compress:   opts.FileCompress,
		})
		if err != nil {
			return nil, err
		}
		r.file = file
	}
	return r, nil
}

// Record 先同步追加完整归档，再非阻塞入队；队列满或超出字节预算时只丢
// SQLite 副本，归档已落盘。ctx 只用于取请求 ID，是否已取消不影响写入。
func (r *Recorder) Record(ctx context.Context, rec domain.AuditRecord) {
	r.mu.Lock()
	stopped := r.stopped
	r.mu.Unlock()
	if stopped {
		return
	}
	rec = r.fill(ctx, rec)
	// 先写归档再入队：归档是长期凭证，不能因为队列满而丢；SQLite 只做查询副本。
	r.archive(ctx, rec)
	rec = capPayload(rec)
	size := recordBytes(rec)
	if r.queuedBytes.Add(int64(size)) > int64(r.maxQueueBytes) {
		r.queuedBytes.Add(-int64(size))
		r.noteDrop(ctx, dropByteBudget)
		return
	}
	select {
	case r.ch <- queuedRecord{rec: rec, bytes: size}:
		r.enqueued.Add(1)
	default:
		r.queuedBytes.Add(-int64(size))
		r.noteDrop(ctx, dropQueueFull)
	}
}

// noteDrop 累计丢弃并按首次、每 100 条记一次 WARN；WARN 带队列深度与已用字节，
// 单靠一行日志也能判断是容量不足还是内存预算打满。
func (r *Recorder) noteDrop(ctx context.Context, reason dropReason) {
	total := r.dropped.Add(1)
	switch reason {
	case dropQueueFull:
		r.droppedFull.Add(1)
	case dropByteBudget:
		r.droppedSize.Add(1)
	}
	if total != 1 && total%100 != 0 {
		return
	}
	attrs := []slog.Attr{
		slog.String("reason", string(reason)),
		slog.Int64("dropped_total", total),
		slog.Int("queue_length", len(r.ch)),
		slog.Int("queue_capacity", cap(r.ch)),
		slog.Int64("queue_bytes", r.queuedBytes.Load()),
	}
	if reason == dropByteBudget {
		r.log.LogAttrs(ctx, slog.LevelWarn, msgOverByteQuota, attrs...)
		return
	}
	r.log.LogAttrs(ctx, slog.LevelWarn, msgQueueFull, attrs...)
}

// Snapshot 返回队列深度、容量、已用字节与累计丢弃数，供指标采样使用。
func (r *Recorder) Snapshot() (depth, capacity int, bytes, droppedFull, droppedBudget int64) {
	return len(r.ch), cap(r.ch), r.queuedBytes.Load(), r.droppedFull.Load(), r.droppedSize.Load()
}

// Counters 返回累计写入/失败条数与最近一次成功落库时间，供指标采样使用。
func (r *Recorder) Counters() (written, writeFailed int64, lastWrite time.Time) {
	unix := r.lastWrite.Load()
	if unix > 0 {
		lastWrite = time.Unix(unix, 0).UTC()
	}
	return r.written.Load(), r.writeFailed.Load(), lastWrite
}

func (r *Recorder) snapshot() queueStats {
	return queueStats{
		enqueued:      r.enqueued.Load(),
		written:       r.written.Load(),
		writeFailed:   r.writeFailed.Load(),
		dropped:       r.dropped.Load(),
		droppedFull:   r.droppedFull.Load(),
		droppedBudget: r.droppedSize.Load(),
	}
}

// reportStats 输出一个统计窗口的日志，并在有缺口时往审计表写一条汇总行。
// 返回本次快照，作为下一窗口的基线。
func (r *Recorder) reportStats(ctx context.Context, prev queueStats) queueStats {
	cur := r.snapshot()
	delta := cur.sub(prev)
	queueLength := len(r.ch)
	queueBytes := r.queuedBytes.Load()

	level := slog.LevelInfo
	if delta.dropped > 0 || delta.writeFailed > 0 {
		level = slog.LevelWarn
	}
	r.log.LogAttrs(ctx, level, msgStats,
		slog.Int64("enqueued", delta.enqueued),
		slog.Int64("written", delta.written),
		slog.Int64("write_failed", delta.writeFailed),
		slog.Int64("dropped", delta.dropped),
		slog.Int64("dropped_queue_full", delta.droppedFull),
		slog.Int64("dropped_byte_budget", delta.droppedBudget),
		slog.Int64("dropped_total", cur.dropped),
		slog.Int("queue_length", queueLength),
		slog.Int("queue_capacity", cap(r.ch)),
		slog.Int64("queue_bytes", queueBytes),
	)
	if delta.dropped > 0 || delta.writeFailed > 0 {
		r.recordGap(ctx, delta, queueLength, queueBytes)
	}
	return cur
}

// recordGap 把窗口内的审计缺口同步写进审计表：日志会轮转，库里的汇总行是长期凭证。
// 写失败只记 WARN，不回灌队列（否则故障时反而放大写压力）。
func (r *Recorder) recordGap(ctx context.Context, delta queueStats, queueLength int, queueBytes int64) {
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), insertTimeout)
	defer cancel()
	rec := domain.AuditRecord{
		ID:     domain.NewID(),
		CallID: domain.NewID(),
		TS:     r.now().UTC(),
		Tool:   toolDropSummary,
		OK:     false,
		Err:    "audit_gap",
		Input: map[string]any{
			"window_seconds":      int64(r.opts.StatsInterval / time.Second),
			"enqueued":            delta.enqueued,
			"written":             delta.written,
			"write_failed":        delta.writeFailed,
			"dropped":             delta.dropped,
			"dropped_queue_full":  delta.droppedFull,
			"dropped_byte_budget": delta.droppedBudget,
			"queue_length":        queueLength,
			"queue_capacity":      cap(r.ch),
			"queue_bytes":         queueBytes,
		},
	}
	rec = r.fill(ctx, rec)
	r.archive(ctx, rec)
	if err := r.store.InsertAudit(writeCtx, capPayload(rec)); err != nil {
		r.log.WarnContext(writeCtx, msgStatsWrite, slog.String("error", err.Error()))
	}
}

// RecordNow 同步写入，用于必须落库的场景（例如后台任务完成行）。
func (r *Recorder) RecordNow(ctx context.Context, rec domain.AuditRecord) error {
	rec = r.fill(ctx, rec)
	r.archive(ctx, rec)
	if err := r.store.InsertAudit(ctx, capPayload(rec)); err != nil {
		r.writeFailed.Add(1)
		return err
	}
	r.noteWritten(1)
	return nil
}

// RecordArchive 只追加 JSONL 归档，不进入 SQLite。用于 MCP 协议帧等没有
// 业务数据、但要求“所有操作留痕”的记录。
func (r *Recorder) RecordArchive(ctx context.Context, rec domain.AuditRecord) {
	r.mu.Lock()
	stopped := r.stopped
	r.mu.Unlock()
	if stopped {
		return
	}
	r.archive(ctx, r.fill(ctx, rec))
}

// FileFailures 返回归档文件累计写入失败条数，供指标采样使用。
func (r *Recorder) FileFailures() int64 { return r.fileFailed.Load() }

// noteWritten 记录成功落库的条数与时间；lastWrite 是审计产出的直接健康信号。
func (r *Recorder) noteWritten(n int64) {
	if n <= 0 {
		return
	}
	r.written.Add(n)
	r.lastWrite.Store(r.now().Unix())
}

// List 查询审计记录。
func (r *Recorder) List(ctx context.Context, f Filter) ([]domain.AuditRecord, int, error) {
	return r.store.ListAudit(ctx, f)
}

// Get 按 ID 取一条审计记录；不存在返回 ErrNotFound。
func (r *Recorder) Get(ctx context.Context, id string) (domain.AuditRecord, error) {
	return r.store.GetAudit(ctx, id)
}

// HourCounts 按 UTC 整点与工具聚合调用次数，过滤条件与 List 同口径（忽略分页；Text、OK 若设置同样生效，当前调用方不设置）。
func (r *Recorder) HourCounts(ctx context.Context, f Filter) ([]domain.AuditHourCount, error) {
	return r.store.AuditHourCounts(ctx, f)
}

// Run 消费队列并定期清理，直到 ctx 取消；退出前排空队列。
func (r *Recorder) Run(ctx context.Context) error {
	r.mu.Lock()
	if r.stopped {
		r.mu.Unlock()
		r.finish.Do(func() { close(r.done) })
		return nil
	}
	r.started = true
	r.mu.Unlock()
	defer r.finish.Do(func() { close(r.done) })

	flushTicker := time.NewTicker(r.opts.FlushInterval)
	defer flushTicker.Stop()
	statsTicker := time.NewTicker(r.opts.StatsInterval)
	defer statsTicker.Stop()

	go r.purgeLoop(ctx)

	var (
		batch      []domain.AuditRecord
		batchBytes int
		prevStats  queueStats
	)
	for {
		select {
		case <-ctx.Done():
			// 停机前汇报最后一个窗口，缺口不因重启而不留痕。
			r.reportStats(ctx, prevStats)
			r.drain(ctx, &batch)
			r.flush(ctx, batch)
			return nil
		case item := <-r.ch:
			r.queuedBytes.Add(-int64(item.bytes))
			batch = append(batch, item.rec)
			batchBytes += item.bytes
			if len(batch) >= batchSize || batchBytes >= maxBatchBytes {
				r.flush(ctx, batch)
				batch = nil
				batchBytes = 0
			}
		case <-flushTicker.C:
			r.flush(ctx, batch)
			batch = nil
			batchBytes = 0
		case <-statsTicker.C:
			prevStats = r.reportStats(ctx, prevStats)
		}
	}
}

// purgeLoop 独立于消费循环做清理；DELETE 再慢也不会挡住审计落库。
func (r *Recorder) purgeLoop(ctx context.Context) {
	r.purge(ctx)
	ticker := time.NewTicker(r.opts.PurgeInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.purge(ctx)
		}
	}
}

// drain 把队列里剩余记录追加到 batch（非阻塞）。
func (r *Recorder) drain(ctx context.Context, batch *[]domain.AuditRecord) {
	for {
		select {
		case item := <-r.ch:
			r.queuedBytes.Add(-int64(item.bytes))
			*batch = append(*batch, item.rec)
		case <-ctx.Done():
			// ctx 已取消只用于退出；继续把已经入队的记录取完。
			if len(r.ch) == 0 {
				return
			}
		default:
			return
		}
	}
}

// flush 按条数与估算字节双阈值把 batch 切成子批次写入；每批用独立的超时
// context，避免停机时的已取消 ctx 让插入全部失败。
func (r *Recorder) flush(ctx context.Context, batch []domain.AuditRecord) {
	for _, chunk := range splitBatch(batch) {
		r.flushChunk(ctx, chunk)
	}
}

// splitBatch 保证每个子批次不超过 batchSize 条与 maxBatchBytes 估算字节。
// 单条记录本身超限时独占一批：宁可慢一点，也不能把一条拆开或空转。
func splitBatch(batch []domain.AuditRecord) [][]domain.AuditRecord {
	if len(batch) == 0 {
		return nil
	}
	chunks := make([][]domain.AuditRecord, 0, len(batch)/batchSize+1)
	start, bytes := 0, 0
	for i := range batch {
		size := recordBytes(batch[i])
		if i > start && (i-start >= batchSize || bytes+size > maxBatchBytes) {
			chunks = append(chunks, batch[start:i])
			start = i
			bytes = 0
		}
		bytes += size
	}
	return append(chunks, batch[start:])
}

// flushChunk 写一个子批次。整批失败时逐条单独重试一次：一条坏记录不应该让
// 同批另外几十条审计一起丢失。成功的计入 written，仍失败的计入 writeFailed，
// 且只打一条汇总 WARN。
func (r *Recorder) flushChunk(ctx context.Context, chunk []domain.AuditRecord) {
	if len(chunk) == 0 {
		return
	}
	batchErr := r.insertBatch(ctx, chunk)
	if batchErr == nil {
		r.noteWritten(int64(len(chunk)))
		return
	}
	if len(chunk) == 1 {
		r.writeFailed.Add(1)
		r.log.WarnContext(ctx, msgBatchFallback,
			slog.Int("count", 1), slog.Int("failed", 1), slog.String("error", batchErr.Error()))
		return
	}
	failed := 0
	firstErr := batchErr
	// 整批失败后的逐条回退共享一个总预算，避免每条各自 5s、
	// 50 条在写锁等待时把消费循环拖住几分钟。
	fallbackCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), insertTimeout)
	defer cancel()
	for i := range chunk {
		if err := r.insertOne(fallbackCtx, chunk[i]); err != nil {
			failed++
			if failed == 1 {
				firstErr = err
			}
			continue
		}
		r.noteWritten(1)
	}
	if failed > 0 {
		r.writeFailed.Add(int64(failed))
	}
	r.log.WarnContext(ctx, msgBatchFallback,
		slog.Int("count", len(chunk)), slog.Int("failed", failed), slog.String("error", firstErr.Error()))
}

func (r *Recorder) insertBatch(ctx context.Context, records []domain.AuditRecord) error {
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), insertTimeout)
	defer cancel()
	return r.store.InsertAudits(writeCtx, records)
}

// insertOne 使用调用方给的 ctx；批量回退时由 flushChunk 统一设总预算，
// 避免每条都重新获得完整的 insertTimeout。
func (r *Recorder) insertOne(ctx context.Context, record domain.AuditRecord) error {
	return r.store.InsertAudit(ctx, record)
}

// Stop 停止接收并等待 Run 排空。可重复调用；排空成功后关闭归档文件。
func (r *Recorder) Stop(ctx context.Context) error {
	r.mu.Lock()
	if r.stopped {
		started := r.started
		r.mu.Unlock()
		if !started {
			r.finish.Do(func() { close(r.done) })
		}
		err := r.wait(ctx)
		if err == nil {
			_ = r.Close()
		}
		return err
	}
	r.stopped = true
	started := r.started
	r.mu.Unlock()

	if !started {
		// 没有 Run 消费，直接同步排空已入队记录。
		for {
			select {
			case item := <-r.ch:
				r.queuedBytes.Add(-int64(item.bytes))
				if err := r.store.InsertAudit(ctx, item.rec); err != nil {
					r.writeFailed.Add(1)
					r.log.WarnContext(ctx, "audit insert failed", slog.String("error", err.Error()))
					continue
				}
				r.noteWritten(1)
			default:
				r.finish.Do(func() { close(r.done) })
				return r.Close()
			}
		}
	}
	err := r.wait(ctx)
	if err == nil {
		_ = r.Close()
	}
	return err
}

// Close 关闭归档文件；不负责排空队列。Run/Stop 结束后由 App 调用。
func (r *Recorder) Close() error {
	r.fileClose.Do(func() {
		if r.file != nil {
			r.closeErr = r.file.Close()
		}
	})
	return r.closeErr
}

func (r *Recorder) wait(ctx context.Context) error {
	select {
	case <-r.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// optimizer 是存储端的可选能力：清理结束后回收空间并刷新查询规划统计。
// 用可选接口而不是扩大 Store 端口，避免只有审计清理用到的能力逼所有 fake 实现。
type optimizer interface {
	Optimize(ctx context.Context) error
}

func (r *Recorder) purge(ctx context.Context) {
	if r.opts.RetentionDays <= 0 {
		return
	}
	before := r.now().Add(-time.Duration(r.opts.RetentionDays) * 24 * time.Hour)
	if _, err := r.store.PurgeAuditBefore(ctx, before); err != nil {
		// 停机时 ctx 取消是预期路径，不必刷 WARN。
		if !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
			r.log.WarnContext(ctx, "audit purge failed", slog.String("error", err.Error()))
		}
		return
	}
	opt, ok := r.store.(optimizer)
	if !ok {
		return
	}
	// 删除已提交，回收不受调用方 ctx 取消影响；单独限时避免拖慢停机。
	optCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), insertTimeout)
	defer cancel()
	if err := opt.Optimize(optCtx); err != nil {
		r.log.WarnContext(optCtx, "audit optimize failed", slog.String("error", err.Error()))
	}
}

// fill 补齐 ID、call_id、时间与请求 ID；不截断正文，归档与数据库共用。
func (r *Recorder) fill(ctx context.Context, rec domain.AuditRecord) domain.AuditRecord {
	if rec.ID == "" {
		rec.ID = domain.NewID()
	}
	if rec.CallID == "" {
		rec.CallID = domain.NewID()
	}
	if rec.TS.IsZero() {
		rec.TS = r.now().UTC()
	}
	// 请求 ID 统一从 context 取：调用方不必逐个传，后台任务没有则为空。
	if rec.RequestID == "" && ctx != nil {
		rec.RequestID = logging.RequestIDFrom(ctx)
	}
	return rec
}

// archive 把完整记录追加到 JSONL。写失败不回滚业务，也不重试：
// 计数并通过 fileFailed 指标告警；SQLite 副本仍会尝试写入。
func (r *Recorder) archive(ctx context.Context, rec domain.AuditRecord) {
	if r.file == nil {
		return
	}
	line, err := encodeFileRecord(rec)
	if err != nil {
		r.noteFileError(ctx, err)
		return
	}
	if err := r.file.WriteLine(line); err != nil {
		r.noteFileError(ctx, err)
	}
}

// noteFileError 累计失败，并同一错误每分钟最多打一条 ERROR，避免磁盘满时刷屏。
func (r *Recorder) noteFileError(ctx context.Context, err error) {
	if err == nil {
		return
	}
	r.fileFailed.Add(1)
	r.fileErrMu.Lock()
	now := r.now()
	if now.Sub(r.fileErrLast) < time.Minute {
		r.fileErrSuppressed++
		r.fileErrMu.Unlock()
		return
	}
	suppressed := r.fileErrSuppressed
	r.fileErrLast = now
	r.fileErrSuppressed = 0
	r.fileErrMu.Unlock()

	attrs := []slog.Attr{slog.String("error", err.Error())}
	if suppressed > 0 {
		attrs = append(attrs, slog.Int64("suppressed", suppressed))
	}
	r.log.LogAttrs(ctx, slog.LevelError, "audit archive write failed", attrs...)
}
