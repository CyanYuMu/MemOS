package piagent

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	// 空闲回收的检查周期。TTL 由配置决定，ticker 固定 1 分钟：回收晚一分钟
	// 没有风险，但更频繁地扫只会增加锁竞争。
	idleCheckInterval = time.Minute
	// 准入等待的轮询间隔：有人在几秒内结束一轮就让位，不让请求立刻失败。
	admissionPoll = 50 * time.Millisecond
	// 驱逐时等进程退出的上限；超过就放弃这次驱逐，让调用方返回 busy。
	evictGrace = 3 * time.Second
	// 会话对象本身很轻，但页面会不断创建；长时间没有进程、订阅与队列就回收。
	defaultSessionTTL = 6 * time.Hour
	// 未知 RSS 时的保守预留：一个 pi 进程按 128 MiB 计。
	defaultProcMemory = 128 << 20
	// 会话目录所在盘的准入阈值：低于这个值就不再接收新对话。
	defaultMinFreeDisk = 256 << 20
)

// ChatMetrics 是运行时可选的指标端口。nil 表示不上报，测试与本地运行不必构造。
type ChatMetrics interface {
	SetChatSlots(total, running, idle, stopping int)
	AddChatEviction()
	AddChatBusy()
	ObserveChatAdmission(time.Duration)
	SetChatPiRSS(bytes int64)
}

// Options 是运行时构造参数。
type Options struct {
	Dir           string
	PiBin         string
	MCPURL        string
	ContextWindow int
	MaxTokens     int
	MaxProcs      int
	IdleTTL       time.Duration
	// SystemPrompt 整段替换 pi 的默认 coding assistant 提示。空时用 DefaultSystemPrompt。
	SystemPrompt string
	// Tools 是直接暴露给模型的 MCP 工具名；空时用 DefaultMCPTools。
	Tools []string
	// Metrics 可为空。
	Metrics            ChatMetrics
	ConversationTitles ConversationTitles

	// AdmissionWait 是槽位满时等待一个会话让位的上限；超时返回 busy。
	AdmissionWait time.Duration
	// TurnIdleLimit 是一轮多久没有新事件后开始用 get_state 对账。
	TurnIdleLimit time.Duration
	// TurnHardLimit 是对账仍显示在生成时的强杀阈值。
	TurnHardLimit time.Duration
	// SessionTTL 是没有进程、没有订阅、没有队列的会话对象保留时长。
	SessionTTL time.Duration
	// ProcMemoryBudget 是准入前要求的内存余量（字节）；0 用默认 128MiB。
	ProcMemoryBudget int64
	// ReapInterval 是空闲回收与看门狗的扫描周期；0 用默认 1 分钟。
	ReapInterval time.Duration
	// ExtraEnv 是额外注入给子进程的环境变量（测试与特殊部署用）；仍走白名单之外
	// 的显式通道，不会被宿主环境自动继承。
	ExtraEnv []string
	// MinFreeDisk 是准入前要求会话目录所在盘的可用空间；0 用默认 256MiB。
	// 磁盘写满会让 transcript 与快照写不进去，宁可先拒绝也不留半截数据。
	MinFreeDisk int64
}

// DefaultSystemPrompt 用可独立维护的人格文档替换编码助手默认提示。
//
//go:embed soul.md
var DefaultSystemPrompt string

// Runtime 管理全部会话与它们的进程。一个进程只服务一个 session；会话对象常驻，
// 进程可被空闲回收或按 LRU 驱逐。
type Runtime struct {
	opts Options
	log  *slog.Logger

	mu       sync.Mutex
	sessions map[string]*Session
	agents   map[string]*Agent
	starting map[string]*spawnCall
	// stopping 是正在被停止/驱逐的会话。它仍然占一个槽位，直到进程真正退出，
	// 否则并发驱逐会把进程数顶过 MaxProcs，内存先于逻辑出错。
	stopping map[string]struct{}

	stop       chan struct{}
	stopOnce   sync.Once
	stopped    chan struct{}
	sessionOps sync.WaitGroup
}

// spawnCall 让同一会话的并发 spawn 共享一次结果，避免起两个进程。
type spawnCall struct {
	done  chan struct{}
	agent *Agent
	err   error
}

