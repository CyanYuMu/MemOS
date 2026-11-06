package audit

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/logging"
)

type fakeStore struct {
	mu       sync.Mutex
	inserted []domain.AuditRecord
	filter   Filter
	purged   time.Time

	batches       [][]domain.AuditRecord
	singleCalls   int
	failBatch     int // 前 N 次 InsertAudits 调用返回错误
	failSingles   int // 前 N 次 InsertAudit 调用返回错误
	optimizeCalls int
}

func (f *fakeStore) InsertAudit(_ context.Context, r domain.AuditRecord) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.singleCalls++
	if f.failSingles > 0 {
		f.failSingles--
		return errors.New("single insert failed")
	}
	f.inserted = append(f.inserted, r)
	return nil
}

func (f *fakeStore) InsertAudits(_ context.Context, records []domain.AuditRecord) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.batches = append(f.batches, append([]domain.AuditRecord(nil), records...))
	if f.failBatch > 0 {
		f.failBatch--
		return errors.New("batch insert failed")
	}
	f.inserted = append(f.inserted, records...)
	return nil
}

func (f *fakeStore) ListAudit(_ context.Context, flt Filter) ([]domain.AuditRecord, int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.filter = flt
	return f.inserted, len(f.inserted), nil
}

func (f *fakeStore) GetAudit(context.Context, string) (domain.AuditRecord, error) {
	return domain.AuditRecord{}, domain.NotFound("get_audit", "")
}

func (f *fakeStore) AuditHourCounts(context.Context, Filter) ([]domain.AuditHourCount, error) {
	return nil, nil
}

func (f *fakeStore) PurgeAuditBefore(_ context.Context, before time.Time) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.purged = before
	return 0, nil
}

func (f *fakeStore) Optimize(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.optimizeCalls++
	return nil
}

func (f *fakeStore) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.inserted)
}

func (f *fakeStore) records() []domain.AuditRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]domain.AuditRecord(nil), f.inserted...)
}

func (f *fakeStore) batchCopies() [][]domain.AuditRecord {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([][]domain.AuditRecord, len(f.batches))
	for i := range f.batches {
		out[i] = append([]domain.AuditRecord(nil), f.batches[i]...)
	}
	return out
}

func newRecorder(t *testing.T, store Store, log *slog.Logger, opts Options) *Recorder {
	t.Helper()
	r, err := New(store, log, opts)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return r
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}

func TestRecordAndRun(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{QueueSize: 8, FlushInterval: 10 * time.Millisecond, RetentionDays: 30})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx) }()

	r.Record(context.Background(), domain.AuditRecord{Tool: "add_message", TenantID: "cube"})
	r.Record(context.Background(), domain.AuditRecord{Tool: "search_memory", TenantID: "cube"})
	waitFor(t, func() bool { return fs.count() == 2 })

	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run: %v", err)
	}
	for _, rec := range fs.records() {
		if rec.ID == "" || rec.CallID == "" || rec.TS.IsZero() {
			t.Fatalf("record not prepared: %+v", rec)
		}
	}
}

func TestQueueFullDrops(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{QueueSize: 1})
	r.Record(context.Background(), domain.AuditRecord{Tool: "a"})
	r.Record(context.Background(), domain.AuditRecord{Tool: "b"})
	r.Record(context.Background(), domain.AuditRecord{Tool: "c"})
	if got := r.dropped.Load(); got != 2 {
		t.Fatalf("dropped = %d, want 2", got)
	}
}

func TestQueueByteBudgetDrops(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{QueueSize: 10})
	r.maxQueueBytes = 2048

	big := domain.AuditRecord{Tool: "a", Output: strings.Repeat("x", 1500)}
	r.Record(context.Background(), big)
	r.Record(context.Background(), big) // 第二条会把估算体积推过预算
	if got := r.dropped.Load(); got != 1 {
		t.Fatalf("dropped = %d, want 1", got)
	}
	if got := len(r.ch); got != 1 {
		t.Fatalf("queued = %d, want 1", got)
	}
	if got := r.queuedBytes.Load(); got <= 0 || got > 2048 {
		t.Fatalf("queuedBytes = %d，应在预算内", got)
	}
}

func TestStopDrainsWithoutRun(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{QueueSize: 4})
	r.Record(context.Background(), domain.AuditRecord{Tool: "a"})
	r.Record(context.Background(), domain.AuditRecord{Tool: "b"})
	if err := r.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if fs.count() != 2 {
		t.Fatalf("inserted = %d", fs.count())
	}
	// 停止后不再接收。
	r.Record(context.Background(), domain.AuditRecord{Tool: "c"})
	if fs.count() != 2 {
		t.Fatalf("record after stop was accepted")
	}
}

