package memory

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
)

// Upstream 是 memos-api 的端口，由 adapter/upstream 实现。
type Upstream interface {
	Add(ctx context.Context, req domain.AddPayload) (domain.AddResult, error)
	Search(ctx context.Context, q domain.SearchQuery) (domain.SearchResult, error)
	Profile(ctx context.Context, q domain.ProfileQuery) (domain.ProfileResult, error)
	OwnedMemory(ctx context.Context, tenantID, userID, memoryID string) (domain.Memory, bool, error)
	OwnedMemories(ctx context.Context, tenantID, userID string, ids []string) (map[string]bool, error)
	Owners(ctx context.Context, ids []string) (map[string]string, error)
	// Delete 删除当前租户的记忆；writable_cube_ids 是上游的租户作用域，缺省会拒绝
	// 按 id 删除（共享图库没有默认 cube）。
	Delete(ctx context.Context, tenantID string, ids []string) error
	Feedback(ctx context.Context, req domain.FeedbackRequest) (domain.FeedbackResult, error)
}

// Jobs 是异步任务端口，由 store 实现（经 worker 消费）。
type Jobs interface {
	EnqueueJob(ctx context.Context, j domain.Job) error
}

// Idempotency 是请求去重端口，由 store 实现。
type Idempotency interface {
	ClaimIdempotency(ctx context.Context, rec domain.IdempotencyRecord) (domain.IdempotencyRecord, bool, error)
	CompleteIdempotency(ctx context.Context, p domain.IdempotencyOutcome) error
	AcceptIdempotency(ctx context.Context, p domain.IdempotencyOutcome) error
	SucceedIdempotency(ctx context.Context, tenantID, key string, at time.Time) error
	ReleaseIdempotency(ctx context.Context, tenantID, key string) error
}

// Observer 记录用例结果；由 internal/platform/metrics 实现，nil 表示不记录。
type Observer interface {
	ObserveMemory(op string, err error)
}

// Options 是记忆服务的配置。
type Options struct {
	DefaultTopK int
	MaxTopK     int
	QueryPrefix string
	DreamTopK   int
	AddAsync    bool
	Idem        Idempotency // 为 nil 时不启用幂等
	Observer    Observer
	Events      EventStore
}

const (
	defaultTopK = 10
	// 上游契约 v2 的 top_k 上限；超过会让整个检索请求 422。
	defaultMaxTopK       = 25
	defaultPageSize      = 20
	maxIdempotencyKeyLen = 128
	autoKeyPrefix        = "auto:"
)

// Service 实现 6 个记忆用例。
type Service struct {
	up          Upstream
	jobs        Jobs
	opts        Options
	log         *slog.Logger
	updateLocks [64]chan struct{}
}

// observe 记录一次用例的最终结果。err 用指针传入，defer 才能拿到最终返回值。
func (s *Service) observe(op string, err *error) {
	if s.opts.Observer != nil {
		s.opts.Observer.ObserveMemory(op, *err)
	}
}

// New 构造记忆服务。
func New(up Upstream, jobs Jobs, opts Options, log *slog.Logger) *Service {
	if opts.DefaultTopK <= 0 {
		opts.DefaultTopK = defaultTopK
	}
	if opts.MaxTopK <= 0 {
		opts.MaxTopK = defaultMaxTopK
	}
	if opts.DefaultTopK > opts.MaxTopK {
		opts.DefaultTopK = opts.MaxTopK
	}
	if log == nil {
		log = slog.Default()
	}
	s := &Service{up: up, jobs: jobs, opts: opts, log: log}
	for i := range s.updateLocks {
		s.updateLocks[i] = make(chan struct{}, 1)
	}
	return s
}