// New 构造运行时并启动回收循环。Dir 会在此时创建。
func New(opts Options, log *slog.Logger) (*Runtime, error) {
	prepared, err := prepareOptions(opts)
	if err != nil {
		return nil, err
	}
	if log == nil {
		log = slog.Default()
	}
	dir, err := filepath.Abs(prepared.Dir)
	if err != nil {
		return nil, fmt.Errorf("piagent: resolve dir: %w", err)
	}
	prepared.Dir = dir
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("piagent: create dir: %w", err)
	}
	r := &Runtime{
		opts:     prepared,
		log:      log,
		sessions: make(map[string]*Session),
		agents:   make(map[string]*Agent),
		starting: make(map[string]*spawnCall),
		stopping: make(map[string]struct{}),
		stop:     make(chan struct{}),
		stopped:  make(chan struct{}),
	}
	// 启动时把会话目录与剩余空间打出来：transcript 和快照都在这块盘上，
	// 容量问题要在日志里能第一眼看到，而不是等写失败。
	attrs := []any{slog.String("dir", dir), slog.Int64("min_free_disk", prepared.MinFreeDisk)}
	if free, ok := diskFree(dir); ok {
		attrs = append(attrs, slog.Int64("free_disk", free))
		if free < prepared.MinFreeDisk {
			log.Warn("chat storage is below the admission threshold", attrs...)
		} else {
			log.Info("chat storage ready", attrs...)
		}
	} else {
		log.Info("chat storage ready (free space unknown)", attrs...)
	}
	go r.reapLoop()
	return r, nil
}

func minFreeDiskOrDefault(v int64) int64 {
	if v == 0 {
		return defaultMinFreeDisk
	}
	return v
}

// prepareOptions 校验必填项并补齐默认值。测试会覆盖超时类默认值。
func prepareOptions(opts Options) (Options, error) {
	if strings.TrimSpace(opts.Dir) == "" {
		return Options{}, errors.New("piagent: Dir 不能为空")
	}
	if strings.TrimSpace(opts.PiBin) == "" {
		return Options{}, errors.New("piagent: PiBin 不能为空")
	}
	if strings.TrimSpace(opts.MCPURL) == "" {
		return Options{}, errors.New("piagent: MCPURL 不能为空")
	}
	if opts.ContextWindow < 1 || opts.MaxTokens < 1 {
		return Options{}, errors.New("piagent: ContextWindow 与 MaxTokens 必须为正数")
	}
	if opts.MaxProcs < 1 {
		return Options{}, errors.New("piagent: MaxProcs 必须为正数")
	}
	if opts.IdleTTL <= 0 {
		return Options{}, errors.New("piagent: IdleTTL 必须为正数")
	}
	if opts.AdmissionWait < 0 || opts.TurnIdleLimit < 0 || opts.TurnHardLimit < 0 ||
		opts.SessionTTL < 0 || opts.ReapInterval < 0 || opts.ProcMemoryBudget < 0 {
		return Options{}, errors.New("piagent: 等待、看门狗与资源预算不能为负")
	}
	if opts.AdmissionWait == 0 {
		opts.AdmissionWait = 5 * time.Second
	}
	if opts.TurnIdleLimit == 0 {
		opts.TurnIdleLimit = 10 * time.Minute
	}
	if opts.TurnHardLimit == 0 {
		opts.TurnHardLimit = 20 * time.Minute
	}
	if opts.SessionTTL == 0 {
		opts.SessionTTL = defaultSessionTTL
	}
	if opts.ReapInterval == 0 {
		opts.ReapInterval = idleCheckInterval
	}
	if opts.ProcMemoryBudget == 0 {
		opts.ProcMemoryBudget = defaultProcMemory
	}
	opts.MinFreeDisk = minFreeDiskOrDefault(opts.MinFreeDisk)
	if strings.TrimSpace(opts.SystemPrompt) == "" {
		opts.SystemPrompt = DefaultSystemPrompt
	}
	if len(opts.Tools) == 0 {
		opts.Tools = append([]string(nil), DefaultMCPTools...)
	}
	for _, name := range opts.Tools {
		if !knownTool(name) {
			return Options{}, fmt.Errorf("piagent: 未知工具 %q", name)
		}
	}
	return opts, nil
}

// Available 报告 pi 二进制是否存在且可执行。每次调用都重新检查：二进制可能在
// 部署过程中被替换，缓存失败结果会让整个进程生命周期都看不到它。
func (r *Runtime) Available() bool {
	_, err := resolveBinary(r.opts.PiBin)
	return err == nil
}

// Session 返回会话对象；不存在时创建，但不会启动进程。
func (r *Runtime) Session(ctx context.Context, spec domain.ChatSessionSpec) (domain.ChatSession, error) {
	r.mu.Lock()
	select {
	case <-r.stop:
		r.mu.Unlock()
		return nil, unavailable("对话服务正在重启，请稍后重试", nil)
	default:
	}
	r.sessionOps.Add(1)
	r.mu.Unlock()
	defer r.sessionOps.Done()
	if err := validateSpec(spec); err != nil {
		return nil, err
	}
	dirs := dirsForSpec(r.opts.Dir, spec)
	if err := os.MkdirAll(dirs.runtime, 0o700); err != nil {
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}
	// 快路径：已有可用会话直接复用。慢路径（新建）里的读盘在 r.mu 外完成，
	// 避免一个大会话拖住所有会话的准入/退出记账。
	r.mu.Lock()
	s := r.sessions[spec.SessionID]
	if s != nil && s.isClosed() {
		s = nil
	}
	r.mu.Unlock()
	if s != nil {
		// 项目 Key / 模型配置可能变了；会话对象按 sessionId 常驻，必须把新配置
		// 带进去，否则换 Key 或吊销要等对象空闲回收（最长 6h）才生效。
		r.refreshSpec(ctx, s, spec)
	}

	if s == nil {
		//nolint:contextcheck // 会话与派发 goroutine 的生命周期长于请求 ctx，不能用请求 ctx
		candidate := newSession(r, spec, dirs, r.opts.ContextWindow, r.log)
		candidate.loadRetry()
		r.mu.Lock()
		if existing := r.sessions[spec.SessionID]; existing != nil && !existing.isClosed() {
			s = existing
		} else {
			r.sessions[spec.SessionID] = candidate
			s = candidate
		}
		r.mu.Unlock()
		if s != candidate {
			candidate.close()
		}
	}
	s.touch()
	return s, nil
}

