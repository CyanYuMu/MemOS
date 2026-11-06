package prompt

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// countingGateway 记录调用次数，并能按需阻塞，用来测在途上限。
type countingGateway struct {
	recordingGateway
	mu        sync.Mutex
	tests     int
	gens      int
	validated []domain.PromptValidateInput
	block     chan struct{}
	entered   chan struct{}
}

func (g *countingGateway) Test(context.Context, domain.PromptTestInput) (domain.PromptTestResult, error) {
	g.mu.Lock()
	g.tests++
	g.mu.Unlock()
	if g.entered != nil {
		g.entered <- struct{}{}
	}
	if g.block != nil {
		<-g.block
	}
	return domain.PromptTestResult{}, nil
}

func (g *countingGateway) Generate(context.Context, domain.PromptGenerateInput) (domain.PromptGenerateResult, error) {
	g.mu.Lock()
	g.gens++
	g.mu.Unlock()
	return domain.PromptGenerateResult{}, nil
}

// Validate 模拟数据面：正文超长时以 200 + too_long 返回，而不是报错。
func (g *countingGateway) Validate(_ context.Context, in domain.PromptValidateInput) (domain.PromptValidation, error) {
	g.mu.Lock()
	g.validated = append(g.validated, in)
	g.mu.Unlock()
	if len([]rune(in.Body)) > maxDraftChars {
		return domain.PromptValidation{
			OK:     false,
			Errors: []domain.PromptIssue{{Code: "too_long", Message: "正文最多 20000 字"}},
		}, nil
	}
	return domain.PromptValidation{OK: true}, nil
}

func (g *countingGateway) genCount() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.gens
}

func (g *countingGateway) testCount() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.tests
}

func TestTestRouteLimitsBurstThenRefills(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	gw := &countingGateway{}
	svc := New(gw, Options{
		TestBurst: 2, TestRefillPerMin: 60,
		Now: func() time.Time { return now },
	})
	in := domain.PromptTestInput{TenantID: "cube-a", Key: "extract.chat", Lang: "zh", Account: "alice"}

	for i := range 2 {
		if _, err := svc.Test(context.Background(), in); err != nil {
			t.Fatalf("第 %d 次应放行: %v", i+1, err)
		}
	}
	_, err := svc.Test(context.Background(), in)
	busy, ok := errors.AsType[*domain.BusyError](err)
	if !ok {
		t.Fatalf("超出突发应返回 BusyError: %v", err)
	}
	if busy.Wait <= 0 || domain.SafeMessage(err) == "" {
		t.Fatalf("限流应带等待时间与可展示文案: %+v", busy)
	}

	now = now.Add(time.Second)
	if _, err := svc.Test(context.Background(), in); err != nil {
		t.Fatalf("补充一个令牌后应放行: %v", err)
	}
	if tests := gw.testCount(); tests != 3 {
		t.Fatalf("被限流的请求不应打到上游: tests=%d", tests)
	}
}

// 账号维度独立限流：同一个人换项目也受限，不同人互不影响。
func TestTestRouteLimitsPerAccount(t *testing.T) {
	t.Parallel()

	gw := &countingGateway{}
	svc := New(gw, Options{TestBurst: 1, TestRefillPerMin: 1})
	ctx := context.Background()

	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "alice", Key: "k"}); err != nil {
		t.Fatalf("首个请求应放行: %v", err)
	}
	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-b", Account: "alice", Key: "k"}); !errors.Is(err, domain.ErrRateLimited) {
		t.Fatalf("同一账号换项目仍应限流: %v", err)
	}
	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-c", Account: "bob", Key: "k"}); err != nil {
		t.Fatalf("另一个账号不应受影响: %v", err)
	}
}

func TestTestRouteInFlightLimit(t *testing.T) {
	t.Parallel()

	gw := &countingGateway{block: make(chan struct{}), entered: make(chan struct{}, 4)}
	svc := New(gw, Options{TestBurst: 5, TestRefillPerMin: 60})
	ctx := context.Background()

	done := make(chan error, 1)
	go func() {
		_, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "alice", Key: "k"})
		done <- err
	}()
	<-gw.entered

	_, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "bob", Key: "k"})
	if _, ok := errors.AsType[*domain.BusyError](err); !ok {
		t.Fatalf("同一项目第二个在途请求应被拒绝: %v", err)
	}

	close(gw.block)
	if err := <-done; err != nil {
		t.Fatalf("第一个请求应成功: %v", err)
	}
	// 释放后再来一次应放行（名额确实归还了）。block 已关闭，不会真的等待。
	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "carol", Key: "k"}); err != nil {
		t.Fatalf("释放后应放行: %v", err)
	}
}