func TestArchiveKeepsFullPayload(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	path := filepath.Join(t.TempDir(), "audit.jsonl")
	r := newRecorder(t, fs, nil, Options{File: path, QueueSize: 8})
	long := strings.Repeat("记", 200_000) // 600000 字节，远超 256 KiB 的库内上限
	if err := r.RecordNow(context.Background(), domain.AuditRecord{
		Tool: "add_message", Source: domain.AuditSourceREST, TenantID: "cube", Output: long, Query: long,
		Input: map[string]any{"messages": long, "n": 1},
	}); err != nil {
		t.Fatalf("RecordNow: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read archive: %v", err)
	}
	lines := bytes.Split(bytes.TrimSuffix(data, []byte("\n")), []byte("\n"))
	if len(lines) != 1 {
		t.Fatalf("archive lines = %d, want 1", len(lines))
	}
	var got struct {
		Tool   string         `json:"tool"`
		Source string         `json:"source"`
		Tenant string         `json:"tenant"`
		TS     string         `json:"ts"`
		Input  map[string]any `json:"input"`
		Output string         `json:"output"`
		Query  string         `json:"query"`
	}
	if err := json.Unmarshal(lines[0], &got); err != nil {
		t.Fatalf("archive line is not JSON: %v", err)
	}
	if got.Tool != "add_message" || got.Tenant != "cube" || got.TS == "" {
		t.Fatalf("metadata = %+v", got)
	}
	// 归档里也要能看出入口：事后拿 JSONL 对账时，工具名分不出 MCP 与记忆插件。
	if got.Source != domain.AuditSourceREST {
		t.Fatalf("archive source = %q", got.Source)
	}
	messages, _ := got.Input["messages"].(string)
	if messages != long || got.Output != long || got.Query != long {
		t.Fatalf("archive payload was truncated: in=%d out=%d query=%d",
			len(messages), len(got.Output), len(got.Query))
	}
	// SQLite 副本仍按 256 KiB 截断，证明归档与查询副本是两条路径。
	rec := fs.records()[0]
	if len(rec.Output) >= len(long) || !strings.Contains(rec.Output, "[audit payload truncated:") {
		t.Fatalf("sqlite output should be truncated, len=%d", len(rec.Output))
	}
	if err := r.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
}

func TestArchiveSurvivesQueueDrop(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	path := filepath.Join(t.TempDir(), "audit.jsonl")
	r := newRecorder(t, fs, nil, Options{File: path, QueueSize: 1})
	r.Record(context.Background(), domain.AuditRecord{Tool: "add_message", TenantID: "first"})
	r.Record(context.Background(), domain.AuditRecord{Tool: "add_message", TenantID: "second"})
	// 队列容量只有 1，第二条会被丢弃；但归档写入发生在入队之前，两条都应在文件里。
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read archive: %v", err)
	}
	lines := bytes.Split(bytes.TrimSuffix(data, []byte("\n")), []byte("\n"))
	if len(lines) != 2 {
		t.Fatalf("archive lines = %d, want 2", len(lines))
	}
	if err := r.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if fs.count() != 1 {
		t.Fatalf("sqlite rows = %d, want 1（第二条按队列上限丢弃）", fs.count())
	}
}

func TestPrepareKeepsInputVerbatim(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{})
	input := map[string]any{
		"apiKey":        "memos_plain",
		"Authorization": "Bearer x",
		"nested":        map[string]any{"client_secret": "s", "deep": map[string]any{"a": []any{1, "b"}}},
		"n":             1,
	}
	if err := r.RecordNow(context.Background(), domain.AuditRecord{Input: input}); err != nil {
		t.Fatalf("RecordNow: %v", err)
	}
	rec := fs.inserted[0]
	for _, key := range []string{"apiKey", "Authorization"} {
		if rec.Input[key] != input[key] {
			t.Fatalf("%s = %v, want verbatim", key, rec.Input[key])
		}
	}
	nested, ok := rec.Input["nested"].(map[string]any)
	if !ok || nested["client_secret"] != "s" {
		t.Fatalf("nested = %#v", rec.Input["nested"])
	}
	if _, ok := nested["deep"].(map[string]any); !ok {
		t.Fatalf("nested content lost: %#v", nested)
	}
}