// Stop 停止并忘记会话进程；不存在时是无操作。spawn 进行中时等它落地再停，
// 否则配置变更会留下一个用旧配置跑起来的进程。
func (r *Runtime) Stop(ctx context.Context, sessionID string) error {
	r.mu.Lock()
	agent := r.agents[sessionID]
	call := r.starting[sessionID]
	r.mu.Unlock()

	if call != nil {
		select {
		case <-call.done:
		case <-ctx.Done():
			return ctx.Err()
		case <-r.stop:
		}
		r.mu.Lock()
		agent = r.agents[sessionID]
		r.mu.Unlock()
	}
	if agent == nil {
		return nil
	}
	// 停止进程会 abort 当前轮：先摘队头进待重试并暂停派发，停完再恢复派发。
	if sess := r.session(sessionID); sess != nil {
		sess.beginRestart()
		defer sess.endRestart()
	}
	return agent.stop(ctx)
}

// StopTenant 停掉某项目下所有账号的会话。项目级配置与 Key 是共享的，
// 只停保存者自己的进程会让其他在线成员继续用旧配置直到空闲回收。
func (r *Runtime) StopTenant(ctx context.Context, tenantID string) error {
	r.mu.Lock()
	sessions := make([]*Session, 0, 4)
	for _, s := range r.sessions {
		if s.tenantID == tenantID {
			sessions = append(sessions, s)
		}
	}
	r.mu.Unlock()

	var wg sync.WaitGroup
	errs := make([]error, len(sessions))
	for i, s := range sessions {
		wg.Add(1)
		go func(i int, s *Session) {
			defer wg.Done()
			// 先在途 spawn 也要等：配置变更后不能留下一个用旧 model 跑起来的进程。
			if call := r.startingCall(s.id); call != nil {
				if _, err := r.waitSpawn(ctx, call); err != nil {
					errs[i] = err
					return
				}
			}
			// 同 Stop：先收尾在途轮次并暂停派发，停完再恢复。
			s.beginRestart()
			defer s.endRestart()
			if agent := s.currentAgent(); agent != nil {
				errs[i] = agent.stop(ctx)
			}
		}(i, s)
	}
	wg.Wait()
	return errors.Join(errs...)
}

// Close 停止回收循环与全部进程。可重复调用。
func (r *Runtime) Close() error {
	r.stopOnce.Do(func() { r.mu.Lock(); close(r.stop); r.mu.Unlock() })
	<-r.stopped
	r.sessionOps.Wait()

	r.mu.Lock()
	agents := make([]*Agent, 0, len(r.agents))
	for id, a := range r.agents {
		agents = append(agents, a)
		delete(r.agents, id)
	}
	sessions := make([]*Session, 0, len(r.sessions))
	for _, s := range r.sessions {
		sessions = append(sessions, s)
	}
	r.sessions = make(map[string]*Session)
	r.mu.Unlock()

	for _, s := range sessions {
		// 先把在途轮次记成待重试，再停进程：关闭是优雅 abort，agent 会推
		// agent_settled，那条消息就会被记成 done，用户只看到半截回答。
		s.interruptForRestart()
		s.close()
	}

	var wg sync.WaitGroup
	errs := make([]error, len(agents))
	for i, a := range agents {
		wg.Add(1)
		go func(i int, a *Agent) {
			defer wg.Done()
			errs[i] = a.stop(context.Background())
		}(i, a)
	}
	wg.Wait()
	for _, s := range sessions {
		s.workers.Wait()
	}
	return errors.Join(errs...)
}

// SweepOrphans 回收上一代进程遗留的 `pi --mode rpc`。只有 linux 实现会真的扫描
// /proc，其他平台是无操作。
func (r *Runtime) SweepOrphans() {
	n := sweepOrphans()
	if n > 0 {
		r.log.Info("reclaimed orphan pi processes", slog.Int("count", n))
	}
}

