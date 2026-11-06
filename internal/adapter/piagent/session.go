package piagent

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	subscriberBuffer = 64

	// 快照合并窗口：一轮结束可能连续触发多次 settle，只需要最后一次结果。
	snapshotTimeout = 10 * time.Second

	// maxQueueDepth 限制单个会话的待发消息数：没有上限时，一个在跑的会话会
	// 无条件收下所有 POST（每条最多 32KB），把内存、磁盘与上游调用量一起放大。
	maxQueueDepth = 20
	// maxRetryDepth 限制待重试消息数，超出时最旧的按 abandoned 丢弃。
	maxRetryDepth = 20
)

// Session 是（账号，项目）的会话对象，实现 domain.ChatSession。
//
// 它是事件与排队的唯一归属：订阅者、seq、受理队列、running 状态都在这里，
// pi 进程只是一个可被回收/驱逐的载体。因此建流、读历史、中断都不会启动进程。
type Session struct {
	rt   *Runtime
	spec domain.ChatSessionSpec
	// id / tenantID 是创建后不变的标识，读它们不需要加锁；s.spec 会被
	// refreshSpec 整体替换，只能经 specSnapshot() 读。
	id       string
	tenantID string
	dirs     agentDirs
	log      *slog.Logger

	mu         sync.Mutex
	subs       map[int64]*subscriber
	nextSub    int64
	seq        int64
	agent      *Agent
	running    bool
	compacting bool
	queue      []pendingMessage
	head       *pendingMessage
	// headCancelled 标记在途轮次是用户主动中断的：settle/exit 时落 cancelled
	// 而不是 failed，也不推「进程退出」错误。
	headCancelled bool
	// retry 是上次进程重启时遗留、尚未完成的消息。不自动重发，只提示用户。
	retry  []pendingMessage
	closed bool
	// turnAgent 是当前这一轮（head）被派给的那个进程。被停掉的旧进程会推一条
	// 兜底 agent_settled，只有它正在跑的那一轮才能被这条事件收尾。
	turnAgent *Agent
	// evicting 表示已经有驱逐/回收在杀这个会话的进程：这期间不能再派发新 prompt，
	// 否则刚发出的那一轮会被 kill 掉，用户看到莫名的 process_exit。
	evicting bool
	// quiescing 是正在进行的重启/换配置计数：派发必须暂停，否则 queue[0] 会被
	// 提成 head 发给旧进程，abort 的 agent_settled 又会把它记成完成。用计数而不是
	// 布尔，因为 Stop/StopTenant 与 refreshSpec 可能重叠，先结束的那次不能提前放行。
	quiescing int

	// specGen 随 spec 一起递增（持 s.mu）。spawn 用它判断自己拿到的配置在进程
	// 起来后是否已被替换：旧代次的进程不能成为当前进程。
	specGen atomic.Int64

	contextWindow  int
	wake           chan struct{}
	snapCh         chan struct{}
	stopCh         chan struct{}
	stopOnce       sync.Once
	dispatchCancel context.CancelFunc
	workers        sync.WaitGroup

	lastActive atomic.Int64
	turnStart  atomic.Int64
	lastEvent  atomic.Int64
}

type subscriber struct {
	id   int64
	ch   chan domain.ChatEvent
	done chan struct{}
}

func newSession(rt *Runtime, spec domain.ChatSessionSpec, dirs agentDirs, contextWindow int, log *slog.Logger) *Session {
	s := &Session{
		rt:            rt,
		spec:          spec,
		id:            spec.SessionID,
		tenantID:      spec.TenantID,
		dirs:          dirs,
		log:           log,
		subs:          make(map[int64]*subscriber),
		contextWindow: contextWindow,
		wake:          make(chan struct{}, 1),
		snapCh:        make(chan struct{}, 1),
		stopCh:        make(chan struct{}),
	}
	s.touch()
	life, cancel := context.WithCancel(context.Background())
	s.dispatchCancel = cancel
	s.workers.Add(2)
	go func() { defer s.workers.Done(); s.dispatchLoop(life) }()
	go func() { defer s.workers.Done(); s.snapshotLoop() }()
	return s
}

