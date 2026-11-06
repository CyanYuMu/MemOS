package piagent

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	// 单行上限。超过阈值只丢这一行并继续读，协议流不因此中断。
	maxLineBytes = 16 << 20
	// 单条对外事件的上限；工具结果可能带整页记忆，超过就丢弃并留日志，
	// 避免一条事件把 SSE 连接拖死。
	maxEventBytes = 256 << 10
	// stderr 只进日志；单行截断避免一次异常输出把日志写爆。
	stderrLineLimit = 4 << 10

	// prompt 应答可能被自动压缩阻塞：压缩是同步的一次 LLM 调用，不能用 10 秒判定失败。
	// 用户的受理回执已经在队列层返回，这里的超时只影响该条消息的重试语义。
	promptTimeout   = 3 * time.Minute
	abortTimeout    = 5 * time.Second
	messagesTimeout = 10 * time.Second
	stateTimeout    = 5 * time.Second
	handshakeStep   = 5 * time.Second
	handshakeRetry  = 250 * time.Millisecond
	handshakeTries  = 4
	// handshakeBudget 是两条命令、每次重试都用满时的上界，给外层 deadline 用。
	handshakeBudget = time.Duration(2*handshakeTries) * handshakeStep
	stopGrace       = 2 * time.Second

	eventChatReady  = "chat_ready"
	eventChatStatus = "chat_status"
	eventChatError  = "chat_error"
	eventAccepted   = "message_accepted"

	stateStarting   = "starting"
	stateQueued     = "queued"
	stateRunning    = "running"
	stateCompacting = "compacting"
	stateIdle       = "idle"
	stateNeedsRetry = "needs_retry"
)

var errAgentExited = errors.New("piagent: 进程已退出")

// eventSink 把 pi 的事件交给会话对象。实现必须非阻塞：它在 stdout 读取循环里被调用。
type eventSink func(eventType string, data json.RawMessage)

// Agent 是一个运行中的 pi 进程。事件通过 eventSink 交给会话对象，进程本身不再
// 维护订阅者：订阅属于会话，进程随时可以被回收或驱逐。
type Agent struct {
	sessionID string
	tenantID  string
	model     string
	secrets   []string
	log       *slog.Logger
	cmd       *exec.Cmd
	stdin     io.WriteCloser
	stdout    io.ReadCloser
	stderr    io.ReadCloser
	pgid      int
	lock      *fileLock

	writeMu sync.Mutex

	pendingMu sync.Mutex
	pending   map[string]chan rpcResult
	nextID    atomic.Int64

	lastActive atomic.Int64
	inflight   atomic.Int32
	stopping   atomic.Bool

	emit   eventSink
	onExit func(*Agent)

	done     chan struct{}
	exitOnce sync.Once
}

type rpcResult struct {
	resp rpcResponse
	err  error
}

type rpcResponse struct {
	ID      string          `json:"id"`
	Type    string          `json:"type"`
	Command string          `json:"command"`
	Success bool            `json:"success"`
	Data    json.RawMessage `json:"data"`
	Error   string          `json:"error"`
}

func newAgent(cmd *exec.Cmd, stdin io.WriteCloser, stdout, stderr io.ReadCloser,
	spec domain.ChatSessionSpec, log *slog.Logger, lock *fileLock, emit eventSink, onExit func(*Agent),
) *Agent {
	a := &Agent{
		sessionID: spec.SessionID,
		tenantID:  spec.TenantID,
		model:     spec.Model,
		secrets:   []string{spec.APIKey, spec.ProjectKey},
		log:       log,
		cmd:       cmd,
		stdin:     stdin,
		stdout:    stdout,
		stderr:    stderr,
		pgid:      cmd.Process.Pid,
		lock:      lock,
		pending:   make(map[string]chan rpcResult),
		emit:      emit,
		onExit:    onExit,
		done:      make(chan struct{}),
	}
	a.touch()
	return a
}

// handshake 显式指定模型并打开自动压缩。pi 的模型目录在 RPC 启动后异步装载，
// 进程刚起来就发 set_model 会偶发 "Model not found"；首次失败重试几次，比固定
// sleep 更稳。最终失败只告警：pi 仍可能用默认模型工作。
func (a *Agent) handshake(ctx context.Context) {
	for _, command := range []map[string]any{
		{"type": "set_model", "provider": "byok", "modelId": a.modelID()},
		{"type": "set_auto_compaction", "enabled": true},
	} {
		var resp rpcResponse
		var err error
		for attempt := range handshakeTries {
			stepCtx, cancel := context.WithTimeout(ctx, handshakeStep)
			resp, err = a.request(stepCtx, command)
			cancel()
			if err == nil && resp.Success {
				break
			}
			if attempt+1 < handshakeTries {
				select {
				case <-ctx.Done():
				case <-time.After(handshakeRetry):
				}
			}
		}
		if err != nil {
			a.log.WarnContext(ctx, "pi handshake failed",
				slog.String("session_id", a.sessionID),
				slog.String("command", fmt.Sprint(command["type"])),
				slog.String("error", a.redact(err.Error())))
			continue
		}
		if !resp.Success {
			a.log.WarnContext(ctx, "pi handshake rejected",
				slog.String("session_id", a.sessionID),
				slog.String("command", resp.Command),
				slog.String("error", a.redact(resp.Error)))
		}
	}
}