// ensureAgent 是唯一的准入点：已有进程直接复用；槽位满时先驱逐空闲会话，
// 全部在生成时才返回 busy。
func (r *Runtime) ensureAgent(ctx context.Context, s *Session) (*Agent, error) {
	started := time.Now()
	defer r.observeAdmission(started)

	// 换配置与准入可能交错：旧代次拉起的进程在注册时被拒绝（errSpecStale），
	// 按新配置再走一遍准入即可，第二轮就拿到新进程。
	for range 2 {
		agent, err := r.admitAgent(ctx, s)
		if !errors.Is(err, errSpecStale) {
			return agent, err
		}
	}
	return nil, unavailable("对话配置刚刚变更，请重新发送消息", errSpecStale)
}

func (r *Runtime) admitAgent(ctx context.Context, s *Session) (*Agent, error) {
	id := s.id
	for range 4 {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if agent, done, err := r.reuseAgent(ctx, id); done {
			return agent, err
		}
		if r.evictForRoom(ctx, id) {
			continue
		}
		if !r.memoryOK() {
			r.addBusy()
			r.log.WarnContext(ctx, "chat admission rejected: memory headroom below one process budget",
				slog.Int64("budget", r.opts.ProcMemoryBudget))
			return nil, busy()
		}
		if !r.diskOK() {
			r.log.WarnContext(ctx, "chat admission rejected: free disk below threshold",
				slog.Int64("threshold", r.opts.MinFreeDisk))
			return nil, unavailable("对话服务存储空间不足，请稍后重试", nil)
		}
		agent, err := r.spawnAndRegister(ctx, s)
		if !errors.Is(err, errNoSlot) {
			return agent, err
		}
		if err := r.waitForSlot(ctx, r.opts.AdmissionWait); err != nil {
			r.addBusy()
			return nil, err
		}
	}
	r.addBusy()
	return nil, busy()
}

// errNoSlot 表示容量已满，调用方可以等待或驱逐后重试。
var errNoSlot = errors.New("piagent: no chat slot available")

// errSpecStale 表示进程是用已被替换的配置拉起来的：它已经被停掉，调用方应按
// 当前配置重新准入。
var errSpecStale = errors.New("piagent: chat spec changed during spawn")

// reuseAgent 返回可复用的进程或正在进行的 spawn；done=false 表示需要继续准入。
func (r *Runtime) reuseAgent(ctx context.Context, id string) (*Agent, bool, error) {
	r.mu.Lock()
	if agent := r.agents[id]; agent != nil && !agent.exited() && !agent.stopping.Load() {
		r.mu.Unlock()
		return agent, true, nil
	}
	if call := r.starting[id]; call != nil {
		r.mu.Unlock()
		agent, err := r.waitSpawn(ctx, call)
		return agent, true, err
	}
	r.mu.Unlock()
	return nil, false, nil
}

// evictForRoom 在满槽时驱逐一个最久空闲的会话。返回 true 表示已经腾出槽位，
// 调用方应重试准入。
func (r *Runtime) evictForRoom(ctx context.Context, id string) bool {
	r.mu.Lock()
	if r.totalLocked() < r.opts.MaxProcs {
		r.mu.Unlock()
		return false
	}
	victim := r.pickVictimLocked(id)
	if victim == nil {
		r.mu.Unlock()
		return false
	}
	r.stopping[id] = struct{}{}
	r.mu.Unlock()

	r.log.InfoContext(ctx, "evicting idle chat session",
		slog.String("session_id", id),
		slog.String("victim", victim.id))
	r.addEviction()
	evictCtx, cancel := context.WithTimeout(ctx, evictGrace)
	victim.evict(evictCtx)
	cancel()

	r.mu.Lock()
	delete(r.stopping, id)
	full := r.totalLocked() >= r.opts.MaxProcs
	r.mu.Unlock()
	r.observeSlots()
	// 驱逐失败（进程没退出）时槽位仍被占用，让调用方回到 waitForSlot。
	return !full
}

// hasRoomFor 判断这个会话现在能不能起进程：还有空位，或者能驱逐一个空闲会话。
// 只用于发消息前的同步预检——真正的准入仍以 ensureAgent 为准。
func (r *Runtime) hasRoomFor(id string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	// 这个会话自己已经有进程（或正在起）就不占新槽位：下一轮直接复用。
	if r.agents[id] != nil || r.starting[id] != nil {
		return true
	}
	if r.totalLocked() < r.opts.MaxProcs {
		return true
	}
	if victim := r.pickVictimLocked(id); victim != nil {
		return true
	}
	return false
}