func TestGenerateRouteLimits(t *testing.T) {
	t.Parallel()

	gw := &countingGateway{}
	svc := New(gw, Options{GenerateBurst: 1, GenerateRefillPerMin: 1})
	ctx := context.Background()
	in := domain.PromptGenerateInput{TenantID: "cube-a", Account: "alice", Key: "extract.chat"}

	if _, err := svc.Generate(ctx, in); err != nil {
		t.Fatalf("首个改写应放行: %v", err)
	}
	if _, err := svc.Generate(ctx, in); !errors.Is(err, domain.ErrRateLimited) {
		t.Fatalf("超出改写额度应限流: %v", err)
	}
}

func TestTestRouteInputLimits(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name  string
		input domain.PromptTestInput
		kind  error
	}{
		{
			name:  "草稿超长",
			input: domain.PromptTestInput{TenantID: "cube-a", Key: "k", Body: strings.Repeat("好", maxDraftChars+1)},
			kind:  domain.ErrTooLarge,
		},
		{
			name: "消息条数超限",
			input: domain.PromptTestInput{TenantID: "cube-a", Key: "k", Input: map[string]any{
				"messages": messages(maxConversationMessages+1, "hi"),
			}},
			kind: domain.ErrTooLarge,
		},
		{
			name: "对话总字数超限",
			input: domain.PromptTestInput{TenantID: "cube-a", Key: "k", Input: map[string]any{
				"messages": messages(2, strings.Repeat("好", maxConversationChars)),
			}},
			kind: domain.ErrTooLarge,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			gw := &countingGateway{}
			svc := New(gw, Options{})
			_, err := svc.Test(context.Background(), tc.input)
			if !errors.Is(err, tc.kind) {
				t.Fatalf("err = %v", err)
			}
			if tests := gw.testCount(); tests != 0 {
				t.Fatalf("超限请求不应打到上游: %d", tests)
			}
		})
	}
}

func TestSaveRequestLimits(t *testing.T) {
	t.Parallel()

	gw := &countingGateway{}
	svc := New(gw, Options{})
	ctx := context.Background()

	if _, err := svc.Save(ctx, domain.PromptSaveInput{TenantID: "cube-a", Key: "k", Lang: "zh", Note: strings.Repeat("备", maxNoteChars+1)}); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("备注超长应 400: %v", err)
	}
	if _, err := svc.Save(ctx, domain.PromptSaveInput{TenantID: "cube-a", Key: "k", Lang: "zh", Body: strings.Repeat("好", maxDraftChars+1)}); !errors.Is(err, domain.ErrTooLarge) {
		t.Fatalf("正文超长应 413: %v", err)
	}
}

// 边界值本身必须放行，避免把“最多 20000 字”实现成 19999。
func TestTestRouteBoundaryAccepted(t *testing.T) {
	t.Parallel()

	gw := &countingGateway{}
	svc := New(gw, Options{})
	_, err := svc.Test(context.Background(), domain.PromptTestInput{
		TenantID: "cube-a", Key: "k",
		Body:  strings.Repeat("好", maxDraftChars),
		Input: map[string]any{"messages": messages(maxConversationMessages, strings.Repeat("好", maxConversationChars/maxConversationMessages-5))},
	})
	if err != nil {
		t.Fatalf("边界值应放行: %v", err)
	}
}

func messages(count int, content string) []any {
	out := make([]any, 0, count)
	for range count {
		out = append(out, map[string]any{"role": "user", "content": content})
	}
	return out
}

// validate 始终由数据面判定：22k 字也必须打过去，不在控制面本地 413，
// 否则前端拿不到 too_long 这条可展示的原因（spec 第 6 节）。
func TestValidateDoesNotRejectLongBodyLocally(t *testing.T) {
	t.Parallel()

	gw := &countingGateway{}
	svc := New(gw, Options{})
	long := strings.Repeat("好", maxDraftChars+1000)
	validation, err := svc.Validate(context.Background(), domain.PromptValidateInput{
		Key: "extract.chat", Lang: "zh", Body: long,
	})
	if err != nil {
		t.Fatalf("validate 不应在本地失败: %v", err)
	}
	if validation.OK || len(validation.Errors) == 0 || validation.Errors[0].Code != "too_long" {
		t.Fatalf("应把数据面的校验结果原样返回: %+v", validation)
	}
}