func (a *Agent) modelID() string { return a.model }

// Send 发 prompt 并等待 pi 接收（响应只表示已开始，不代表整轮完成）。
// streamingBehavior=followUp 是兜底：Go 侧已经串行派发，正常情况下不会在生成中再发；
// 万一两个标签页绕过队列，pi 会把消息排队而不是报错。
func (a *Agent) Send(ctx context.Context, text string) error {
	callCtx, cancel := context.WithTimeout(ctx, promptTimeout)
	defer cancel()
	resp, err := a.request(callCtx, map[string]any{
		"type":              "prompt",
		"message":           text,
		"streamingBehavior": "followUp",
	})
	if err != nil {
		return unavailable("对话请求超时，请稍后重试", err)
	}
	if !resp.Success {
		if strings.Contains(strings.ToLower(resp.Error), "streaming") {
			return a.rejected("上一条回答还在生成，请稍后重试", resp)
		}
		return a.rejected("对话请求失败，请检查模型配置后重试", resp)
	}
	return nil
}

// Abort 中断当前轮；RPC 无响应时杀掉整个进程组兜底。
func (a *Agent) Abort(ctx context.Context) error {
	if err := a.abort(ctx); err != nil {
		if !a.exited() {
			a.kill()
		}
		return unavailable("对话已停止，请重新发送消息", err)
	}
	return nil
}

func (a *Agent) abort(ctx context.Context) error {
	callCtx, cancel := context.WithTimeout(ctx, abortTimeout)
	defer cancel()
	resp, err := a.request(callCtx, map[string]any{"type": "abort"})
	if err != nil {
		return err
	}
	if !resp.Success {
		return a.rejected("中断对话失败", resp)
	}
	return nil
}

// Messages 原样返回 get_messages 的 data，由前端宽松渲染。
func (a *Agent) Messages(ctx context.Context) (json.RawMessage, error) {
	callCtx, cancel := context.WithTimeout(ctx, messagesTimeout)
	defer cancel()
	resp, err := a.request(callCtx, map[string]any{"type": "get_messages"})
	if err != nil {
		return nil, unavailable("读取对话历史失败，请稍后重试", err)
	}
	if !resp.Success {
		return nil, a.rejected("读取对话历史失败，请稍后重试", resp)
	}
	return resp.Data, nil
}

// state 是驱逐候选与看门狗对账用的最小状态。
type agentState struct {
	Streaming  bool `json:"isStreaming"`
	Compacting bool `json:"isCompacting"`
}

func (a *Agent) getState(ctx context.Context) (agentState, error) {
	callCtx, cancel := context.WithTimeout(ctx, stateTimeout)
	defer cancel()
	resp, err := a.request(callCtx, map[string]any{"type": "get_state"})
	if err != nil {
		return agentState{}, err
	}
	if !resp.Success {
		return agentState{}, errors.New(resp.Error)
	}
	var state agentState
	if err := json.Unmarshal(resp.Data, &state); err != nil {
		return agentState{}, err
	}
	return state, nil
}

// Close 优雅停止进程：先 abort，超时或失败再杀进程组。
func (a *Agent) Close() error { return a.stop(context.Background()) }

func (a *Agent) stop(ctx context.Context) error {
	a.stopping.Store(true)
	if a.exited() {
		return nil
	}
	abortCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), abortTimeout)
	abortErr := a.abort(abortCtx)
	cancel()
	// abort 成功后给 pi 一点时间把中断的这轮落到 JSONL；进程本身不会因为
	// abort 退出，所以等一小段就杀，不能等满 stopGrace。
	if abortErr == nil {
		select {
		case <-a.done:
		case <-time.After(300 * time.Millisecond):
		}
	}
	a.kill()
	select {
	case <-a.done:
	case <-time.After(stopGrace):
	}
	return nil
}

// killWait 是驱逐/空闲回收用的快速路径：空闲进程没有需要落盘的进行中轮次，
// 不发 abort，直接杀进程组并等它真正退出（锁在 shutdown 里释放）。
func (a *Agent) killWait(ctx context.Context) {
	a.kill()
	select {
	case <-a.done:
	case <-ctx.Done():
	case <-time.After(stopGrace):
	}
}