func (r *Runtime) spawnAndRegister(ctx context.Context, s *Session) (*Agent, error) {
	id := s.id
	// 占位与容量判断必须在同一个临界区里完成，否则并发准入会一起读到
	// 「还有空位」然后同时 spawn，把 MaxProcs 冲掉。
	call := &spawnCall{done: make(chan struct{})}
	r.mu.Lock()
	if r.totalLocked() >= r.opts.MaxProcs {
		r.mu.Unlock()
		return nil, errNoSlot
	}
	r.starting[id] = call
	r.mu.Unlock()

	spec, gen := s.specSnapshot()
	agent, err := r.spawn(ctx, spec, s)

	r.mu.Lock()
	delete(r.starting, id)
	call.agent, call.err = agent, err
	shuttingDown := false
	select {
	case <-r.stop:
		shuttingDown = true
	default:
	}
	if err == nil {
		switch {
		case s.specGeneration() != gen:
			// 这段等待期间配置被换掉了：进程用的是旧凭据，不能成为当前进程。
			// 读代次与 applySpec 之间仍有极窄的窗口，这时进程会短暂注册；它拿不到
			// prompt（beginRestart 已把队头摘走，派发复查会看到 head 为空）。
			call.err = errSpecStale
		case agent.exited():
			call.err = unavailable("对话服务启动失败，请稍后重试", nil)
		case shuttingDown:
			// Close 已经遍历过 agents；现在加入 map 就再也没人停了。
			call.err = unavailable("对话服务正在重启，请稍后重试", nil)
		default:
			r.agents[id] = agent
		}
	}
	close(call.done)
	r.mu.Unlock()

	if agent != nil && call.err != nil {
		// 尚未注册的进程从未收到prompt，无需等待abort落盘；停机取消握手后直接回收。
		stopCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), stopGrace)
		agent.killWait(stopCtx)
		cancel()
	}
	if call.err == nil {
		s.setAgent(agent)
	}
	r.observeSlots()
	return call.agent, call.err
}

// refreshSpec 把最新配置带进已有会话。凭据/模型变了就暂停派发、停掉旧进程，
// 再让下一轮按新配置重建。在途 spawn 只能尽力等待（best-effort）：正确的保证来自
// 注册时的代次校验，等待失败也不会让旧配置的进程接手。
func (r *Runtime) refreshSpec(ctx context.Context, s *Session, spec domain.ChatSessionSpec) {
	current, _ := s.specSnapshot()
	if sameSpec(current, spec) {
		return
	}
	r.log.InfoContext(ctx, "chat config changed; restarting process", slog.String("session_id", s.id))
	s.beginRestart()
	defer s.endRestart()
	// 在途 spawn 用的一定是旧配置：applySpec 会把代次推高，它注册时会被拒绝并
	// 停掉（见 spawnAndRegister）。这里等它落地只是少留一个正在起的老进程，
	// 等不到也不影响正确性。
	if call := r.startingCall(s.id); call != nil {
		if _, err := r.waitSpawn(ctx, call); err != nil {
			r.log.DebugContext(ctx, "chat spawn wait interrupted during config refresh",
				slog.String("session_id", s.id), slog.Any("err", err))
		}
	}
	r.stopCurrentAgent(ctx, s)
	s.applySpec(spec)
	// 「停旧进程」与「换配置」之间落地并注册的进程仍属于旧代次，这里复查一次。
	r.stopCurrentAgent(ctx, s)
}

// stopCurrentAgent 停掉会话当前的进程；没有进程或已经退出时是无操作。
func (r *Runtime) stopCurrentAgent(ctx context.Context, s *Session) {
	agent := s.currentAgent()
	if agent == nil || agent.exited() {
		return
	}
	stopCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), evictGrace)
	_ = agent.stop(stopCtx)
	cancel()
}

func sameSpec(a, b domain.ChatSessionSpec) bool {
	return a.APIKey == b.APIKey && a.BaseURL == b.BaseURL &&
		a.Model == b.Model && a.ProjectKey == b.ProjectKey
}

func (r *Runtime) session(id string) *Session {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sessions[id]
}

func (r *Runtime) startingCall(id string) *spawnCall {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.starting[id]
}

// waitSpawn 等另一个 goroutine 把同一会话拉起来。
func (r *Runtime) waitSpawn(ctx context.Context, call *spawnCall) (*Agent, error) {
	select {
	case <-call.done:
		return call.agent, call.err
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-r.stop:
		return nil, unavailable("对话服务正在重启，请稍后重试", nil)
	}
}

// waitForSlot 等待任一会话让出槽位；超时即 busy。
func (r *Runtime) waitForSlot(ctx context.Context, wait time.Duration) error {
	deadline := time.Now().Add(wait)
	ticker := time.NewTicker(admissionPoll)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		full := r.totalLocked() >= r.opts.MaxProcs
		r.mu.Unlock()
		if !full {
			return nil
		}
		if time.Now().After(deadline) {
			return busy()
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-r.stop:
			return unavailable("对话服务正在重启，请稍后重试", nil)
		case <-ticker.C:
		}
	}
}

func (r *Runtime) totalLocked() int {
	return len(r.agents) + len(r.starting) + len(r.stopping)
}