// loadRetry 读上次进程退出时遗留的受理记录（不自动重发，交给用户决定）。
// 读盘与重写放在全局锁外：pending.jsonl 可能很大，不能卡住准入与退出记账。
func (s *Session) loadRetry() {
	msgs := loadPending(s.dirs.pendingPath())
	if len(msgs) == 0 {
		return
	}
	s.mu.Lock()
	s.retry = append(msgs, s.retry...)
	var dropped []pendingMessage
	if len(s.retry) > maxRetryDepth {
		dropped = append(dropped, s.retry[:len(s.retry)-maxRetryDepth]...)
		s.retry = append([]pendingMessage(nil), s.retry[len(s.retry)-maxRetryDepth:]...)
	}
	s.mu.Unlock()
	for _, old := range dropped {
		_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: old.ID, Status: pendingAbandoned})
	}
}

func (s *Session) touch() {
	now := time.Now().UnixNano()
	s.lastActive.Store(now)
	s.lastEvent.Store(now)
}

// Submit 先落盘再入队并立即返回；spawn 发生在派发 goroutine 里。
func (s *Session) Submit(_ context.Context, text string) (domain.ChatSubmission, error) {
	msg := pendingMessage{
		ID:     domain.NewID(),
		Text:   text,
		At:     time.Now().UTC(),
		Status: pendingAccepted,
	}

	s.mu.Lock()
	closed := s.closed
	idle := !s.running && !s.compacting && s.head == nil && len(s.queue) == 0
	s.mu.Unlock()
	if closed {
		return domain.ChatSubmission{}, unavailable("对话会话已关闭，请刷新页面后重试", nil)
	}
	// 空闲会话且没有可用槽位时直接拒：用户此刻就该知道要重试，而不是先收下
	// 再在派发时抛一个异步错误（那时他可能已经断线）。
	if idle && (!s.rt.hasRoomFor(s.id) || !s.rt.memoryOK()) {
		s.rt.addBusy()
		return domain.ChatSubmission{}, busy()
	}
	// 落盘在锁外做：fsync 可能阻塞，不能卡住事件广播与派发。
	if err := appendPending(s.dirs.pendingPath(), msg); err != nil {
		return domain.ChatSubmission{}, unavailable("消息未能保存，请稍后重试", err)
	}

	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: msg.ID, Status: pendingCancelled})
		return domain.ChatSubmission{}, unavailable("对话会话已关闭，请刷新页面后重试", nil)
	}
	// 入队时重新判定：落盘期间上一轮可能已经结束，这条其实可以直接开始。
	queued := s.running || s.compacting || s.head != nil || len(s.queue) > 0
	if len(s.queue) >= maxQueueDepth {
		s.mu.Unlock()
		_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: msg.ID, Status: pendingCancelled})
		s.rt.addBusy()
		return domain.ChatSubmission{}, busy()
	}
	s.queue = append(s.queue, msg)
	s.mu.Unlock()

	s.touch()
	s.broadcast(eventAccepted, mustJSON(map[string]any{
		"id":     msg.ID,
		"text":   text,
		"queued": queued,
	}))
	if queued {
		s.broadcastStatus(stateQueued)
	} else {
		s.broadcastStatus(stateStarting)
	}
	s.wakeup()
	return domain.ChatSubmission{ID: msg.ID, Queued: queued}, nil
}

// Abort 中断当前轮并清空排队；进程不存在时只是清空队列。
func (s *Session) Abort(ctx context.Context) error {
	s.mu.Lock()
	agent := s.agent
	cancelled := append([]pendingMessage(nil), s.queue...)
	s.queue = nil
	if s.head != nil {
		s.headCancelled = true
	}
	s.mu.Unlock()

	if len(cancelled) > 0 {
		// 队列可能有几十条：逐条 open+fsync 会把一次中断拖成秒级。
		_ = appendPendingBatch(s.dirs.pendingPath(), cancelled, pendingCancelled)
	}
	if agent != nil && !agent.exited() {
		if err := agent.Abort(ctx); err != nil {
			// Agent.Abort 已经在失败时尝试 kill；这里把结果通过 SSE 告诉用户，
			// 中断请求本身不再返回错误（否则前端会把「已停止」当成失败）。
			s.broadcastError("对话已停止，可以重新发送消息", "aborted", true)
			//nolint:nilerr // 中断失败已通过 SSE 告知，HTTP 层保持幂等成功语义
			return nil
		}
	}
	s.broadcastStatus(stateIdle)
	return nil
}