func TestPrepareTruncatesPayloadByBytes(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{})
	// 每个字段单独都远超上限；按比例分配后仍应保留足够长的前缀。
	long := strings.Repeat("记", 200_000) // 600000 字节
	if err := r.RecordNow(context.Background(), domain.AuditRecord{
		Tool: "add_message", Output: long, Query: long, ResultBytes: 4242,
		Input: map[string]any{"messages": long, "n": 1},
	}); err != nil {
		t.Fatalf("RecordNow: %v", err)
	}
	rec := fs.inserted[0]
	if rec.ResultBytes != 4242 {
		t.Fatalf("ResultBytes = %d, want 4242（保留上游结果大小）", rec.ResultBytes)
	}
	total := valueBytes(rec.Input, 0) + len(rec.Output) + len(rec.Query)
	if total > maxAuditPayloadBytes {
		t.Fatalf("payload = %d bytes, want <= %d", total, maxAuditPayloadBytes)
	}
	for name, text := range map[string]string{"output": rec.Output, "query": rec.Query} {
		if len(text) >= len(long) {
			t.Fatalf("%s 未被截断: %d", name, len(text))
		}
		if !utf8.ValidString(text) {
			t.Fatalf("%s 截断破坏了 UTF-8", name)
		}
		if !strings.Contains(text, "[audit payload truncated:") {
			t.Fatalf("%s 缺少截断标记: %q", name, text[max(len(text)-80, 0):])
		}
		if len(text) < 1000 {
			t.Fatalf("%s 截得过狠: %d", name, len(text))
		}
	}
	messages, ok := rec.Input["messages"].(string)
	if !ok {
		t.Fatalf("input.messages = %T", rec.Input["messages"])
	}
	if len(messages) >= len(long) || !strings.Contains(messages, "[audit payload truncated:") {
		t.Fatalf("input.messages 未按预期截断: len=%d", len(messages))
	}
	if !utf8.ValidString(messages) {
		t.Fatal("input.messages 截断破坏了 UTF-8")
	}
	if rec.Input["n"] != 1 {
		t.Fatalf("短字段被误改: %#v", rec.Input)
	}
}

func TestPrepareKeepsPayloadUnderCap(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{})
	medium := strings.Repeat("x", 1000)
	if err := r.RecordNow(context.Background(), domain.AuditRecord{
		Tool: "add_message", Output: medium, Query: medium,
		Input: map[string]any{"messages": medium},
	}); err != nil {
		t.Fatalf("RecordNow: %v", err)
	}
	rec := fs.inserted[0]
	if rec.Output != medium || rec.Query != medium || rec.Input["messages"] != medium {
		t.Fatal("未超限的 payload 不应被改写")
	}
}

func TestRecordTakesRequestIDFromContext(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{QueueSize: 4})
	ctx := logging.WithRequestID(context.Background(), "req-123")
	r.Record(ctx, domain.AuditRecord{Tool: "a"})
	if err := r.Stop(context.Background()); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if got := fs.inserted[0].RequestID; got != "req-123" {
		t.Fatalf("RequestID = %q, want req-123", got)
	}
}

func TestStatsWritesGapSummaryOnlyWhenDropped(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{QueueSize: 1, FlushInterval: 10 * time.Millisecond, StatsInterval: time.Hour})
	r.Record(context.Background(), domain.AuditRecord{Tool: "a"})
	r.Record(context.Background(), domain.AuditRecord{Tool: "b"}) // 队列满，丢弃并计数

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx) }()
	cancel() // 停机前会汇报最后一个窗口
	if err := <-done; err != nil {
		t.Fatalf("Run: %v", err)
	}

	var gap *domain.AuditRecord
	records := fs.records()
	for i := range records {
		if records[i].Tool == toolDropSummary {
			gap = &records[i]
		}
	}
	if gap == nil {
		t.Fatal("no gap summary written")
	}
	if got, ok := gap.Input["dropped"].(int64); !ok || got != 1 {
		t.Fatalf("dropped = %v, want 1", gap.Input["dropped"])
	}
	if got, ok := gap.Input["dropped_queue_full"].(int64); !ok || got != 1 {
		t.Fatalf("dropped_queue_full = %v, want 1", gap.Input["dropped_queue_full"])
	}
	if gap.OK || gap.Err != "audit_gap" {
		t.Fatalf("summary status = %+v", gap)
	}
}

func TestListAndPurge(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{RetentionDays: 7})
	if err := r.RecordNow(context.Background(), domain.AuditRecord{Tool: "a"}); err != nil {
		t.Fatalf("RecordNow: %v", err)
	}
	_, total, err := r.List(context.Background(), Filter{TenantID: "cube", Limit: 5})
	if err != nil || total != 1 || fs.filter.TenantID != "cube" || fs.filter.Limit != 5 {
		t.Fatalf("List = %d, %v, filter=%+v", total, err, fs.filter)
	}
}