// pickVictimLocked 选最久空闲、没有在途轮次的会话。调用方持有 r.mu。
func (r *Runtime) pickVictimLocked(requester string) *Session {
	var best *Session
	var bestAge time.Duration
	for id := range r.agents {
		if id == requester {
			continue
		}
		s := r.sessions[id]
		if s == nil || !s.evictable() {
			continue
		}
		age := s.idleFor()
		if best == nil || age > bestAge {
			best, bestAge = s, age
		}
	}
	return best
}

// diskOK 检查会话目录还有多少空间。transcript 与快照都写在这块盘上，
// 写满后再受理只会留下写不进去的半截数据。
func (r *Runtime) diskOK() bool {
	if r.opts.MinFreeDisk <= 0 {
		return true
	}
	free, ok := diskFree(r.opts.Dir)
	if !ok {
		return true
	}
	return free >= r.opts.MinFreeDisk
}

func (r *Runtime) memoryOK() bool {
	if r.opts.ProcMemoryBudget <= 0 {
		return true
	}
	available, _, ok := memoryAvailable()
	if !ok {
		return true
	}
	return available >= r.opts.ProcMemoryBudget
}

// spawn 准备目录与配置文件、取会话锁、启动进程并完成握手。
func (r *Runtime) spawn(ctx context.Context, spec domain.ChatSessionSpec, s *Session) (*Agent, error) {
	bin, err := resolveBinary(r.opts.PiBin)
	if err != nil {
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}
	dirs := dirsForSpec(r.opts.Dir, spec)
	for _, dir := range []string{dirs.agent, dirs.sessions, dirs.cwd, dirs.runtime} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
		}
	}

	lock, err := r.acquireSessionLock(ctx, dirs.lockPath(spec.SessionID))
	if err != nil {
		return nil, err
	}
	locked := true
	defer func() {
		if locked {
			lock.release()
		}
	}()

	if err := writeModelsConfig(dirs.agent, spec, r.opts); err != nil {
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}
	if err := writeMCPConfig(dirs.agent, r.opts.MCPURL, spec.ProjectKey, r.opts.Tools); err != nil {
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}

	systemPrompt := r.opts.SystemPrompt
	if spec.ConversationID != "" {
		systemPrompt += titleInstruction
	}
	args := []string{
		"--mode", "rpc",
		"--no-extensions",
		"--no-builtin-tools",
		// MCP resource 工具与记忆无关，默认 direct+active，会额外占用模型的工具位。
		"--exclude-tools", "list_mcp_resources,list_mcp_resource_templates,read_mcp_resource",
		"--no-approve",
		"--session-dir", dirs.sessions,
		"--session-id", spec.SessionID,
		// 整段替换 pi 的 coding assistant 提示，避免暴露其本地文档路径与人设。
		"--system-prompt", systemPrompt,
	}
	// 用 WithoutCancel：进程生命周期不跟着单次请求结束。
	cmd := exec.CommandContext(context.WithoutCancel(ctx), bin, args...)
	cmd.Dir = dirs.cwd
	extraEnv := append([]string{
		"PI_CODING_AGENT_DIR=" + dirs.agent,
		"PI_DISABLE_SIDECAR_PREWARM=1",
		"PI_BYOK_KEY=" + spec.APIKey,
		"MEMOS_MCP_TOKEN=" + spec.ProjectKey,
		"GRAPH_MEMORY_CHAT_SESSION_ID=" + spec.SessionID,
	}, r.opts.ExtraEnv...)
	cmd.Env = childEnv(extraEnv...)
	cmd.SysProcAttr = sysProcAttr()

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}
	// 自己建管道而不是用 StdoutPipe/StderrPipe：cmd.Wait 会在进程退出时关闭
	// StdoutPipe 的读端，可能丢掉最后几行事件。自建管道由读 goroutine 关。
	stdout, stdoutW, err := os.Pipe()
	if err != nil {
		_ = stdin.Close()
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}
	stderr, stderrW, err := os.Pipe()
	if err != nil {
		_ = stdin.Close()
		_ = stdout.Close()
		_ = stdoutW.Close()
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}
	cmd.Stdout = stdoutW
	cmd.Stderr = stderrW
	if err := cmd.Start(); err != nil {
		_ = stdin.Close()
		_ = stdout.Close()
		_ = stdoutW.Close()
		_ = stderr.Close()
		_ = stderrW.Close()
		return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
	}
	_ = stdoutW.Close()
	_ = stderrW.Close()

	var a *Agent
	a = newAgent(cmd, stdin, stdout, stderr, spec, r.log, lock,
		func(eventType string, data json.RawMessage) { s.onAgentEventFrom(a, eventType, data) },
		r.onAgentExit)
	go a.readStdout()
	go a.readStderr()
	go a.wait(ctx)
	// 进程已经接管锁；后续失败路径由 shutdown 释放。
	locked = false

	// 握手跟随会话派发生命周期，停机取消时也必须结束。外层给内层的分步
	// 重试留余量（两条命令各 4 次 × 5s），否则内层重试还没用完外层就先超时了。
	handshakeCtx, cancel := context.WithTimeout(ctx, 2*handshakeBudget)
	defer cancel()
	a.handshake(handshakeCtx)
	if a.exited() {
		_ = a.stop(context.WithoutCancel(ctx))
		return nil, unavailable("对话服务启动失败，请稍后重试", nil)
	}
	return a, nil
}