// Messages 进程在时读实时结果，否则读上一轮落下的快照。两种情况都不启动进程。
func (s *Session) Messages(ctx context.Context) (json.RawMessage, error) {
	s.mu.Lock()
	agent := s.agent
	s.mu.Unlock()

	if agent != nil && !agent.exited() {
		raw, err := agent.Messages(ctx)
		if err == nil && len(raw) > 0 {
			return raw, nil
		}
		if err != nil {
			s.log.DebugContext(ctx, "live get_messages failed, fallback to snapshot",
				slog.String("session_id", s.id), slog.Any("err", err))
		}
	}
	raw, err := readHistory(s.dirs.historyPath())
	if err == nil {
		return raw, nil
	}
	return emptyHistoryDoc, nil
}

// Subscribe 注册事件订阅者。进程不存在时也会立即收到 chat_ready。
func (s *Session) Subscribe(ctx context.Context) (<-chan domain.ChatEvent, func()) {
	sub := &subscriber{
		ch:   make(chan domain.ChatEvent, subscriberBuffer),
		done: make(chan struct{}),
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		close(sub.ch)
		close(sub.done)
		return sub.ch, func() {}
	}
	sub.id = s.nextSub
	s.nextSub++
	s.subs[sub.id] = sub
	s.seq++
	sub.ch <- domain.ChatEvent{Seq: s.seq, Type: eventChatReady, Data: s.readyDocLocked()}
	if len(s.retry) > 0 {
		s.seq++
		sub.ch <- domain.ChatEvent{Seq: s.seq, Type: eventChatStatus, Data: mustJSON(map[string]any{
			"state":   stateNeedsRetry,
			"queued":  s.waitingLocked(),
			"message": map[string]any{"id": s.retry[0].ID, "text": s.retry[0].Text},
		})}
	}
	s.mu.Unlock()

	s.touch()
	var once sync.Once
	cancel := func() {
		once.Do(func() { s.removeSub(sub) })
	}
	go func() {
		select {
		case <-ctx.Done():
			cancel()
		case <-sub.done:
		}
	}()
	return sub.ch, cancel
}

func (s *Session) readyDocLocked() json.RawMessage {
	doc := map[string]any{
		"sessionId":     s.id,
		"contextWindow": s.contextWindow,
		"state":         s.stateLocked(),
		"running":       s.running || s.compacting,
		"compacting":    s.compacting,
		"queued":        s.waitingLocked(),
		"needsRetry":    len(s.retry),
	}
	return mustJSON(doc)
}

// onAgentEventFrom 只接受当前进程的事件：旧进程被替换后，它的读取 goroutine
// 可能还在排空管道，残留的 agent_settled 不能落到新一轮上。
func (s *Session) onAgentEventFrom(agent *Agent, eventType string, data json.RawMessage) {
	s.mu.Lock()
	current := s.agent
	s.mu.Unlock()
	// 只接受当前进程的事件：旧进程的读取 goroutine 可能还在排空管道，
	// 它残留的 agent_settled 不能落到新一轮上（s.agent 为 nil 时同样丢弃）。
	if current != agent {
		return
	}
	s.onAgentEvent(agent, eventType, data)
}

// specSnapshot 返回当前配置的快照与它的代次：s.spec 会被整体替换，读它必须持锁。
func (s *Session) specSnapshot() (domain.ChatSessionSpec, int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.spec, s.specGen.Load()
}

// specGeneration 只读代次，不取 s.mu：准入注册时已经在持有全局锁。
func (s *Session) specGeneration() int64 { return s.specGen.Load() }

// beginRestart 收尾在途轮次并暂停派发，供重启/换配置使用。调用方必须保证
// 之后会调 endRestart（除非会话正在关闭）。
func (s *Session) beginRestart() {
	s.mu.Lock()
	head := s.head
	s.head = nil
	s.headCancelled = false
	s.turnAgent = nil
	s.running = false
	s.compacting = false
	s.quiescing++
	if head != nil {
		s.removeQueueLocked(head.ID)
	}
	s.mu.Unlock()
	if head != nil {
		// 这一轮执行到一半就断了，交给用户重试，不能记成完成。
		s.retryLocked(*head)
		s.broadcastError("对话正在重启，请稍后重试", "restarting", true)
		s.broadcastStatus(stateNeedsRetry)
	}
}

func (s *Session) endRestart() {
	s.mu.Lock()
	if s.quiescing > 0 {
		s.quiescing--
	}
	s.mu.Unlock()
	s.wakeup()
}

func (s *Session) applySpec(spec domain.ChatSessionSpec) {
	s.mu.Lock()
	s.spec = spec
	s.specGen.Add(1)
	s.mu.Unlock()
}