// save 的 body 与 restoreFromVersionId 二选一：都给、都不给都拒绝。
func TestSaveRequiresExactlyOneSource(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name  string
		input domain.PromptSaveInput
	}{
		{
			name:  "都不给",
			input: domain.PromptSaveInput{TenantID: "cube-a", Key: "k", Lang: "zh"},
		},
		{
			name: "都给",
			input: domain.PromptSaveInput{
				TenantID: "cube-a", Key: "k", Lang: "zh",
				Body: "x ${conversation}", RestoreFromVersionID: 2,
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			gw := &countingGateway{}
			_, err := New(gw, Options{}).Save(context.Background(), tc.input)
			if !errors.Is(err, domain.ErrInvalidInput) {
				t.Fatalf("应 400: %v", err)
			}
			if !strings.Contains(domain.SafeMessage(err), "二选一") {
				t.Fatalf("文案应说明二选一: %q", domain.SafeMessage(err))
			}
		})
	}

	// 只给其中一个仍然放行。
	gw := &countingGateway{}
	svc := New(gw, Options{})
	if _, err := svc.Save(context.Background(), domain.PromptSaveInput{
		TenantID: "cube-a", Key: "k", Lang: "zh", Body: "x ${conversation}",
	}); err != nil {
		t.Fatalf("只给 body 应放行: %v", err)
	}
	if _, err := svc.Save(context.Background(), domain.PromptSaveInput{
		TenantID: "cube-a", Key: "k", Lang: "zh", RestoreFromVersionID: 2,
	}); err != nil {
		t.Fatalf("只给 restoreFromVersionId 应放行: %v", err)
	}
}

// AI 改写的两个输入各有上限，超出是 413（input_too_large 同口径）。
func TestGenerateInputLimits(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name  string
		input domain.PromptGenerateInput
	}{
		{
			name: "instruction 超长",
			input: domain.PromptGenerateInput{
				TenantID: "cube-a", Key: "k", Lang: "zh",
				Instruction: strings.Repeat("改", maxInstructionChars+1),
			},
		},
		{
			name: "baseBody 超长",
			input: domain.PromptGenerateInput{
				TenantID: "cube-a", Key: "k", Lang: "zh",
				Instruction: "改", BaseBody: strings.Repeat("好", maxDraftChars+1),
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			gw := &countingGateway{}
			_, err := New(gw, Options{}).Generate(context.Background(), tc.input)
			if !errors.Is(err, domain.ErrTooLarge) {
				t.Fatalf("应 413: %v", err)
			}
			if gens := gw.genCount(); gens != 0 {
				t.Fatalf("超限请求不应打到上游")
			}
		})
	}

	// 边界值放行。
	gw := &countingGateway{}
	if _, err := New(gw, Options{}).Generate(context.Background(), domain.PromptGenerateInput{
		TenantID: "cube-a", Key: "k", Lang: "zh",
		Instruction: strings.Repeat("改", maxInstructionChars),
		BaseBody:    strings.Repeat("好", maxDraftChars),
	}); err != nil {
		t.Fatalf("边界值应放行: %v", err)
	}
}

// 试跑与改写的在途计数互不占用：一次在途试跑不能挡住 AI 改写。
func TestInFlightIsIndependentPerAction(t *testing.T) {
	t.Parallel()

	gw := &countingGateway{block: make(chan struct{}), entered: make(chan struct{}, 4)}
	svc := New(gw, Options{TestBurst: 5, TestRefillPerMin: 60, GenerateBurst: 5, GenerateRefillPerMin: 60})
	ctx := context.Background()

	done := make(chan error, 1)
	go func() {
		_, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "alice", Key: "k"})
		done <- err
	}()
	<-gw.entered

	if _, err := svc.Generate(ctx, domain.PromptGenerateInput{
		TenantID: "cube-a", Account: "alice", Key: "k", Instruction: "改",
	}); err != nil {
		t.Fatalf("试跑在途不应挡住改写: %v", err)
	}
	close(gw.block)
	if err := <-done; err != nil {
		t.Fatalf("试跑应成功: %v", err)
	}
}