// acquireSessionLock 取会话排他锁。锁被占用通常意味着上一代进程还没退出：
// 在 evictGrace 内每 100ms 重试一次，仍失败就如实告诉用户「正在恢复」。
//
// 这里刻意不做定向孤儿清扫：锁被一个仍然活着的进程持有时，盲目 kill 会打断
// 一个正在写会话的进程。上一代残留由启动时的 SweepOrphans 统一回收。
func (r *Runtime) acquireSessionLock(ctx context.Context, path string) (*fileLock, error) {
	// 旧进程优雅停止时要先 abort 再等 300ms 才 kill；只等 200ms 会在换配置/
	// 重启后几乎必然撞锁。这里按「最多等一个 evictGrace」轮询。
	deadline := time.Now().Add(evictGrace)
	for {
		lock, err := acquireFileLock(path)
		if err == nil {
			return lock, nil
		}
		if !errors.Is(err, errLockBusy) {
			return nil, unavailable("对话服务暂时不可用，请稍后重试", err)
		}
		if time.Now().After(deadline) {
			return nil, unavailable("会话正在恢复，请稍后重试", err)
		}
		select {
		case <-ctx.Done():
			return nil, unavailable("对话服务暂时不可用，请稍后重试", ctx.Err())
		case <-r.stop:
			return nil, unavailable("对话服务正在重启，请稍后重试", nil)
		case <-time.After(100 * time.Millisecond):
		}
	}
}

func (r *Runtime) onAgentExit(a *Agent) {
	r.mu.Lock()
	if r.agents[a.sessionID] == a {
		delete(r.agents, a.sessionID)
	}
	s := r.sessions[a.sessionID]
	r.mu.Unlock()
	if s != nil {
		s.agentExited(a)
	}
	r.observeSlots()
}

// reapLoop 周期性做三件事：回收空闲进程、对账卡住的轮次、回收无用会话对象。
func (r *Runtime) reapLoop() {
	defer close(r.stopped)
	ticker := time.NewTicker(r.opts.ReapInterval)
	defer ticker.Stop()
	for {
		select {
		case <-r.stop:
			return
		case <-ticker.C:
			r.reapOnce()
		}
	}
}

func (r *Runtime) reapOnce() {
	r.mu.Lock()
	sessions := make([]*Session, 0, len(r.sessions))
	for _, s := range r.sessions {
		sessions = append(sessions, s)
	}
	r.mu.Unlock()

	var totalRSS int64
	for _, s := range sessions {
		// 每个周期采一次子进程常驻内存：容量预算（MaxProcs × P95 RSS）要用
		// 真实值而不是估算值，日志留给事后对齐。
		if agent := s.currentAgent(); agent != nil && !agent.exited() {
			rss := processRSS(agent.pid())
			totalRSS += rss
			r.log.Debug("chat process memory",
				slog.String("session_id", s.id), slog.Int64("rss_bytes", rss))
		}
		if s.turnStalled() >= r.opts.TurnIdleLimit {
			r.reconcileStalled(s)
		}
		if s.evictable() && s.idleFor() >= r.opts.IdleTTL {
			r.log.Debug("reaping idle chat session", slog.String("session_id", s.id))
			ctx, cancel := context.WithTimeout(context.Background(), evictGrace)
			s.evict(ctx)
			cancel()
			continue
		}
		if s.reapIfIdle(r.opts.SessionTTL) {
			r.mu.Lock()
			if r.sessions[s.id] == s {
				delete(r.sessions, s.id)
			}
			r.mu.Unlock()
			s.close()
		}
	}
	if r.opts.Metrics != nil {
		r.opts.Metrics.SetChatPiRSS(totalRSS)
	}
}

// reconcileStalled 处理「事件流里丢了 agent_settled」：先用 get_state 对账，
// 确认不在生成就按正常结束收尾；仍显示在生成且超过硬上限则杀掉。
func (r *Runtime) reconcileStalled(s *Session) {
	agent := s.currentAgent()
	if agent == nil || agent.exited() {
		return
	}
	// 对账期间这一轮可能已经正常结束、甚至下一条已经在生成：记下当时的队头，
	// 只收尾它，别把新一轮当成卡住的旧一轮 settle 掉。
	headID := ""
	s.mu.Lock()
	if s.head != nil {
		headID = s.head.ID
	}
	s.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), stateTimeout)
	state, err := agent.getState(ctx)
	cancel()
	if err == nil && !state.Streaming && !state.Compacting {
		r.log.Info("chat turn finished without agent_settled; reconciling",
			slog.String("session_id", s.id))
		s.settleHead(headID)
		return
	}
	if s.turnAge() >= r.opts.TurnHardLimit {
		r.log.Warn("chat turn exceeded hard limit; killing process",
			slog.String("session_id", s.id))
		// 强杀前先把这一轮放进 needs_retry：用户必须知道那句话没有执行完，
		// 而不是只看到状态回到 idle。
		s.markHeadRetry(headID, "这一轮执行时间过长，已中断，请重试")
		evictCtx, cancel := context.WithTimeout(context.Background(), evictGrace)
		agent.killWait(evictCtx)
		cancel()
	}
}