// onAgentEvent 在 stdout 读取循环里被调用，必须非阻塞。
func (s *Session) onAgentEvent(agent *Agent, eventType string, data json.RawMessage) {
	s.lastEvent.Store(time.Now().UnixNano())

	switch eventType {
	case "agent_start", "turn_start", "message_start", "message_update",
		"tool_execution_start", "tool_execution_update", "tool_execution_end":
		s.setRunning(true)
	case "compaction_start":
		s.setCompacting(true)
		s.broadcastTurn(eventType, data)
		s.broadcastStatus(stateCompacting)
		return
	case "compaction_end":
		s.setCompacting(false)
		s.broadcastTurn(eventType, data)
		s.broadcastStatus(stateRunning)
		return
	case "agent_settled":
		s.broadcastTurn(eventType, data)
		s.settleFrom(agent)
		return
	}
	s.broadcastTurn(eventType, data)
}

func (s *Session) setRunning(running bool) {
	s.mu.Lock()
	changed := s.running != running
	s.running = running
	if running {
		s.turnStart.Store(time.Now().UnixNano())
	}
	s.mu.Unlock()
	if changed && !running {
		s.broadcastStatus(stateIdle)
	}
}

func (s *Session) setCompacting(compacting bool) {
	s.mu.Lock()
	s.compacting = compacting
	s.mu.Unlock()
}

// settle 处理一轮结束：出队在途消息、落历史快照、唤醒下一条。
func (s *Session) settle() { s.settleHead("") }

// settleFrom 只收尾这个进程正在跑的那一轮。被停掉的进程（换配置、驱逐、中断）
// 会推一条兜底 agent_settled，此时会话可能已经在跑下一条消息，不能把它记成完成。
func (s *Session) settleFrom(agent *Agent) {
	s.mu.Lock()
	owns := s.turnAgent == agent
	s.mu.Unlock()
	if !owns {
		return
	}
	s.settle()
}

// settleHead 只收尾 headID 对应的那一轮。对账/看门狗拿到的是过期快照，
// 必须带上当时的 head id：那一轮如果已经结束、下一条已经在跑，就不能把
// 新一轮当成旧一轮收掉。
func (s *Session) settleHead(headID string) {
	s.mu.Lock()
	if headID != "" && (s.head == nil || s.head.ID != headID) {
		s.mu.Unlock()
		return
	}
	s.running = false
	s.compacting = false
	head := s.head
	cancelled := s.headCancelled
	s.head = nil
	s.headCancelled = false
	s.turnAgent = nil
	if head != nil {
		s.removeQueueLocked(head.ID)
	}
	s.mu.Unlock()

	if head != nil {
		status := pendingDone
		if cancelled {
			status = pendingCancelled
		}
		_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: head.ID, Status: status})
	}
	s.kickSnapshot()
	s.touch()
	if s.retryCount() > 0 {
		s.broadcastStatus(stateNeedsRetry)
	} else {
		s.broadcastStatus(stateIdle)
	}
	s.wakeup()
}

// agentExited 由 Runtime 在进程退出后调用。message 级别的 chat_error 已由
// Agent 在非主动停止时推过，这里只收尾队列与状态。
func (s *Session) agentExited(agent *Agent) {
	s.mu.Lock()
	// 旧进程的回调：会话对象可能已经被重建并挂着新进程，不能动它的状态。
	if s.agent != agent && s.agent != nil {
		s.mu.Unlock()
		return
	}
	s.agent = nil
	if s.turnAgent != agent {
		// 退出的是上一代进程（被停掉/换配置），当前 head 已经交给别的进程或者
		// 正在派发下一条：不能把这一轮当成它自己的那一条收掉。
		state := s.stateLocked()
		s.mu.Unlock()
		s.broadcastStatus(state)
		s.wakeup()
		return
	}
	s.running = false
	s.compacting = false
	s.turnAgent = nil
	head := s.head
	cancelled := s.headCancelled
	s.head = nil
	s.headCancelled = false
	if head != nil {
		s.removeQueueLocked(head.ID)
	}
	s.mu.Unlock()

	if head != nil {
		if cancelled {
			_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: head.ID, Status: pendingCancelled})
		} else {
			// 进程死在半路：这一轮没执行完，留在 needs_retry 里，别让 202
			// 受理过的消息随进程一起消失。Agent.shutdown 已经推过一条错误，
			// 这里不再重复推送。
			s.keepForRetry(*head)
		}
	}
	if s.retryCount() > 0 {
		s.broadcastStatus(stateNeedsRetry)
	} else {
		s.broadcastStatus(stateIdle)
	}
	s.wakeup()
}

