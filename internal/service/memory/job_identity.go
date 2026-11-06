package memory

import (
	"context"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type (
	jobIdentity    struct{ callID, tenant, user string }
	jobIdentityKey struct{}
)

// WithJobIdentity 仅绑定队列实际身份，旧payload不需要靠新增字段推断调用ID。
func (s *Service) WithJobIdentity(ctx context.Context, job domain.Job) context.Context {
	return context.WithValue(ctx, jobIdentityKey{}, jobIdentity{job.ID, job.TenantID, job.UserID})
}

func bindAddJobIdentity(ctx context.Context, payload addJobPayload, err error) (addJobPayload, error) {
	if err != nil {
		return payload, err
	}
	identity, ok := ctx.Value(jobIdentityKey{}).(jobIdentity)
	if !ok {
		return payload, nil
	}
	if identity.callID == "" || identity.tenant != payload.Request.TenantID || identity.user != payload.Request.UserID {
		return payload, domain.Invalid("run_add_job", "任务无法执行，请重新提交对话")
	}
	payload.CallID = identity.callID
	return payload, nil
}