// 项目桶与账号桶必须“先检查再扣”：账号桶拒绝时项目桶一个令牌都不能少。
func TestTokensAreNotConsumedWhenAccountBucketRejects(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	gw := &countingGateway{}
	svc := New(gw, Options{
		TestBurst: 3, TestRefillPerMin: 1, InFlightPerProject: 10,
		Now: func() time.Time { return now },
	})
	ctx := context.Background()

	// alice 在项目 A 耗尽自己的账号桶（项目 A 的桶也一起耗尽）。
	for range 3 {
		if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "alice", Key: "k"}); err != nil {
			t.Fatalf("额度内应放行: %v", err)
		}
	}
	// alice 换到项目 B：账号桶空 → 拒绝；项目 B 的 3 个令牌必须原封不动。
	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-b", Account: "alice", Key: "k"}); !errors.Is(err, domain.ErrRateLimited) {
		t.Fatalf("账号桶应拒绝: %v", err)
	}
	for i := range 3 {
		if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-b", Account: "bob", Key: "k"}); err != nil {
			t.Fatalf("项目 B 的第 %d 个请求应放行（被拒绝的请求不能白扣项目令牌）: %v", i+1, err)
		}
	}
}

// 没有账号身份（管理令牌）时用固定账号键，不能跳过账号维度。
func TestEmptyAccountUsesFixedKey(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	gw := &countingGateway{}
	svc := New(gw, Options{
		TestBurst: 1, TestRefillPerMin: 1, InFlightPerProject: 10,
		Now: func() time.Time { return now },
	})
	ctx := context.Background()

	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Key: "k"}); err != nil {
		t.Fatalf("首个请求应放行: %v", err)
	}
	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-b", Key: "k"}); !errors.Is(err, domain.ErrRateLimited) {
		t.Fatalf("空账号也必须受账号维度限制: %v", err)
	}
}

// 并发下“检查再扣”不能超发：突发 3，50 个并发只应通过 3 个。
func TestConcurrentTakeDoesNotOverIssue(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	gw := &countingGateway{}
	svc := New(gw, Options{
		TestBurst: 3, TestRefillPerMin: 1, InFlightPerProject: 200,
		Now: func() time.Time { return now },
	})
	ctx := context.Background()

	const callers = 50
	var (
		wg    sync.WaitGroup
		mu    sync.Mutex
		okN   int
		busyN int
	)
	for i := range callers {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			account := "alice"
			if i%2 == 0 {
				account = "bob"
			}
			_, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: account, Key: "k"})
			mu.Lock()
			defer mu.Unlock()
			if err == nil {
				okN++
			} else if errors.Is(err, domain.ErrRateLimited) {
				busyN++
			}
		}(i)
	}
	wg.Wait()
	if okN != 3 || busyN != callers-3 {
		t.Fatalf("并发下超发或错拒: ok=%d busy=%d", okN, busyN)
	}
}

// compareDefault 实际调用两次模型，扣 2 个令牌：突发 3 时对比一次 + 普通一次。
func TestCompareDefaultCostsTwoTokens(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	gw := &countingGateway{}
	svc := New(gw, Options{
		TestBurst: 3, TestRefillPerMin: 1, InFlightPerProject: 10,
		Now: func() time.Time { return now },
	})
	ctx := context.Background()

	if _, err := svc.Test(ctx, domain.PromptTestInput{
		TenantID: "cube-a", Account: "alice", Key: "k", CompareDefault: true,
	}); err != nil {
		t.Fatalf("对比试跑应放行: %v", err)
	}
	if _, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "alice", Key: "k"}); err != nil {
		t.Fatalf("剩 1 个令牌时普通试跑应放行: %v", err)
	}
	_, err := svc.Test(ctx, domain.PromptTestInput{TenantID: "cube-a", Account: "alice", Key: "k"})
	busy, ok := errors.AsType[*domain.BusyError](err)
	if !ok {
		t.Fatalf("第三次应被拒绝: %v", err)
	}
	if busy.Wait < 50*time.Second {
		t.Fatalf("等待时间应按补足一个令牌算（每分钟 1 个 ≈ 60s）: %v", busy.Wait)
	}
}