func (a *Agent) exited() bool {
	select {
	case <-a.done:
		return true
	default:
		return false
	}
}

func (a *Agent) touch() { a.lastActive.Store(time.Now().UnixNano()) }

// pid 返回子进程 pid；进程未启动或已退出时返回 0。
func (a *Agent) pid() int {
	if a.cmd == nil || a.cmd.Process == nil {
		return 0
	}
	return a.cmd.Process.Pid
}

// request 写一条命令并按 id 等待响应。
func (a *Agent) request(ctx context.Context, command map[string]any) (rpcResponse, error) {
	id := strconv.FormatInt(a.nextID.Add(1), 10)
	command["id"] = id
	ch := make(chan rpcResult, 1)

	a.pendingMu.Lock()
	a.pending[id] = ch
	a.pendingMu.Unlock()
	defer func() {
		a.pendingMu.Lock()
		delete(a.pending, id)
		a.pendingMu.Unlock()
	}()

	a.inflight.Add(1)
	defer a.inflight.Add(-1)
	a.touch()

	if err := a.writeCommand(command); err != nil {
		return rpcResponse{}, err
	}
	select {
	case result := <-ch:
		return result.resp, result.err
	case <-ctx.Done():
		return rpcResponse{}, ctx.Err()
	case <-a.done:
		return rpcResponse{}, errAgentExited
	}
}

func (a *Agent) writeCommand(command map[string]any) error {
	body, err := json.Marshal(command)
	if err != nil {
		return fmt.Errorf("piagent: encode command: %w", err)
	}
	body = append(body, '\n')
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	if _, err := a.stdin.Write(body); err != nil {
		return fmt.Errorf("piagent: write command: %w", err)
	}
	return nil
}

// readStdout 是唯一读取协议流的地方。响应按 id 关联，其余行交给会话对象。
func (a *Agent) readStdout() {
	defer func() { _ = a.stdout.Close() }()
	reader := bufio.NewReaderSize(a.stdout, 64<<10)
	for {
		line, tooLong, err := readLimitedLine(reader, maxLineBytes)
		if tooLong {
			a.log.Warn("dropping oversized pi output line", slog.String("session_id", a.sessionID))
		}
		if err != nil {
			if !errors.Is(err, io.EOF) {
				a.log.Debug("pi stdout closed",
					slog.String("session_id", a.sessionID), slog.Any("err", err))
			}
			return
		}
		line = bytes.TrimSpace(line)
		if len(line) == 0 {
			continue
		}
		a.handleLine(line)
	}
}

func (a *Agent) handleLine(line []byte) {
	var probe struct {
		ID   string `json:"id"`
		Type string `json:"type"`
	}
	if err := json.Unmarshal(line, &probe); err != nil {
		a.log.Debug("discarding malformed pi output",
			slog.String("session_id", a.sessionID), slog.Any("err", err))
		return
	}
	switch probe.Type {
	case "response":
		var resp rpcResponse
		if err := json.Unmarshal(line, &resp); err != nil {
			return
		}
		a.resolve(resp)
		return
	case "extension_ui_request":
		a.rejectExtensionUI(probe.ID)
		return
	case "message_update":
		// thinking / toolcall 的流式片段一次对话可达上千条，前端不展示；
		// 留在服务端只会放大 SSE 流量。只放行文本增量。
		if !forwardMessageUpdate(line) {
			return
		}
	case "":
		return
	}
	if len(line) > maxEventBytes {
		a.log.Warn("dropping oversized pi event",
			slog.String("session_id", a.sessionID),
			slog.String("event", probe.Type),
			slog.Int("bytes", len(line)))
		return
	}
	a.touch()
	if a.emit != nil {
		// 只记事件类型，不记负载：负载里可能有用户原文。debug 级别用来排查
		// 「压缩/收尾事件到底有没有来」这类问题。
		a.log.Debug("pi event", slog.String("session_id", a.sessionID), slog.String("type", probe.Type))
		a.emit(probe.Type, append(json.RawMessage(nil), line...))
	}
}

// forwardMessageUpdate 判断一条 message_update 是否值得发给前端。
func forwardMessageUpdate(line []byte) bool {
	var event struct {
		AssistantMessageEvent struct {
			Type string `json:"type"`
		} `json:"assistantMessageEvent"`
	}
	if err := json.Unmarshal(line, &event); err != nil {
		return false
	}
	switch event.AssistantMessageEvent.Type {
	case "text_start", "text_delta", "text_end":
		return true
	}
	// 思考过程与正式回答分开透传：前端会折叠展示，不能和正文混在一起。
	kind := event.AssistantMessageEvent.Type
	return strings.HasPrefix(kind, "thinking_") || strings.HasPrefix(kind, "reasoning_")
}