// Add 写入消息；Async 打开时入队并立即返回“已接收”。
// 调用方不需要提供幂等键：同一租户下，归一内容相同的请求在 TTL 内只执行一次。
func (s *Service) Add(
	ctx context.Context,
	caller domain.Caller,
	req domain.AddRequest,
) (result domain.AddResult, err error) {
	defer s.observe("add", &err)
	if err := requireTenant(caller); err != nil {
		return domain.AddResult{}, err
	}
	messages, err := NormalizeMessages(req.Messages)
	if err != nil {
		return domain.AddResult{}, err
	}
	payload := domain.AddPayload{
		TenantID:  caller.TenantID,
		UserID:    caller.UserID,
		SessionID: ResolveSessionID(req.SessionID, req.FirstMessage),
		Messages:  messages,
		Tags:      cleanTags(req.Tags),
	}

	key := strings.TrimSpace(req.IdempotencyKey)
	if len(key) > maxIdempotencyKeyLen {
		return domain.AddResult{}, domain.Invalid("add_message", "idempotency_key 最多 128 字节")
	}
	if s.opts.Idem != nil {
		hash, err := addRequestHash(payload, req.Async)
		if err != nil {
			return domain.AddResult{}, err
		}
		if key == "" {
			key = autoKeyPrefix + hash
		}
		return s.addIdempotent(ctx, addAttempt{payload: payload, async: req.Async, key: key, hash: hash, callID: req.CallID})
	}
	return s.addOnce(ctx, addAttempt{payload: payload, async: req.Async, callID: req.CallID})
}

// addAttempt 是一次 add 的执行参数；key 为空表示不参与请求去重。
type addAttempt struct {
	payload domain.AddPayload
	async   bool
	key     string
	hash    string
	callID  string
}

// addIdempotent 用 (tenant, key) 占用一次写入；重复请求命中记录时直接重放响应。
func (s *Service) addIdempotent(ctx context.Context, a addAttempt) (domain.AddResult, error) {
	now := time.Now()
	existing, claimed, err := s.opts.Idem.ClaimIdempotency(ctx, domain.IdempotencyRecord{
		TenantID: a.payload.TenantID, Key: a.key, Tool: domain.ToolAddMessage,
		RequestHash: a.hash, State: domain.IdempotencyInProgress, CreatedAt: now, UpdatedAt: now,
	})
	if err != nil {
		return domain.AddResult{}, err
	}
	if !claimed {
		if existing.RequestHash != a.hash {
			return domain.AddResult{}, domain.Wrap(domain.ErrConflict, "add_message", "该幂等键已用于不同的请求", nil)
		}
		if (existing.State == domain.IdempotencySucceeded || existing.State == domain.IdempotencyAccepted) &&
			len(existing.Response) > 0 {
			var cached domain.AddResult
			if err := json.Unmarshal(existing.Response, &cached); err != nil {
				return domain.AddResult{}, fmt.Errorf("memory: decode idempotent response: %w", err)
			}
			return cached, nil
		}
		return domain.AddResult{}, domain.Wrap(domain.ErrConflict, "add_message", "同一幂等键的请求正在处理中，请稍后重试", nil)
	}

	result, err := s.addOnce(ctx, a)
	if err != nil {
		if relErr := s.opts.Idem.ReleaseIdempotency(ctx, a.payload.TenantID, a.key); relErr != nil {
			s.log.WarnContext(ctx, "release idempotency failed",
				slog.String("key", a.key), slog.String("error", relErr.Error()))
		}
		return domain.AddResult{}, err
	}
	body, err := json.Marshal(result)
	if err != nil {
		return domain.AddResult{}, fmt.Errorf("memory: encode idempotent response: %w", err)
	}
	// 异步只表示“已入队”，记录停在 accepted，由 worker 终态收敛；
	// 同步已经真正写完，直接记 succeeded。
	var idemErr error
	if result.Deferred {
		idemErr = s.opts.Idem.AcceptIdempotency(ctx, domain.IdempotencyOutcome{
			TenantID: a.payload.TenantID,
			Key:      a.key,
			Response: body,
			At:       time.Now(),
		})
	} else {
		idemErr = s.opts.Idem.CompleteIdempotency(ctx, domain.IdempotencyOutcome{
			TenantID: a.payload.TenantID,
			Key:      a.key,
			Response: body,
			At:       time.Now(),
		})
	}
	if idemErr != nil {
		s.log.WarnContext(ctx, "record idempotency failed",
			slog.String("key", a.key), slog.String("error", idemErr.Error()))
	}
	return result, nil
}