// interruptForRestart 在控制面重启前收尾在途轮次：这一条执行到一半就断了，
// 不能当成完成（用户只看到半截回答且没有任何提示），放进 needs_retry 让用户决定。
func (s *Session) interruptForRestart() { s.beginRestart() }

// keepForRetry 把没能执行完的消息放进 needs_retry，并落一条可回放的记录：
// 重启后 loadPending 会把它还原成待重试，而不是当成已完成丢掉。
func (s *Session) keepForRetry(msg pendingMessage) {
	// 走 retryLocked 的裁剪逻辑：进程反复崩溃时待重试列表也不能无限增长。
	s.retryLocked(msg)
}

func (s *Session) setAgent(agent *Agent) {
	if agent == nil || agent.exited() {
		return
	}
	s.mu.Lock()
	s.agent = agent
	s.mu.Unlock()
}

func (s *Session) currentAgent() *Agent {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.agent
}

// evictable 表示这个会话可以被驱逐：没有在途轮次、没有排队的发送。
func (s *Session) evictable() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed || s.running || s.compacting || s.head != nil {
		return false
	}
	return s.agent != nil && !s.agent.exited()
}

// reapIfIdle 原子地判断会话是否已可回收：没有进程、没有在途与排队、没有订阅者，
// 且空闲超过 ttl。判断与置 closed 在同一把锁里，避免「刚判断完又有消息进来」。
func (s *Session) reapIfIdle(ttl time.Duration) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed || s.agent != nil || s.head != nil || s.running || s.compacting ||
		len(s.queue) > 0 || len(s.subs) > 0 {
		return false
	}
	if time.Since(time.Unix(0, s.lastActive.Load())) < ttl {
		return false
	}
	s.closed = true
	return true
}

func (s *Session) isClosed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

func (s *Session) idleFor() time.Duration {
	return time.Since(time.Unix(0, s.lastActive.Load()))
}

// turnAge 是这一轮从开始到现在的时长：事件不断的长轮次也要受硬上限约束，
// 所以不能用 lastEvent 算。
func (s *Session) turnAge() time.Duration {
	s.mu.Lock()
	running := s.running || s.compacting
	s.mu.Unlock()
	if !running {
		return 0
	}
	return time.Since(time.Unix(0, s.turnStart.Load()))
}

func (s *Session) turnStalled() time.Duration {
	s.mu.Lock()
	running := s.running || s.compacting
	s.mu.Unlock()
	if !running {
		return 0
	}
	return time.Since(time.Unix(0, s.lastEvent.Load()))
}

func (s *Session) evict(ctx context.Context) {
	s.mu.Lock()
	if s.closed || s.running || s.compacting || s.head != nil || s.evicting {
		s.mu.Unlock()
		return
	}
	s.evicting = true
	agent := s.agent
	s.mu.Unlock()

	if agent != nil && !agent.exited() {
		agent.killWait(ctx)
	}

	s.mu.Lock()
	s.evicting = false
	s.mu.Unlock()
	s.wakeup()
}

// dispatchLoop 是每个会话唯一的派发者：同一时刻只发一个 prompt。
func (s *Session) dispatchLoop(life context.Context) {
	for {
		select {
		case <-s.stopCh:
			return
		case <-s.wake:
		}
		s.dispatchOnce(life)
	}
}