func validateSpec(spec domain.ChatSessionSpec) error {
	switch {
	case strings.TrimSpace(spec.SessionID) == "":
		return errors.New("piagent: session id 不能为空")
	case strings.TrimSpace(spec.TenantID) == "":
		return errors.New("piagent: tenant id 不能为空")
	case strings.TrimSpace(spec.BaseURL) == "":
		return errors.New("piagent: base url 不能为空")
	case strings.TrimSpace(spec.Model) == "":
		return errors.New("piagent: model 不能为空")
	case spec.APIKey == "":
		return errors.New("piagent: api key 不能为空")
	case spec.ProjectKey == "":
		return errors.New("piagent: project key 不能为空")
	}
	return nil
}

func unavailable(message string, err error) error {
	return domain.Wrap(domain.ErrChatUnavailable, "piagent", message, err)
}

func busy() error {
	return domain.Wrap(domain.ErrChatBusy, "piagent", "当前对话较多，请稍后再试", nil)
}

// resolveBinary 在 PiBin 是裸命令时走 PATH，否则要求文件存在且有执行位。
func resolveBinary(bin string) (string, error) {
	if !strings.ContainsRune(bin, os.PathSeparator) {
		return exec.LookPath(bin)
	}
	info, err := os.Stat(bin)
	if err != nil {
		return "", err
	}
	if info.IsDir() || info.Mode()&0o111 == 0 {
		return "", fmt.Errorf("piagent: %s 不是可执行文件", bin)
	}
	return bin, nil
}

// childEnv 复制宿主环境并剔除会影响 pi 会话定位的变量。直接 append 会让宿主
// 已有的同名变量排在前面，POSIX getenv 取第一个，子进程就会用到错误的 agent 目录
// 或上一代的 key。
//
// childEnv 给 pi 子进程组装环境变量。
//
// 白名单而不是黑名单：pi 是外部二进制，控制面环境里有平台网关 Key、管理员口令、
// 数据库与 Redis 地址；任何一个崩溃转储或未来的调试输出都会把它们带出去。pi 真正
// 需要的只有可执行路径、HOME、语言、时区、临时目录，以及出网用的代理与 CA。
//
//nolint:forbidigo // 子进程要继承 PATH/HOME 等基础变量，配置包管不到子进程环境
func childEnv(extra ...string) []string {
	allowed := map[string]bool{
		"PATH": true, "HOME": true, "USER": true, "LOGNAME": true,
		"LANG": true, "LC_ALL": true, "LC_CTYPE": true, "TZ": true,
		"TMPDIR": true, "TEMP": true, "TMP": true,
		"SSL_CERT_FILE": true, "SSL_CERT_DIR": true, "CURL_CA_BUNDLE": true,
		"HTTP_PROXY": true, "HTTPS_PROXY": true, "NO_PROXY": true,
		"http_proxy": true, "https_proxy": true, "no_proxy": true,
	}
	env := make([]string, 0, len(allowed)+len(extra))
	for _, kv := range os.Environ() {
		key, _, _ := strings.Cut(kv, "=")
		if allowed[key] {
			env = append(env, kv)
		}
	}
	return append(env, extra...)
}

func (r *Runtime) observeAdmission(start time.Time) {
	if r.opts.Metrics == nil {
		return
	}
	r.opts.Metrics.ObserveChatAdmission(time.Since(start))
}

func (r *Runtime) addEviction() {
	if r.opts.Metrics != nil {
		r.opts.Metrics.AddChatEviction()
	}
}

func (r *Runtime) addBusy() {
	if r.opts.Metrics != nil {
		r.opts.Metrics.AddChatBusy()
	}
}

// observeSlots 上报槽位分布。idle 用 total 减 running/stopping 近似：它表示
// 「占着进程但没在生成」，正是驱逐的候选集合。
func (r *Runtime) observeSlots() {
	if r.opts.Metrics == nil {
		return
	}
	r.mu.Lock()
	total := len(r.agents) + len(r.starting) + len(r.stopping)
	stopping := len(r.stopping)
	running := 0
	for id := range r.agents {
		s := r.sessions[id]
		if s == nil {
			continue
		}
		s.mu.Lock()
		if s.running || s.compacting {
			running++
		}
		s.mu.Unlock()
	}
	r.mu.Unlock()

	idle := max(total-running-stopping, 0)
	r.opts.Metrics.SetChatSlots(total, running, idle, stopping)
}
