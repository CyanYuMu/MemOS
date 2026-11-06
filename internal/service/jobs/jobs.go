package jobs

import (
	"context"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const op = "jobs"

// Store 是任务运维需要的存储端口，由 adapter/store 结构化实现。
type Store interface {
	ListJobs(ctx context.Context, f domain.JobFilter) ([]domain.Job, int, error)
	GetJob(ctx context.Context, id string) (domain.Job, error)
	// RequeueJob 在事务内判状态并放回 pending，只接受 failed。
	RequeueJob(ctx context.Context, id string, now time.Time) (domain.Job, error)
}

// Service 提供死信查询与人工重放。
type Service struct {
	store Store
	now   func() time.Time
}

// New 构造任务运维服务。
func New(store Store) *Service {
	return &Service{store: store, now: time.Now}
}

// List 是死信排查用的只读查询：只返回任务元数据，不返回 payload。
func (s *Service) List(ctx context.Context, f domain.JobFilter) ([]domain.Job, int, error) {
	if f.Status != "" && !f.Status.Valid() {
		return nil, 0, domain.Invalid(op, "status 只能是 pending、running、succeeded 或 failed")
	}
	return s.store.ListJobs(ctx, f)
}

// Retry 重放一条死信任务：失败原因处理完之后，人从控制台点「重放」。
//
// 只接受 failed（存储层判），重放后 attempts 归零、立即可被 worker 认领。
// 但「能重放」不等于「重放安全」：上游 `/product/add` 没有幂等键，任务因为超时/
// 连不上而失败时，分不清是真的没写进去，还是写成功了但响应丢了。所以先按
// last_error 判（domain.ReplaySafe）：保证发生在请求被上游接受之前的错误（校验失败、
// 认证/路由被拒、上游明确拒绝）直接重放；其余必须显式 force —— 由人确认
// 「上游确实没写成功」之后再重放，宁可多一步，也不静默写出第二条记忆。
func (s *Service) Retry(ctx context.Context, id string, force bool) (domain.Job, error) {
	if id == "" {
		return domain.Job{}, domain.Invalid(op, "缺少任务 id")
	}
	if !force {
		job, err := s.store.GetJob(ctx, id)
		if err != nil {
			return domain.Job{}, err
		}
		if !domain.ReplaySafe(job.LastError) {
			return domain.Job{}, domain.Wrap(domain.ErrConflict, op,
				"这条任务失败在「可能已经写进上游」的位置（"+job.LastError+
					"），重放可能写出第二条记忆；确认上游没写成功再加 force=true", nil)
		}
	}
	return s.store.RequeueJob(ctx, id, s.now())
}