func (s *Session) dispatchOnce(life context.Context) {
	s.mu.Lock()
	if s.closed || s.quiescing > 0 || s.running || s.compacting || s.head != nil || len(s.queue) == 0 {
		s.mu.Unlock()
		return
	}
	msg := s.queue[0]
	s.head = &msg
	hasAgent := s.agent != nil && !s.agent.exited()
	s.mu.Unlock()

	if !hasAgent {
		s.broadcastStatus(stateStarting)
	}
	ctx, cancel := context.WithTimeout(life, promptTimeout+time.Minute)
	agent, err := s.rt.ensureAgent(ctx, s)
	cancel()
	if err != nil {
		s.failHead(msg.ID, err)
		return
	}

	// 启动期间用户可能点了停止、会话可能被回收：这时不能再把 prompt 发出去，
	// 否则「停止」之后回答照样生成，历史里还会留下一条 cancelled 的完整回答。
	s.mu.Lock()
	stale := s.closed || s.quiescing > 0 || s.head == nil || s.head.ID != msg.ID || s.headCancelled
	evicting := s.evicting
	s.mu.Unlock()
	if evicting && !stale {
		// 驱逐/回收正在杀进程：把队头留着，等它退出后再派发。
		s.mu.Lock()
		if s.head != nil && s.head.ID == msg.ID {
			s.head = nil
			s.removeQueueLocked(msg.ID)
			s.queue = append([]pendingMessage{msg}, s.queue...)
		}
		s.mu.Unlock()
		return
	}
	if stale {
		stopCtx, cancel := context.WithTimeout(context.WithoutCancel(life), evictGrace)
		_ = agent.stop(stopCtx)
		cancel()
		// beginRestart 可能已经把这条消息挪进待重试：这里 dropHead 是空操作，
		// 状态不能刷成 idle，否则「上一条没完成」的提示会消失。
		s.dropHead(msg.ID, pendingCancelled)
		if s.retryCount() > 0 {
			s.broadcastStatus(stateNeedsRetry)
		} else {
			s.broadcastStatus(stateIdle)
		}
		s.wakeup()
		return
	}

	s.claimTurn(agent, msg.ID)
	agent.touch()
	sendCtx, cancel := context.WithTimeout(life, promptTimeout+time.Minute)
	err = agent.Send(sendCtx, msg.Text)
	cancel()
	if err != nil {
		// prompt 超时或写入失败时进程可能还在跑（压缩是同步 LLM 调用，可能正好
		// 卡在上游）。顺序很重要：**先把队头摘走**，再停进程——stop 会发 abort，
		// pi 随即推 agent_settled，如果队头还在就会被 settle 记成 done，
		// 用户看到的是一次「已经完成」的半截回答，且重试无门。
		s.failTurn(msg.ID, err)
		stopCtx, stopCancel := context.WithTimeout(context.WithoutCancel(life), evictGrace)
		_ = agent.stop(stopCtx)
		stopCancel()
		return
	}

	s.mu.Lock()
	// 极短的一轮可能在 Send 返回前就已经 settle；此时 head 已被清掉，
	// 不能再把 running 置真，否则会话会永远停在“生成中”。
	active := s.head != nil && s.head.ID == msg.ID && !s.headCancelled
	if active {
		s.running = true
		s.turnStart.Store(time.Now().UnixNano())
	}
	s.mu.Unlock()
	if active {
		s.broadcastStatus(stateRunning)
	}
}

// claimTurn 先登记这一轮归哪个进程跑，再发 prompt：进程在中途被停时推的兜底
// agent_settled 只能收尾它自己那一轮，不能把下一条消息记成完成。
func (s *Session) claimTurn(agent *Agent, msgID string) {
	s.mu.Lock()
	if s.head != nil && s.head.ID == msgID {
		s.turnAgent = agent
	}
	s.mu.Unlock()
}

// failTurn 收尾「进程还在、这一轮已经发不出去」的情况。与 failHead 的区别是
// 不 pendingFailed、不 wakeup：进程要被停掉，等它真正退出后再派发下一条。
func (s *Session) failTurn(expectID string, err error) {
	message, code, retryable := describeChatError(err)
	head := s.dropHead(expectID, "")
	if head != nil && retryable {
		s.retryLocked(*head)
	}
	s.log.Warn("chat turn failed",
		slog.String("session_id", s.id), slog.String("code", code),
		slog.String("err", errorDetail(err)))
	s.broadcastError(message, code, retryable)
	if s.retryCount() > 0 {
		s.broadcastStatus(stateNeedsRetry)
	} else {
		s.broadcastStatus(stateIdle)
	}
}

func (s *Session) failHead(expectID string, err error) {
	message, code, retryable := describeChatError(err)
	head := s.dropHead(expectID, pendingFailed)
	if head != nil && retryable {
		// 容量满/进程退出这类失败是暂时的：消息留在 needs_retry 里，用户
		// 断线重连后还能看到并重试，而不是只闪过一次 SSE 错误。
		s.retryLocked(*head)
	}
	s.log.Warn("chat message failed",
		slog.String("session_id", s.id), slog.String("code", code),
		slog.String("err", errorDetail(err)))
	s.broadcastError(message, code, retryable)
	if s.retryCount() > 0 {
		s.broadcastStatus(stateNeedsRetry)
	} else {
		s.broadcastStatus(stateIdle)
	}
	// 队里可能还有别的消息：继续派发，别让它们停在队列里等下一次用户输入。
	s.wakeup()
}

