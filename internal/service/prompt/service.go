package prompt

import (
	"context"
	"time"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/throttle"
)

// 请求上限。数值来自 spec 第 8 节，数据面会再检查一次：
// 它也可能被别的客户端直接调用，控制面的限额不是安全边界。
const (
	maxDraftChars           = 20000
	maxNoteChars            = 200
	maxInstructionChars     = 2000
	maxConversationMessages = 30
	maxConversationChars    = 12000
)

// Options 是限流参数（来自 internal/config）与测试用的时钟。
type Options struct {
	TestBurst            int
	TestRefillPerMin     int
	GenerateBurst        int
	GenerateRefillPerMin int
	InFlightPerProject   int
	// Now 只用于测试令牌桶的补充速率；nil 用 time.Now。
	Now func() time.Time
}

// Service 是控制台的提示词工作台。控制面只做代理与限额，因此除限流计数外没有状态。
type Service struct {
	gw           Gateway
	testLimit    *throttle.Buckets
	testAccount  *throttle.Buckets
	genLimit     *throttle.Buckets
	genAccount   *throttle.Buckets
	testInFlight *throttle.Gauge
	genInFlight  *throttle.Gauge
}

// New 构造服务。限流参数缺省时用 spec 的默认值；生产配置由 internal/config 注入。
func New(gw Gateway, opts Options) *Service {
	if opts.TestBurst <= 0 {
		opts.TestBurst = 3
	}
	if opts.TestRefillPerMin <= 0 {
		opts.TestRefillPerMin = 6
	}
	if opts.GenerateBurst <= 0 {
		opts.GenerateBurst = 2
	}
	if opts.GenerateRefillPerMin <= 0 {
		opts.GenerateRefillPerMin = 4
	}
	if opts.InFlightPerProject <= 0 {
		opts.InFlightPerProject = 1
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	return &Service{
		gw:          gw,
		testLimit:   throttle.NewBucketsAt(opts.TestBurst, opts.TestRefillPerMin, opts.Now),
		testAccount: throttle.NewBucketsAt(opts.TestBurst, opts.TestRefillPerMin, opts.Now),
		genLimit:    throttle.NewBucketsAt(opts.GenerateBurst, opts.GenerateRefillPerMin, opts.Now),
		genAccount:  throttle.NewBucketsAt(opts.GenerateBurst, opts.GenerateRefillPerMin, opts.Now),
		// 试跑与改写各自独立计在途：一个是模型跑得慢，另一个是人在改文案，
		// 共用一个名额会让改写被试跑堵住（spec 第 8 节要求互不占用）。
		testInFlight: throttle.NewGauge(opts.InFlightPerProject),
		genInFlight:  throttle.NewGauge(opts.InFlightPerProject),
	}
}

// List 返回项目的提示词清单。
func (s *Service) List(ctx context.Context, tenantID string) (domain.PromptCatalog, error) {
	return s.gw.List(ctx, tenantID)
}

// Detail 返回一条提示词的注册项、默认值与生效值。
func (s *Service) Detail(ctx context.Context, tenantID, key string) (domain.PromptDetail, error) {
	return s.gw.Detail(ctx, tenantID, key)
}

// Versions 返回一个槽位的历史版本。
func (s *Service) Versions(ctx context.Context, in domain.PromptVersionsQuery) (domain.PromptVersionPage, error) {
	return s.gw.Versions(ctx, in)
}

// Validate 做静态校验，不落库。
//
// spec 第 6 节要求这条接口始终 200：正文超长由数据面在 errors 里给 too_long，
// 页面把原因显示在校验区。控制面在这里本地 413 会让前端拿不到那条原因，
// 所以不检查正文长度，只由 transport 限制整个请求体的大小。
func (s *Service) Validate(ctx context.Context, in domain.PromptValidateInput) (domain.PromptValidation, error) {
	return s.gw.Validate(ctx, in)
}

// Save 保存一个槽位的新版本。body 与 restoreFromVersionId 二选一。
func (s *Service) Save(ctx context.Context, in domain.PromptSaveInput) (domain.PromptSaveResult, error) {
	hasBody := in.Body != ""
	hasRestore := in.RestoreFromVersionID > 0
	if hasBody == hasRestore {
		return domain.PromptSaveResult{}, domain.Invalid("prompt",
			"正文与「从历史版本恢复」只能二选一：要保存新内容就填正文，要回滚就选一个历史版本")
	}
	if in.Note != "" && utf8.RuneCountInString(in.Note) > maxNoteChars {
		return domain.PromptSaveResult{}, domain.Invalid("prompt", "备注最多 200 字，请精简后再保存")
	}
	if in.Body != "" {
		if err := checkDraft(in.Body); err != nil {
			return domain.PromptSaveResult{}, err
		}
	}
	return s.gw.Save(ctx, in)
}

// Reset 把槽位还原成内置默认。
func (s *Service) Reset(ctx context.Context, in domain.PromptResetInput) (domain.PromptResetResult, error) {
	return s.gw.Reset(ctx, in)
}

// Test 用真实抽取路径试跑草稿，不写任何存储。限流与在途上限都在这里挡；
// 项目维度与账号维度任一超限都拒绝。
func (s *Service) Test(ctx context.Context, in domain.PromptTestInput) (domain.PromptTestResult, error) {
	if err := checkDraft(in.Body); err != nil {
		return domain.PromptTestResult{}, err
	}
	if err := checkConversation(in.Input); err != nil {
		return domain.PromptTestResult{}, err
	}
	// compareDefault 实际调用两次模型，按两次扣令牌；在途仍算 1 个。
	cost := 1
	if in.CompareDefault {
		cost = 2
	}
	release, err := s.reserve(in.TenantID, in.Account, "试跑", s.testLimit, s.testAccount, s.testInFlight, cost)
	if err != nil {
		return domain.PromptTestResult{}, err
	}
	defer release()
	return s.gw.Test(ctx, in)
}

// Generate 让数据面按一句话指令改写提示词，不保存。与试跑共用账号/项目维度的限额。
func (s *Service) Generate(ctx context.Context, in domain.PromptGenerateInput) (domain.PromptGenerateResult, error) {
	if utf8.RuneCountInString(in.Instruction) > maxInstructionChars {
		return domain.PromptGenerateResult{}, domain.TooLarge("prompt", "改写要求最多 2000 字，请精简后再试")
	}
	if utf8.RuneCountInString(in.BaseBody) > maxDraftChars {
		return domain.PromptGenerateResult{}, domain.TooLarge("prompt", "用作改写的原文最多 20000 字，请精简后再试")
	}
	release, err := s.reserve(in.TenantID, in.Account, "改写", s.genLimit, s.genAccount, s.genInFlight, 1)
	if err != nil {
		return domain.PromptGenerateResult{}, err
	}
	defer release()
	return s.gw.Generate(ctx, in)
}

// reserve 先占在途名额，再原子地从项目桶与账号桶各取 cost 个令牌：
// 任一桶不足时两个桶都不扣，在途名额也立刻归还。
//
// 没有账号身份的调用（管理令牌）用固定键，不能因为账号为空就跳过账号维度，
// 否则一条管理令牌可以绕开按人的限额。
func (s *Service) reserve(
	tenantID, account, action string,
	projectLimit, accountLimit *throttle.Buckets,
	gauge *throttle.Gauge,
	cost int,
) (func(), error) {
	release, ok := gauge.Acquire(tenantID)
	if !ok {
		return nil, &domain.BusyError{Wait: time.Second, Message: "该项目已有一次" + action + "正在进行，请等它结束"}
	}
	if account == "" {
		account = domain.AdminTokenUsername
	}
	okTokens, wait := throttle.Take(projectLimit, accountLimit, tenantID, account, cost)
	if okTokens {
		return release, nil
	}
	release()
	if wait <= 0 {
		wait = time.Second
	}
	return nil, &domain.BusyError{Wait: wait, Message: action + "太频繁，请稍后重试"}
}

// checkDraft 检查草稿正文长度；空串表示“用当前生效值”，不拦。
func checkDraft(body string) error {
	if utf8.RuneCountInString(body) > maxDraftChars {
		return domain.TooLarge("prompt", "草稿正文最多 20000 字，请精简后再试")
	}
	return nil
}

// checkConversation 检查对话类输入：30 条消息、合计 12000 字。
// 只有带 messages 数组的输入才检查；其它 input_kind 的形状由数据面按 input_schema 校验。
func checkConversation(input any) error {
	object, ok := input.(map[string]any)
	if !ok {
		return nil
	}
	raw, ok := object["messages"]
	if !ok {
		return nil
	}
	items, ok := raw.([]any)
	if !ok {
		return nil
	}
	if len(items) > maxConversationMessages {
		return domain.TooLarge("prompt", "一次最多试跑 30 条对话消息，请精简后再试")
	}
	total := 0
	for _, item := range items {
		message, ok := item.(map[string]any)
		if !ok {
			continue
		}
		total += utf8.RuneCountInString(textValue(message["role"]))
		total += utf8.RuneCountInString(textValue(message["content"]))
	}
	if total > maxConversationChars {
		return domain.TooLarge("prompt", "对话内容最多 12000 字，请精简后再试")
	}
	return nil
}

func textValue(value any) string {
	text, _ := value.(string)
	return text
}
