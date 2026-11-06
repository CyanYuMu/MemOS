package logging

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNewKeepsRawFieldsAndInjectsRequestID(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	log, closer, err := New(Options{Level: LevelInfo, Writer: &buf})
	if err != nil || closer != nil {
		t.Fatalf("New: %v, closer=%v", err, closer)
	}
	ctx := WithRequestID(context.Background(), "req-123")

	log.InfoContext(ctx, "tool call",
		slog.String("token", "super-secret"),       //nolint:sloglint // 验证日志保留原始字段
		slog.String("authorization", "Bearer abc"), //nolint:sloglint // 同上
		slog.String("messages", `[{"role":"user"}]`),
		slog.Int("content_bytes", 32),
		slog.String("tool", "add_message"),
	)

	var record map[string]any
	if err := json.Unmarshal(buf.Bytes(), &record); err != nil {
		t.Fatalf("log is not JSON: %v (%s)", err, buf.String())
	}
	if record["token"] != "super-secret" || record["authorization"] != "Bearer abc" {
		t.Errorf("raw credentials should be kept: %v", record)
	}
	if record["messages"] != `[{"role":"user"}]` {
		t.Errorf("content should be kept at INFO: %v", record["messages"])
	}
	if record["request_id"] != "req-123" {
		t.Errorf("request_id = %v", record["request_id"])
	}
	if record["tool"] != "add_message" || record["content_bytes"] != float64(32) {
		t.Errorf("fields lost: %v", record)
	}
}

func TestDebugKeepsContent(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	log, _, err := New(Options{Level: LevelDebug, Writer: &buf})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	log.Debug("dump", slog.String("query", "hello"))

	if !strings.Contains(buf.String(), "hello") {
		t.Fatalf("debug content should be kept: %s", buf.String())
	}
}

