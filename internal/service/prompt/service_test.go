package prompt

import (
	"context"
	"errors"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// recordingGateway 记录服务层转发过来的入参，用来锁定「控制面只代理」的边界。
type recordingGateway struct {
	calls []string
	saved domain.PromptSaveInput
}

func (g *recordingGateway) List(context.Context, string) (domain.PromptCatalog, error) {
	g.calls = append(g.calls, "list")
	return domain.PromptCatalog{}, nil
}

func (g *recordingGateway) Detail(context.Context, string, string) (domain.PromptDetail, error) {
	g.calls = append(g.calls, "detail")
	return domain.PromptDetail{}, nil
}

func (g *recordingGateway) Versions(context.Context, domain.PromptVersionsQuery) (domain.PromptVersionPage, error) {
	g.calls = append(g.calls, "versions")
	return domain.PromptVersionPage{}, nil
}

func (g *recordingGateway) Validate(context.Context, domain.PromptValidateInput) (domain.PromptValidation, error) {
	g.calls = append(g.calls, "validate")
	return domain.PromptValidation{}, nil
}

func (g *recordingGateway) Save(_ context.Context, in domain.PromptSaveInput) (domain.PromptSaveResult, error) {
	g.calls = append(g.calls, "save")
	g.saved = in
	if in.Key == "err" {
		return domain.PromptSaveResult{}, domain.Invalid("prompt", "缺少必填占位符")
	}
	return domain.PromptSaveResult{VersionID: in.BaseVersionID + 1}, nil
}

func (g *recordingGateway) Reset(context.Context, domain.PromptResetInput) (domain.PromptResetResult, error) {
	g.calls = append(g.calls, "reset")
	return domain.PromptResetResult{}, nil
}

func (g *recordingGateway) Test(context.Context, domain.PromptTestInput) (domain.PromptTestResult, error) {
	g.calls = append(g.calls, "test")
	return domain.PromptTestResult{}, nil
}

func (g *recordingGateway) Generate(context.Context, domain.PromptGenerateInput) (domain.PromptGenerateResult, error) {
	g.calls = append(g.calls, "generate")
	return domain.PromptGenerateResult{}, nil
}

func TestServiceDelegatesAllActions(t *testing.T) {
	t.Parallel()

	gw := &recordingGateway{}
	svc := New(gw, Options{})
	ctx := context.Background()

	if _, err := svc.List(ctx, "cube-a"); err != nil {
		t.Fatalf("list: %v", err)
	}
	if _, err := svc.Detail(ctx, "cube-a", "extract.chat"); err != nil {
		t.Fatalf("detail: %v", err)
	}
	if _, err := svc.Versions(ctx, domain.PromptVersionsQuery{TenantID: "cube-a"}); err != nil {
		t.Fatalf("versions: %v", err)
	}
	if _, err := svc.Validate(ctx, domain.PromptValidateInput{Key: "k"}); err != nil {
		t.Fatalf("validate: %v", err)
	}
	saved, err := svc.Save(ctx, domain.PromptSaveInput{
		TenantID: "cube-a", Key: "k", Lang: "zh", Body: "x ${conversation}", BaseVersionID: 4,
	})
	if err != nil || saved.VersionID != 5 {
		t.Fatalf("save = %+v, %v", saved, err)
	}
	if _, err := svc.Reset(ctx, domain.PromptResetInput{TenantID: "cube-a"}); err != nil {
		t.Fatalf("reset: %v", err)
	}
	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a"}); err != nil {
		t.Fatalf("test: %v", err)
	}
	if _, err := svc.Generate(ctx, domain.PromptGenerateInput{TenantID: "cube-a"}); err != nil {
		t.Fatalf("generate: %v", err)
	}

	want := []string{"list", "detail", "versions", "validate", "save", "reset", "test", "generate"}
	if len(gw.calls) != len(want) {
		t.Fatalf("调用 = %v", gw.calls)
	}
	for i := range want {
		if gw.calls[i] != want[i] {
			t.Fatalf("调用顺序 = %v", gw.calls)
		}
	}
	if gw.saved.Author != "" || gw.saved.BaseVersionID != 4 {
		t.Fatalf("入参被改写: %+v", gw.saved)
	}
}

// 领域错误必须原样透传，不能在服务层被包装成另一种类型。
func TestServicePassesErrorsThrough(t *testing.T) {
	t.Parallel()

	_, err := New(&recordingGateway{}, Options{}).Save(context.Background(), domain.PromptSaveInput{Key: "err"})
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("err = %v", err)
	}
}
