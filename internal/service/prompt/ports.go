package prompt

import (
	"context"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Gateway 是数据面的提示词端口，由 adapter/upstream 实现。
// 注册表、默认模板、版本与试跑都在数据面；控制面不落库、不缓存。
type Gateway interface {
	List(ctx context.Context, tenantID string) (domain.PromptCatalog, error)
	Detail(ctx context.Context, tenantID, key string) (domain.PromptDetail, error)
	Versions(ctx context.Context, in domain.PromptVersionsQuery) (domain.PromptVersionPage, error)
	Validate(ctx context.Context, in domain.PromptValidateInput) (domain.PromptValidation, error)
	Save(ctx context.Context, in domain.PromptSaveInput) (domain.PromptSaveResult, error)
	Reset(ctx context.Context, in domain.PromptResetInput) (domain.PromptResetResult, error)
	Test(ctx context.Context, in domain.PromptTestInput) (domain.PromptTestResult, error)
	Generate(ctx context.Context, in domain.PromptGenerateInput) (domain.PromptGenerateResult, error)
}
