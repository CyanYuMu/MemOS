package piagent

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// newTestSession 构造一个不启动任何 goroutine 依赖的会话对象，用于直接验证
// 收尾逻辑（不看进程）。
func newTestSession(t *testing.T) *Session {
	t.Helper()
	dir := t.TempDir()
	dirs := agentDirs{
		agent:    filepath.Join(dir, "agent"),
		sessions: filepath.Join(dir, "sessions"),
		cwd:      filepath.Join(dir, "cwd"),
		runtime:  filepath.Join(dir, "runtime"),
	}
	if err := os.MkdirAll(dirs.runtime, 0o700); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	s := &Session{
		spec:   domain.ChatSessionSpec{SessionID: "chat-unit"},
		dirs:   dirs,
		log:    slog.New(slog.DiscardHandler),
		subs:   make(map[int64]*subscriber),
		wake:   make(chan struct{}, 1),
		snapCh: make(chan struct{}, 1),
		stopCh: make(chan struct{}),
	}
	return s
}

func TestBroadcastTurnUsesExecutingHeadNotQueuedTail(t *testing.T) {
	t.Parallel()
	s := newTestSession(t)
	s.head = &pendingMessage{ID: "executing-a"}
	s.queue = []pendingMessage{{ID: "executing-a"}, {ID: "queued-b"}}
	sub := &subscriber{ch: make(chan domain.ChatEvent, 4), done: make(chan struct{})}
	s.subs[1] = sub
	s.broadcastTurn("tool_execution_end", json.RawMessage(`{"toolCallId":"tool-a"}`))
	if got := <-sub.ch; got.SubmissionID != "executing-a" {
		t.Fatalf("wrong turn: %+v", got)
	}
	s.broadcast(eventAccepted, json.RawMessage(`{"id":"queued-b","queued":true}`))
	if got := <-sub.ch; got.SubmissionID != "" {
		t.Fatalf("control event borrowed executing id: %+v", got)
	}
	s.head = &pendingMessage{ID: "queued-b"}
	s.broadcastTurn("agent_start", nil)
	if got := <-sub.ch; got.SubmissionID != "queued-b" {
		t.Fatalf("next turn: %+v", got)
	}
}

// TestFailTurnThenSettleKeepsRetry 锁住「发送失败要先摘队头」的顺序：
// 之后 agent 的 abort 会推一条 agent_settled，它不能把这条消息记成完成。
func TestFailTurnThenSettleKeepsRetry(t *testing.T) {
	s := newTestSession(t)
	msg := pendingMessage{ID: "msg-1", Text: "hi", At: time.Now().UTC(), Status: pendingAccepted}

	s.mu.Lock()
	s.queue = append(s.queue, msg)
	s.head = &msg
	s.running = true
	s.mu.Unlock()

	s.failTurn(msg.ID, unavailable("对话服务暂时不可用，请稍后重试", errors.New("write timeout")))
	// 进程收到 abort 后推 agent_settled：此时队头已被摘走，settle 不能动它。
	s.settle()

	s.mu.Lock()
	retry := append([]pendingMessage(nil), s.retry...)
	head := s.head
	s.mu.Unlock()
	if head != nil {
		t.Fatalf("队头应已清空，得到 %+v", head)
	}
	if len(retry) != 1 || retry[0].ID != msg.ID {
		t.Fatalf("失败的消息应进入待重试，得到 %+v", retry)
	}
	if got := lastPendingStatus(t, s.dirs.pendingPath(), msg.ID); got != pendingRetry {
		t.Fatalf("pending 状态 = %q，期望 %q", got, pendingRetry)
	}
}

// TestBeginRestartKeepsInFlightTurn 验证重启/换配置前会把在途轮次摘进待重试，
// 并暂停派发（quiescing），endRestart 之后恢复。
func TestBeginRestartKeepsInFlightTurn(t *testing.T) {
	s := newTestSession(t)
	msg := pendingMessage{ID: "msg-2", Text: "hi", At: time.Now().UTC(), Status: pendingAccepted}
	s.queue = append(s.queue, msg)
	s.head = &msg
	s.running = true

	s.beginRestart()
	s.mu.Lock()
	retry := append([]pendingMessage(nil), s.retry...)
	head := s.head
	quiescing := s.quiescing
	s.mu.Unlock()
	if head != nil {
		t.Fatalf("重启时在途轮次应被摘走，得到 %+v", head)
	}
	if quiescing == 0 {
		t.Fatal("重启期间必须暂停派发")
	}
	if len(retry) != 1 || retry[0].ID != msg.ID {
		t.Fatalf("在途消息应进入待重试，得到 %+v", retry)
	}
	if got := lastPendingStatus(t, s.dirs.pendingPath(), msg.ID); got != pendingRetry {
		t.Fatalf("pending 状态 = %q，期望 %q", got, pendingRetry)
	}

	s.endRestart()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.quiescing != 0 {
		t.Fatal("endRestart 之后应恢复派发")
	}
}