func TestRedact(t *testing.T) {
	t.Parallel()

	tests := []struct {
		in, want string
	}{
		{"", ""},
		{"short", "*****"},
		{"0123456789ab", "************"},
		{"0123456789abcdef", "0123…cdef"},
	}
	for _, tt := range tests {
		if got := Redact(tt.in); got != tt.want {
			t.Errorf("Redact(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestParseLevel(t *testing.T) {
	t.Parallel()

	for _, tt := range []struct {
		in   string
		want slog.Level
	}{
		{"debug", slog.LevelDebug},
		{"INFO", slog.LevelInfo},
		{"warn", slog.LevelWarn},
		{"error", slog.LevelError},
	} {
		got, err := ParseLevel(tt.in)
		if err != nil || got != tt.want {
			t.Errorf("ParseLevel(%q) = %v, %v", tt.in, got, err)
		}
	}
	if _, err := ParseLevel("loud"); err == nil {
		t.Error("ParseLevel(loud) should fail")
	}
}

func TestNewWritesToFile(t *testing.T) {
	t.Parallel()

	var buf bytes.Buffer
	path := filepath.Join(t.TempDir(), "logs", "memosd.jsonl")
	log, closer, err := New(Options{
		Level: LevelInfo, Format: "json", Writer: &buf,
		File: path, MaxSizeMB: 1, MaxBackups: 1, MaxAgeDays: 1,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	log.Info("persisted", slog.String("tool", "add_message"))
	if closer != nil {
		_ = closer.Close()
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read log file: %v", err)
	}
	if !strings.Contains(string(raw), `"persisted"`) || !strings.Contains(string(raw), `"add_message"`) {
		t.Fatalf("log file = %s", raw)
	}
	if !strings.Contains(buf.String(), "persisted") {
		t.Fatal("stdout sink should still receive the record")
	}
}

func TestNewFailsWhenLogPathUnusable(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	blocker := filepath.Join(dir, "blocker")
	if err := os.WriteFile(blocker, []byte("x"), 0o600); err != nil {
		t.Fatalf("write blocker: %v", err)
	}
	// blocker 是普通文件，MkdirAll 会失败；启动期必须报错而不是等到第一条日志。
	_, _, err := New(Options{
		Level: LevelInfo, Writer: io.Discard,
		File: filepath.Join(blocker, "memosd.jsonl"),
	})
	if err == nil {
		t.Fatal("New should fail when the log directory cannot be created")
	}
}

func TestWarnIsFlushedImmediately(t *testing.T) {
	t.Parallel()

	path := filepath.Join(t.TempDir(), "memosd.jsonl")
	log, closer, err := New(Options{
		Level: LevelInfo, Format: "json", Writer: io.Discard,
		File: path, MaxSizeMB: 1, MaxBackups: 1, MaxAgeDays: 1,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer func() { _ = closer.Close() }()

	log.Warn("disk pressure", slog.Int("free_mb", 1))

	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read log file: %v", err)
	}
	if !strings.Contains(string(raw), "disk pressure") {
		t.Fatalf("warn record should be flushed before Close: %s", raw)
	}
}

type errorWriter struct{ err error }

func (w errorWriter) Write([]byte) (int, error) { return 0, w.err }

// flakyWriter 先失败 fails 次，之后正常写入，用来验证出错后的恢复。
type flakyWriter struct {
	fails int
	buf   bytes.Buffer
}

func (w *flakyWriter) Write(p []byte) (int, error) {
	if w.fails > 0 {
		w.fails--
		return 0, errors.New("disk full")
	}
	return w.buf.Write(p)
}

func TestAsyncWriterReportsFlushFailure(t *testing.T) {
	t.Parallel()

	var errOut bytes.Buffer
	var ops []string
	ew := errorWriter{err: errors.New("disk full")}
	aw := &asyncWriter{
		next:    ew,
		bw:      bufio.NewWriterSize(ew, 8),
		errOut:  &errOut,
		onError: func(op string, _ error) { ops = append(ops, op) },
	}
	if _, err := aw.bw.WriteString("0123456789abcdef"); err == nil {
		t.Fatal("underlying write should fail")
	}
	if err := aw.flush(); err == nil {
		t.Fatal("flush should return the sink error")
	}
	if len(ops) != 1 || ops[0] != "flush" {
		t.Fatalf("onError ops = %v, want [flush]", ops)
	}
	if !strings.Contains(errOut.String(), "memosd: log flush failed: disk full") {
		t.Fatalf("stderr fallback = %q", errOut.String())
	}
}

func TestAsyncWriterRecoversAfterWriteError(t *testing.T) {
	t.Parallel()

	fw := &flakyWriter{fails: 1}
	aw := &asyncWriter{
		next:   fw,
		bw:     bufio.NewWriterSize(fw, 8),
		errOut: io.Discard,
	}
	// 第一次写失败（模拟磁盘满），asyncWriter 必须 Reset 掉 bufio 的粘性错误。
	if _, err := aw.Write([]byte("0123456789abcdef")); err == nil {
		t.Fatal("first write should fail")
	}
	if _, err := aw.Write([]byte("ok")); err != nil {
		t.Fatalf("write after recovery: %v", err)
	}
	if err := aw.flush(); err != nil {
		t.Fatalf("flush after recovery: %v", err)
	}
	if got := fw.buf.String(); !strings.Contains(got, "ok") {
		t.Fatalf("sink did not recover: %q", got)
	}
}

func TestAsyncWriterThrottlesSameError(t *testing.T) {
	t.Parallel()

	var errOut bytes.Buffer
	var n int
	aw := &asyncWriter{
		errOut:  &errOut,
		onError: func(string, error) { n++ },
	}
	diskFull := errors.New("disk full")
	aw.report("write", diskFull)
	aw.report("write", diskFull)
	if n != 1 {
		t.Fatalf("same error reported %d times within the throttle window, want 1", n)
	}
	aw.report("write", errors.New("permission denied"))
	if n != 2 {
		t.Fatalf("different error should be reported, got %d reports", n)
	}
	if got := strings.Count(errOut.String(), "log write failed"); got != 2 {
		t.Fatalf("stderr lines = %d, want 2: %s", got, errOut.String())
	}
}