func (a *Agent) resolve(resp rpcResponse) {
	if resp.ID == "" {
		return
	}
	a.pendingMu.Lock()
	ch := a.pending[resp.ID]
	delete(a.pending, resp.ID)
	a.pendingMu.Unlock()
	if ch != nil {
		ch <- rpcResult{resp: resp}
	}
}

// rejectExtensionUI 兜底应答：--no-extensions 下正常不会出现，但等待应答的
// 消息不回复会让 pi 挂起。
func (a *Agent) rejectExtensionUI(id string) {
	if id == "" {
		return
	}
	if err := a.writeCommand(map[string]any{
		"type":      "extension_ui_response",
		"id":        id,
		"cancelled": true,
	}); err != nil {
		a.log.Warn("reply to pi extension request",
			slog.String("session_id", a.sessionID), slog.Any("err", err))
	}
}

func (a *Agent) readStderr() {
	defer func() { _ = a.stderr.Close() }()
	reader := bufio.NewReaderSize(a.stderr, 64<<10)
	for {
		line, tooLong, err := readLimitedLine(reader, 64<<10)
		if err != nil {
			return
		}
		if tooLong {
			continue
		}
		text := bytes.TrimSpace(line)
		if len(text) == 0 {
			continue
		}
		if len(text) > stderrLineLimit {
			text = text[:stderrLineLimit]
		}
		a.log.Debug("pi stderr",
			slog.String("session_id", a.sessionID),
			slog.String("output", a.redact(string(text))))
	}
}

func (a *Agent) wait(ctx context.Context) {
	err := a.cmd.Wait()
	a.shutdown(ctx, err)
}

func (a *Agent) shutdown(ctx context.Context, waitErr error) {
	a.exitOnce.Do(func() {
		close(a.done)
		a.failPending(errAgentExited)
		// 进程已经由 cmd.Wait 确认退出，这时才能让下一个进程接手同一份会话文件。
		a.lock.release()
		level := slog.LevelDebug
		if waitErr != nil && !a.stopping.Load() {
			level = slog.LevelWarn
		}
		a.log.Log(ctx, level, "pi process exited",
			slog.String("session_id", a.sessionID), slog.Any("err", waitErr))
		// 主动停止（配置变更、空闲回收、驱逐）不是故障，不向用户推错误。
		if a.emit != nil && !a.stopping.Load() {
			a.emit(eventChatError, json.RawMessage(
				`{"message":"对话进程已退出，请重试","code":"process_exit","retryable":true}`))
		}
		if a.onExit != nil {
			a.onExit(a)
		}
	})
}

func (a *Agent) failPending(err error) {
	a.pendingMu.Lock()
	pending := a.pending
	a.pending = make(map[string]chan rpcResult)
	a.pendingMu.Unlock()
	for _, ch := range pending {
		select {
		case ch <- rpcResult{err: err}:
		default:
		}
	}
}

// redact 兜底抹掉注入的明文密钥：pi 正常不打印它们，但 stderr 与错误文本
// 来自外部二进制，不能假设其内容安全。
func (a *Agent) redact(text string) string {
	for _, secret := range a.secrets {
		if secret != "" {
			text = strings.ReplaceAll(text, secret, "[redacted]")
		}
	}
	return text
}

func (a *Agent) kill() {
	a.stopping.Store(true)
	if a.pgid > 0 {
		killGroup(a.pgid)
	}
	if a.cmd.Process != nil {
		_ = a.cmd.Process.Kill()
	}
}

func (a *Agent) rejected(message string, resp rpcResponse) error {
	a.log.Warn("pi command rejected",
		slog.String("session_id", a.sessionID),
		slog.String("command", resp.Command),
		slog.String("error", a.redact(resp.Error)))
	return unavailable(message, errors.New(resp.Error))
}

// readLimitedLine 读一行，超过 max 时继续消费到换行并标记 tooLong，
// 这样单行异常不会中断整个协议流。
func readLimitedLine(r *bufio.Reader, limit int) ([]byte, bool, error) {
	var buf []byte
	tooLong := false
	for {
		frag, err := r.ReadSlice('\n')
		if !tooLong && len(buf)+len(frag) > limit {
			tooLong = true
			buf = nil
		}
		if !tooLong {
			buf = append(buf, frag...)
		}
		switch {
		case err == nil:
			return buf, tooLong, nil
		case errors.Is(err, bufio.ErrBufferFull):
			continue
		case errors.Is(err, io.EOF):
			if len(buf) == 0 && !tooLong {
				return nil, false, io.EOF
			}
			return buf, tooLong, nil
		default:
			return nil, tooLong, err
		}
	}
}