func TestBatchSplitsByEstimatedBytes(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{QueueSize: 64, FlushInterval: time.Hour, StatsInterval: time.Hour})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx) }()

	// 单条正好用满 payload 上限；十几条就会顶到 4 MiB 的批次预算。
	output := strings.Repeat("x", maxAuditPayloadBytes-8)
	for range 20 {
		r.Record(context.Background(), domain.AuditRecord{Tool: "add_message", Output: output})
	}
	// 队列被消费完时可能仍有记录留在消费循环的本地 batch 里，cancel 会排空并 flush。
	waitFor(t, func() bool { return len(r.ch) == 0 })

	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run: %v", err)
	}
	if got := fs.count(); got != 20 {
		t.Fatalf("inserted = %d, want 20", got)
	}

	batches := fs.batchCopies()
	if len(batches) < 2 {
		t.Fatalf("batches = %d, want >= 2（应按字节切批）", len(batches))
	}
	total := 0
	for i, batch := range batches {
		if len(batch) > batchSize {
			t.Fatalf("batch %d has %d records, want <= %d", i, len(batch), batchSize)
		}
		size := 0
		for j := range batch {
			size += recordBytes(batch[j])
		}
		if size > maxBatchBytes {
			t.Fatalf("batch %d = %d bytes, want <= %d", i, size, maxBatchBytes)
		}
		total += len(batch)
	}
	if total != 20 {
		t.Fatalf("batched records = %d, want 20", total)
	}
}

func TestFlushFallsBackToPerRecord(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{failBatch: 1}
	r := newRecorder(t, fs, nil, Options{})
	batch := []domain.AuditRecord{
		{ID: "a", Tool: "a"}, {ID: "b", Tool: "b"}, {ID: "c", Tool: "c"},
	}
	r.flush(context.Background(), batch)

	if got := fs.count(); got != 3 {
		t.Fatalf("inserted = %d, want 3", got)
	}
	if got := fs.singleCalls; got != 3 {
		t.Fatalf("single calls = %d, want 3", got)
	}
	if got := r.written.Load(); got != 3 {
		t.Fatalf("written = %d, want 3", got)
	}
	if got := r.writeFailed.Load(); got != 0 {
		t.Fatalf("writeFailed = %d, want 0", got)
	}
	written, failed, lastWrite := r.Counters()
	if written != 3 || failed != 0 || lastWrite.IsZero() {
		t.Fatalf("Counters = %d/%d/%v, want 3/0/non-zero", written, failed, lastWrite)
	}
}

func TestFlushFallbackCountsRemainingFailures(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{failBatch: 1, failSingles: 1}
	r := newRecorder(t, fs, nil, Options{})
	batch := []domain.AuditRecord{
		{ID: "a", Tool: "a"}, {ID: "b", Tool: "b"}, {ID: "c", Tool: "c"},
	}
	r.flush(context.Background(), batch)

	if got := fs.count(); got != 2 {
		t.Fatalf("inserted = %d, want 2", got)
	}
	if got := r.written.Load(); got != 2 {
		t.Fatalf("written = %d, want 2", got)
	}
	if got := r.writeFailed.Load(); got != 1 {
		t.Fatalf("writeFailed = %d, want 1", got)
	}
}

func TestFlushFallbackLogsOneSummary(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{failBatch: 1, failSingles: 3}
	var logs bytes.Buffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelWarn}))
	r := newRecorder(t, fs, logger, Options{})
	r.flush(context.Background(), []domain.AuditRecord{
		{ID: "a", Tool: "a"}, {ID: "b", Tool: "b"}, {ID: "c", Tool: "c"},
	})

	if got := r.writeFailed.Load(); got != 3 {
		t.Fatalf("writeFailed = %d, want 3", got)
	}
	if got := r.written.Load(); got != 0 {
		t.Fatalf("written = %d, want 0", got)
	}
	if _, _, lastWrite := r.Counters(); !lastWrite.IsZero() {
		t.Fatalf("lastWrite = %v，全失败时不应推进", lastWrite)
	}
	if got := strings.Count(logs.String(), msgBatchFallback); got != 1 {
		t.Fatalf("summary WARN lines = %d, want 1\n%s", got, logs.String())
	}
}

func TestPurgeCallsOptimize(t *testing.T) {
	t.Parallel()

	fs := &fakeStore{}
	r := newRecorder(t, fs, nil, Options{RetentionDays: 7})
	r.purge(context.Background())

	if got := fs.optimizeCalls; got != 1 {
		t.Fatalf("optimize calls = %d, want 1", got)
	}
}