func (s *Service) addOnce(ctx context.Context, a addAttempt) (domain.AddResult, error) {
	if a.async && s.opts.AddAsync && s.jobs != nil {
		if result, ok := s.enqueueAdd(ctx, a.payload, a.key, a.callID); ok {
			return result, nil
		}
		// 入队失败退回同步写入，不丢数据。
	}
	result, err := s.up.Add(ctx, a.payload)
	if err == nil && s.opts.Events != nil {
		s.captureWriteSnapshot(ctx, a.payload, &result)
		s.recordAddResult(ctx, a.payload, result, a.callID)
	}
	return result, err
}

// addRequestHash 是对归一后负载的内容哈希；键相同但内容不同会被拒绝。
// 开关一并参与哈希，是为了让「同一个请求在某些实例上异步、在另一些实例上同步」
// 时不会被误判成重放；MCP 与 REST 现在都走异步，同一段对话无论从哪个入口进来
// 都命中同一个自动键，只写一次。
func addRequestHash(payload domain.AddPayload, async bool) (string, error) {
	body, err := json.Marshal(struct {
		Payload domain.AddPayload `json:"payload"`
		Async   bool              `json:"async"`
	}{payload, async})
	if err != nil {
		return "", fmt.Errorf("memory: hash add request: %w", err)
	}
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:]), nil
}

// addJobPayload 是异步写入的任务负载：归一后的请求快照 + 内部请求去重键。
type addJobPayload struct {
	Request domain.AddPayload `json:"request"`
	Key     string            `json:"idempotency_key,omitempty"`
	CallID  string            `json:"call_id,omitempty"`
}

func (s *Service) enqueueAdd(
	ctx context.Context,
	payload domain.AddPayload,
	key,
	callID string,
) (domain.AddResult, bool) {
	// 同一标识进入任务快照，业务事件与终态审计才能精确关联。
	id := strings.TrimSpace(callID)
	if id == "" {
		id = domain.NewID()
	}
	body, err := json.Marshal(addJobPayload{Request: payload, Key: key, CallID: id})
	if err != nil {
		s.log.WarnContext(ctx, "marshal add job failed", slog.String("error", err.Error()))
		return domain.AddResult{}, false
	}
	job := domain.Job{
		ID:       id,
		Kind:     domain.JobKindMemoryAdd,
		Status:   domain.JobStatusPending,
		TenantID: payload.TenantID,
		UserID:   payload.UserID,
		Payload:  body,
	}
	if err := s.jobs.EnqueueJob(ctx, job); err != nil {
		s.log.WarnContext(ctx, "enqueue add job failed, falling back to sync",
			slog.String("error", err.Error()))
		return domain.AddResult{}, false
	}
	return domain.AddResult{
		Status:   "success",
		Message:  "Memory accepted. Extraction continues in the background; no need to wait.",
		Deferred: true,
		CallID:   id,
	}, true
}

// RunAddJob 由 worker 调用，执行入队时保存的同步写入。
func (s *Service) RunAddJob(ctx context.Context, payload []byte) (domain.AddResult, error) {
	job, err := decodeAddJob(payload)
	job, err = bindAddJobIdentity(ctx, job, err)
	if err != nil {
		return domain.AddResult{}, domain.Wrap(domain.ErrInvalidInput, "run_add_job", "任务负载无法解析", err)
	}
	req := job.Request
	if req.TenantID == "" || req.UserID == "" {
		return domain.AddResult{}, domain.Wrap(domain.ErrInvalidInput, "run_add_job", "任务缺少租户", nil)
	}
	result, err := s.up.Add(ctx, req)
	if err == nil {
		s.captureWriteSnapshot(ctx, req, &result)
		if job.CallID != "" {
			s.recordAddResult(ctx, req, result, job.CallID)
		}
	}
	return result, err
}