// errorDetail 把 domain.Error 里被折叠掉的原因也带进日志：只打 err.Error()
// 时，磁盘满、二进制缺失这类故障都只剩一句给用户看的文案。
func errorDetail(err error) string {
	if err == nil {
		return ""
	}
	parts := []string{err.Error()}
	for cause := errors.Unwrap(err); cause != nil; cause = errors.Unwrap(cause) {
		text := cause.Error()
		if text != parts[len(parts)-1] {
			parts = append(parts, text)
		}
	}
	return strings.Join(parts, " | ")
}

func (s *Session) retryCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.retry)
}

// dropHead 收尾队头：expectID 非空时只在队头仍是它时才动。返回被收尾的消息。
func (s *Session) dropHead(expectID, status string) *pendingMessage {
	s.mu.Lock()
	if s.head == nil || (expectID != "" && s.head.ID != expectID) {
		s.mu.Unlock()
		return nil
	}
	head := *s.head
	s.head = nil
	s.running = false
	s.compacting = false
	s.headCancelled = false
	s.turnAgent = nil
	s.removeQueueLocked(head.ID)
	s.mu.Unlock()

	if status != "" {
		_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: head.ID, Status: status})
	}
	return &head
}

func (s *Session) retryLocked(msg pendingMessage) {
	_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: msg.ID, Status: pendingRetry})
	s.mu.Lock()
	s.retry = append(s.retry, msg)
	var dropped []pendingMessage
	if len(s.retry) > maxRetryDepth {
		dropped = append(dropped, s.retry[:len(s.retry)-maxRetryDepth]...)
		s.retry = append([]pendingMessage(nil), s.retry[len(s.retry)-maxRetryDepth:]...)
	}
	s.mu.Unlock()
	for _, old := range dropped {
		_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: old.ID, Status: pendingAbandoned})
	}
}

// Retry 把最早一条待重试消息重新入队。用户点「重试」必须重发服务端记下的
// 那一条，而不是前端自己猜的最后一条用户消息。
func (s *Session) Retry(_ context.Context) (domain.ChatSubmission, error) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return domain.ChatSubmission{}, unavailable("对话会话已关闭，请刷新页面后重试", nil)
	}
	if len(s.retry) == 0 {
		s.mu.Unlock()
		return domain.ChatSubmission{}, domain.NotFound("chat", "没有待重试的消息")
	}
	if len(s.queue) >= maxQueueDepth {
		s.mu.Unlock()
		return domain.ChatSubmission{}, busy()
	}
	msg := s.retry[0]
	s.retry = s.retry[1:]
	queued := s.running || s.compacting || s.head != nil || len(s.queue) > 0
	s.queue = append(s.queue, msg)
	s.mu.Unlock()

	_ = appendPending(s.dirs.pendingPath(), pendingMessage{ID: msg.ID, Status: pendingAccepted})
	s.touch()
	s.broadcast(eventAccepted, mustJSON(map[string]any{"id": msg.ID, "text": msg.Text, "queued": queued}))
	if queued {
		s.broadcastStatus(stateQueued)
	} else {
		s.broadcastStatus(stateStarting)
	}
	s.wakeup()
	return domain.ChatSubmission{ID: msg.ID, Queued: queued}, nil
}

// markHeadRetry 用于看门狗强杀：这一轮没跑完，但进程要停掉。
func (s *Session) markHeadRetry(headID, reason string) {
	head := s.dropHead(headID, "")
	if head == nil {
		return
	}
	s.retryLocked(*head)
	s.broadcastError(reason, "turn_timeout", true)
	s.broadcastStatus(stateNeedsRetry)
	s.wakeup()
}

func (s *Session) removeQueueLocked(id string) {
	for i, msg := range s.queue {
		if msg.ID == id {
			s.queue = append(s.queue[:i], s.queue[i+1:]...)
			return
		}
	}
}

func (s *Session) wakeup() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *Session) kickSnapshot() {
	select {
	case s.snapCh <- struct{}{}:
	default:
	}
}

func (s *Session) snapshotLoop() {
	for {
		select {
		case <-s.stopCh:
			return
		case <-s.snapCh:
			s.writeSnapshot()
		}
	}
}

