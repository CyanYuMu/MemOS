package logging

import (
	"bufio"
	"io"
	"os"
	"strconv"
	"sync"
	"time"
)

// asyncWriter 把日志落盘从请求 goroutine 移开：Write 只写内存缓冲，
// 后台按固定周期 Flush。缓冲满时 Write 会直接 Flush，保证内存不无界增长。
//
// 落盘失败不能静默：slog 会丢掉 handler 返回的错误，所以这里把错误写到
// stderr（kubectl logs 可见）并回调 OnError（累加指标）。
type asyncWriter struct {
	mu     sync.Mutex
	next   io.Writer
	bw     *bufio.Writer
	done   chan struct{}
	once   sync.Once
	closed bool

	onError func(op string, err error)
	errOut  io.Writer

	lastErr    string
	lastReport time.Time
	suppressed int
}

const (
	asyncBufferSize = 64 << 10
	asyncFlushEvery = 200 * time.Millisecond
	// 同一错误 1 分钟内只报一次，避免磁盘满时每条日志都刷 stderr。
	errorReportEvery = time.Minute
)

func newAsyncWriter(next io.Writer, onError func(op string, err error)) *asyncWriter {
	a := &asyncWriter{
		next:    next,
		bw:      bufio.NewWriterSize(next, asyncBufferSize),
		done:    make(chan struct{}),
		onError: onError,
		errOut:  os.Stderr,
	}
	go func() {
		ticker := time.NewTicker(asyncFlushEvery)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				_ = a.flush()
			case <-a.done:
				return
			}
		}
	}()
	return a
}

func (a *asyncWriter) Write(p []byte) (int, error) {
	a.mu.Lock()
	if a.closed {
		a.mu.Unlock()
		return 0, io.ErrClosedPipe
	}
	n, err := a.bw.Write(p)
	if err == nil && a.bw.Buffered() >= asyncBufferSize/2 {
		err = a.bw.Flush()
	}
	if err != nil {
		a.reset()
	}
	a.mu.Unlock()
	if err != nil {
		a.report("write", err)
	}
	return n, err
}

func (a *asyncWriter) flush() error {
	a.mu.Lock()
	err := a.bw.Flush()
	if err != nil {
		a.reset()
	}
	a.mu.Unlock()
	if err != nil {
		a.report("flush", err)
	}
	return err
}

// reset 丢掉写失败的缓冲并清掉 bufio 的错误状态。bufio 出错后会一直返回
// 同一个错误，不重置的话磁盘腾出空间后日志也永远写不进去。
// 调用方必须持有 a.mu。
func (a *asyncWriter) reset() {
	if a.next != nil {
		a.bw.Reset(a.next)
	}
}

// Close 停止后台 Flush 并写尽缓冲，可重复调用。
func (a *asyncWriter) Close() error {
	var err error
	a.once.Do(func() {
		close(a.done)
		a.mu.Lock()
		a.closed = true
		err = a.bw.Flush()
		a.mu.Unlock()
		if err != nil {
			a.report("flush", err)
		}
	})
	return err
}

// report 把落盘失败同时写到 stderr 与回调；同一错误按 errorReportEvery 节流。
func (a *asyncWriter) report(op string, err error) {
	if err == nil {
		return
	}
	a.mu.Lock()
	now := time.Now()
	if a.lastErr == err.Error() && now.Sub(a.lastReport) < errorReportEvery {
		a.suppressed++
		a.mu.Unlock()
		return
	}
	a.lastErr = err.Error()
	a.lastReport = now
	suppressed := a.suppressed
	a.suppressed = 0
	a.mu.Unlock()

	if a.errOut != nil {
		msg := "memosd: log " + op + " failed"
		if suppressed > 0 {
			msg += " (" + strconv.Itoa(suppressed) + " similar errors suppressed)"
		}
		msg += ": " + err.Error() + "\n"
		_, _ = io.WriteString(a.errOut, msg)
	}
	if a.onError != nil {
		a.onError(op, err)
	}
}

// closerGroup 按顺序关闭多个资源；第一个错误优先返回。
type closerGroup []io.Closer

func (g closerGroup) Close() error {
	var first error
	for _, c := range g {
		if c == nil {
			continue
		}
		if err := c.Close(); err != nil && first == nil {
			first = err
		}
	}
	return first
}