// ReportJobOutcome 由 worker 在任务进入终态时调用，收敛异步写入的请求去重记录：
// 成功置 succeeded，永久失败删除记录，让同内容请求可以重新入队。
func (s *Service) ReportJobOutcome(ctx context.Context, payload []byte, jobErr error) {
	job, err := decodeAddJob(payload)
	job, err = bindAddJobIdentity(ctx, job, err)
	if err != nil {
		return
	}
	req, key := job.Request, job.Key
	if jobErr != nil && job.CallID != "" {
		s.recordEvent(ctx, domain.Caller{TenantID: req.TenantID, UserID: req.UserID}, domain.MemoryEvent{Kind: "add", Status: "failed", CallID: job.CallID, SessionID: req.SessionID})
	}
	if s.opts.Idem == nil || key == "" {
		return
	}
	if jobErr == nil {
		err = s.opts.Idem.SucceedIdempotency(ctx, req.TenantID, key, time.Now())
	} else {
		err = s.opts.Idem.ReleaseIdempotency(ctx, req.TenantID, key)
	}
	if err != nil {
		s.log.WarnContext(ctx, "finish idempotency failed",
			slog.String("key", key), slog.String("error", err.Error()))
	}
}

func decodeAddJob(payload []byte) (addJobPayload, error) {
	var wrapped addJobPayload
	if err := json.Unmarshal(payload, &wrapped); err != nil {
		return addJobPayload{}, err
	}
	if wrapped.Request.TenantID == "" {
		// jobs 表只保留 7 天（MEMOSD_JOB_RETENTION_DAYS），早于包装格式的任务早已清理，
		// 没有需要读的旧负载；走到这里只可能是手工写坏的行。
		return addJobPayload{}, domain.Invalid("run_add_job", "任务负载缺少租户")
	}
	return wrapped, nil
}

// Search 检索记忆。
func (s *Service) Search(
	ctx context.Context,
	caller domain.Caller,
	q domain.SearchQuery,
) (result domain.SearchResult, err error) {
	defer s.observe("search", &err)
	if err := requireTenant(caller); err != nil {
		return domain.SearchResult{}, err
	}
	defer s.recordFailure(ctx, caller, "recall", &err)
	if strings.TrimSpace(q.Text) == "" {
		return domain.SearchResult{}, domain.Invalid("search", "query 不能为空")
	}
	switch q.Mode {
	case "", "fast", "fine", "mixture":
	default:
		return domain.SearchResult{}, domain.Invalid("search", "mode 只能是 fast、fine 或 mixture")
	}
	q.TenantID = caller.TenantID
	q.UserID = caller.UserID
	q.TopK = num.WithDefault(q.TopK, s.opts.DefaultTopK, 1, s.opts.MaxTopK)
	q.SessionID = ResolveSessionID(q.SessionID, q.FirstMessage)
	q.DreamTopK = s.opts.DreamTopK
	if s.opts.QueryPrefix != "" {
		q.Text = s.opts.QueryPrefix + q.Text
	}
	result, err = s.up.Search(ctx, q)
	if err == nil {
		s.recordRecall(ctx, caller, result, q.SessionID)
	}
	return result, err
}

// Delete 删除当前租户的记忆。查询与删除都带租户条件；不属于本租户的 id 一律按不存在处理。
func (s *Service) Delete(
	ctx context.Context,
	caller domain.Caller,
	ids []string,
) (result domain.DeleteResult, err error) {
	defer s.observe("delete", &err)
	if err := requireTenant(caller); err != nil {
		return domain.DeleteResult{}, err
	}
	defer s.recordFailure(ctx, caller, "delete", &err)
	unique := make([]string, 0, len(ids))
	seen := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if id == "" {
			continue
		}
		if _, dup := seen[id]; dup {
			continue
		}
		seen[id] = struct{}{}
		unique = append(unique, id)
	}
	if len(unique) == 0 {
		return domain.DeleteResult{}, domain.Invalid("delete", "memory_ids 至少需要一个 id")
	}

	mine, unknown, err := s.splitOwned(ctx, caller, unique)
	if err != nil {
		return domain.DeleteResult{}, err
	}
	notFound, err := s.filterForeign(ctx, caller, unknown)
	if err != nil {
		return domain.DeleteResult{}, err
	}
	before, truncated := s.beforeDelete(ctx, caller, mine)
	if len(mine) > 0 {
		if err := s.up.Delete(ctx, caller.TenantID, mine); err != nil {
			return domain.DeleteResult{}, err
		}
	}
	if len(mine) > 0 {
		s.recordEvent(ctx, caller, domain.MemoryEvent{Kind: "delete", Status: "succeeded", IDs: mine, Before: before, Truncated: truncated})
	}
	return domain.DeleteResult{DeletedIDs: mine, NotFoundIDs: notFound}, nil
}