func (s *Session) writeSnapshot() {
	agent := s.currentAgent()
	if agent == nil || agent.exited() {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), snapshotTimeout)
	defer cancel()
	raw, err := agent.Messages(ctx)
	if err != nil {
		s.log.Debug("history snapshot skipped",
			slog.String("session_id", s.id), slog.Any("err", err))
		return
	}
	if err := writeHistory(s.dirs.historyPath(), raw); err != nil {
		s.log.Warn("history snapshot failed",
			slog.String("session_id", s.id), slog.Any("err", err))
		return
	}
	s.persistTitle(ctx, raw)
}

func (s *Session) close() {
	s.stopOnce.Do(func() {
		close(s.stopCh)
		if s.dispatchCancel != nil {
			s.dispatchCancel()
		}
		s.mu.Lock()
		s.closed = true
		for id, sub := range s.subs {
			delete(s.subs, id)
			close(sub.ch)
			close(sub.done)
		}
		s.mu.Unlock()
	})
}

func (s *Session) broadcast(eventType string, data json.RawMessage) {
	s.publish(eventType, data, false)
}

func (s *Session) broadcastTurn(eventType string, data json.RawMessage) {
	s.publish(eventType, data, true)
}

func (s *Session) publish(eventType string, data json.RawMessage, owned bool) {
	s.mu.Lock()
	s.seq++
	event := domain.ChatEvent{Seq: s.seq, Type: eventType, Data: data}
	if owned && s.head != nil {
		event.SubmissionID = s.head.ID
	}
	for id, sub := range s.subs {
		select {
		case sub.ch <- event:
		default:
			delete(s.subs, id)
			close(sub.ch)
			close(sub.done)
		}
	}
	s.mu.Unlock()
}

// stateLocked 把内部状态折叠成前端认识的 state，供重连时的 chat_ready 使用：
// 没有它，断线重连后「启动/压缩」就退化成空闲。
func (s *Session) stateLocked() string {
	switch {
	case s.compacting:
		return stateCompacting
	case s.running:
		return stateRunning
	case s.head != nil:
		return stateStarting
	case len(s.queue) > 0:
		return stateQueued
	case len(s.retry) > 0:
		// 只在没有任何在途工作时才把 needs_retry 作为当前状态，避免它盖住
		// 运行/排队；重试本身可离开（POST /api/chat/retry）。
		return stateNeedsRetry
	default:
		return stateIdle
	}
}

// waitingLocked 返回还在排队等待的消息数：队头（正在执行或即将派发的那条）
// 不算排队，否则用户会看到「有一条自己在等自己」或者把启动中误读成积压。
func (s *Session) waitingLocked() int {
	if len(s.queue) == 0 {
		return 0
	}
	return len(s.queue) - 1
}

func (s *Session) broadcastStatus(state string) {
	s.mu.Lock()
	payload := mustJSON(map[string]any{
		"state":      state,
		"queued":     s.waitingLocked(),
		"running":    s.running || s.compacting,
		"compacting": s.compacting,
		"needsRetry": len(s.retry),
	})
	s.seq++
	event := domain.ChatEvent{Seq: s.seq, Type: eventChatStatus, Data: payload}
	for id, sub := range s.subs {
		select {
		case sub.ch <- event:
		default:
			delete(s.subs, id)
			close(sub.ch)
			close(sub.done)
		}
	}
	s.mu.Unlock()
}

func (s *Session) broadcastError(message, code string, retryable bool) {
	s.broadcast(eventChatError, mustJSON(map[string]any{
		"message":   message,
		"code":      code,
		"retryable": retryable,
	}))
}

func (s *Session) removeSub(sub *subscriber) {
	s.mu.Lock()
	if s.subs[sub.id] == sub {
		delete(s.subs, sub.id)
		close(sub.ch)
		close(sub.done)
	}
	s.mu.Unlock()
}

func mustJSON(value any) json.RawMessage {
	body, err := json.Marshal(value)
	if err != nil {
		return json.RawMessage(`null`)
	}
	return body
}

func describeChatError(err error) (message, code string, retryable bool) {
	switch {
	case err == nil:
		return "", "", false
	case errors.Is(err, domain.ErrChatBusy):
		return "当前对话较多，请稍后再试", "chat_busy", true
	case errors.Is(err, domain.ErrChatUnavailable):
		if msg := domain.SafeMessage(err); msg != "" {
			return msg, "chat_unavailable", true
		}
		return "对话服务暂时不可用，请稍后重试", "chat_unavailable", true
	default:
		return "消息发送失败，请重试", "send_failed", true
	}
}