// TestEndRestartWaitsForNestedRestart 锁住「先结束的那次重启不能放开派发」：
// Stop/StopTenant 与 refreshSpec 可能同时进行。
func TestEndRestartWaitsForNestedRestart(t *testing.T) {
	s := newTestSession(t)
	s.beginRestart()
	s.beginRestart()
	s.endRestart()

	s.mu.Lock()
	remaining := s.quiescing
	s.mu.Unlock()
	if remaining == 0 {
		t.Fatal("还有一次重启在进行时不能恢复派发")
	}

	s.endRestart()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.quiescing != 0 {
		t.Fatalf("全部重启结束之后应恢复派发，得到 %d", s.quiescing)
	}
}

// TestDispatchPausedWhileRestarting 锁住派发入口的暂停：重启期间即使被唤醒，
// queue[0] 也不能被提成 head。
func TestDispatchPausedWhileRestarting(t *testing.T) {
	s := newTestSession(t)
	msg := pendingMessage{ID: "msg-4", Text: "hi", At: time.Now().UTC(), Status: pendingAccepted}
	s.mu.Lock()
	s.queue = append(s.queue, msg)
	s.quiescing = 1
	s.mu.Unlock()

	func() {
		// 少了暂停判断就会走进准入；用断言而不是 nil 解引用报错来说明原因。
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("重启期间不应进入准入：%v", r)
			}
		}()
		s.dispatchOnce(context.Background())
	}()

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.head != nil {
		t.Fatalf("重启期间不应派发，得到队头 %+v", s.head)
	}
	if len(s.queue) != 1 {
		t.Fatalf("消息应留在队列里，得到 %d 条", len(s.queue))
	}
}

// TestStaleAgentEventsDropped 锁住事件归属：旧进程残留的事件（尤其 agent_settled）
// 不能落到当前轮次上。
func TestStaleAgentEventsDropped(t *testing.T) {
	s := newTestSession(t)
	stale := &Agent{}
	s.onAgentEventFrom(stale, "agent_start", nil)

	s.mu.Lock()
	running := s.running
	s.agent = stale
	s.mu.Unlock()
	if running {
		t.Fatal("非当前进程的事件必须丢弃")
	}

	s.onAgentEventFrom(stale, "agent_start", nil)
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.running {
		t.Fatal("当前进程的事件应当生效")
	}
}

// TestSettleOnlyOwnsTurn 锁住「一轮只归一个进程收尾」：被停掉的进程推的兜底
// agent_settled 不能把已经派发出去的下一轮记成完成。
func TestSettleOnlyOwnsTurn(t *testing.T) {
	s := newTestSession(t)
	owner, stale := &Agent{}, &Agent{}
	msg := pendingMessage{ID: "msg-5", Text: "hi", At: time.Now().UTC(), Status: pendingAccepted}

	s.mu.Lock()
	s.agent = stale
	s.queue = append(s.queue, msg)
	s.head = &msg
	s.turnAgent = owner
	s.mu.Unlock()

	s.onAgentEvent(stale, "agent_settled", json.RawMessage(`{"type":"agent_settled"}`))
	s.mu.Lock()
	head := s.head
	s.mu.Unlock()
	if head == nil || head.ID != msg.ID {
		t.Fatal("非执行者的 agent_settled 不应收尾这一轮")
	}

	s.onAgentEvent(owner, "agent_settled", json.RawMessage(`{"type":"agent_settled"}`))
	s.mu.Lock()
	head = s.head
	ownerStill := s.turnAgent
	s.mu.Unlock()
	if head != nil {
		t.Fatalf("执行者的 agent_settled 应收尾队头，得到 %+v", head)
	}
	if ownerStill != nil {
		t.Fatal("收尾后应清空 turnAgent")
	}
	if got := lastPendingStatus(t, s.dirs.pendingPath(), msg.ID); got != pendingDone {
		t.Fatalf("pending 状态 = %q，期望 %q", got, pendingDone)
	}
}

// TestOldAgentExitKeepsDispatchedHead 锁住同一个归属判断的另一半：被停掉的旧进程
// 退出时，不能把已经派发给新进程的队头从队列里摘掉。
func TestOldAgentExitKeepsDispatchedHead(t *testing.T) {
	s := newTestSession(t)
	old := &Agent{}
	msg := pendingMessage{ID: "msg-6", Text: "hi", At: time.Now().UTC(), Status: pendingAccepted}

	s.mu.Lock()
	s.agent = old
	s.queue = append(s.queue, msg)
	s.head = &msg
	s.mu.Unlock()

	s.agentExited(old)

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.head == nil || s.head.ID != msg.ID {
		t.Fatalf("旧进程退出不应动正在派发的队头，得到 %+v", s.head)
	}
	if len(s.queue) != 1 {
		t.Fatalf("消息应留在队列里，得到 %d 条", len(s.queue))
	}
	if len(s.retry) != 0 {
		t.Fatalf("旧进程退出不应把这一轮挪进待重试，得到 %+v", s.retry)
	}
}

func lastPendingStatus(t *testing.T, path, id string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read pending: %v", err)
	}
	status := ""
	for _, line := range splitLines(raw) {
		var msg pendingMessage
		if err := json.Unmarshal(line, &msg); err != nil || msg.ID != id {
			continue
		}
		status = msg.Status
	}
	return status
}

func splitLines(raw []byte) [][]byte {
	var out [][]byte
	start := 0
	for i, b := range raw {
		if b == '\n' {
			if i > start {
				out = append(out, raw[start:i])
			}
			start = i + 1
		}
	}
	if start < len(raw) {
		out = append(out, raw[start:])
	}
	return out
}