// splitOwned 批量查询归属，把 id 分成自有与待定两类。一次上游请求代替逐 id 的 N+1。
func (s *Service) splitOwned(
	ctx context.Context,
	caller domain.Caller,
	ids []string,
) (mine, unknown []string, err error) {
	if len(ids) == 0 {
		return nil, nil, nil
	}
	owned, err := s.up.OwnedMemories(ctx, caller.TenantID, caller.UserID, ids)
	if err != nil {
		return nil, nil, err
	}
	for _, id := range ids {
		if owned[id] {
			mine = append(mine, id)
		} else {
			unknown = append(unknown, id)
		}
	}
	return mine, unknown, nil
}

// filterForeign 用归属查询区分“不存在”与“别人的记忆”。
// 归属查询是删除的最后防线：接口失败时不删除任何 id，避免把“查不到归属”误当放行。
func (s *Service) filterForeign(ctx context.Context, caller domain.Caller, ids []string) ([]string, error) {
	if len(ids) == 0 {
		return nil, nil
	}
	owners, err := s.up.Owners(ctx, ids)
	if err != nil {
		return nil, err
	}
	notFound := make([]string, 0, len(ids))
	foreign := 0
	for _, id := range ids {
		if owner, ok := owners[id]; ok && owner != caller.TenantID {
			foreign++
			continue
		}
		notFound = append(notFound, id)
	}
	if foreign > 0 {
		return nil, domain.Wrap(domain.ErrForbidden, "delete",
			fmt.Sprintf("拒绝执行：%d 个 id 不属于当前调用方的记忆空间，未做任何删除。", foreign), nil)
	}
	return notFound, nil
}

// Feedback 记录一次纠错。
func (s *Service) Feedback(
	ctx context.Context,
	caller domain.Caller,
	req domain.FeedbackRequest,
) (result domain.FeedbackResult, err error) {
	defer s.observe("feedback", &err)
	if err := requireTenant(caller); err != nil {
		return domain.FeedbackResult{}, err
	}
	if strings.TrimSpace(req.Content) == "" {
		return domain.FeedbackResult{}, domain.Invalid("feedback", "feedback_content 不能为空")
	}
	if len(req.RetrievedMemoryIDs) > 0 {
		mine, unknown, checkErr := s.splitOwned(ctx, caller, req.RetrievedMemoryIDs)
		if checkErr != nil {
			return result, checkErr
		}
		if len(unknown) > 0 {
			return result, domain.Wrap(domain.ErrNotFound, "feedback", "未找到要纠正的记忆", nil)
		}
		req.RetrievedMemoryIDs = mine
	}
	req.TenantID = caller.TenantID
	req.UserID = caller.UserID
	req.SessionID = ResolveSessionID(req.SessionID, req.FirstMessage)
	return s.up.Feedback(ctx, req)
}

// Profile 查询画像。
func (s *Service) Profile(
	ctx context.Context,
	caller domain.Caller,
	q domain.ProfileQuery,
) (result domain.ProfileResult, err error) {
	defer s.observe("profile", &err)
	if err := requireTenant(caller); err != nil {
		return domain.ProfileResult{}, err
	}
	q.TenantID = caller.TenantID
	q.UserID = caller.UserID
	if q.Page < 1 {
		q.Page = 1
	}
	q.PageSize = num.WithDefault(q.PageSize, defaultPageSize, 1, 50)
	defer s.recordFailure(ctx, caller, "recall", &err)
	result, err = s.up.Profile(ctx, q)
	if err == nil {
		s.recordRecall(ctx, caller, domain.SearchResult{Facts: result.Facts, Preferences: result.Preferences, Tools: result.Tools}, "")
	}
	return result, err
}

func requireTenant(caller domain.Caller) error {
	if caller.Admin {
		return domain.Wrap(domain.ErrForbidden, "memory", "管理 Token 不能调用记忆接口", nil)
	}
	return caller.Validate()
}

func cleanTags(tags []string) []string {
	out := make([]string, 0, len(tags))
	for _, tag := range tags {
		if tag = strings.TrimSpace(tag); tag != "" {
			out = append(out, tag)
		}
	}
	return out
}
