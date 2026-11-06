package domain

import "encoding/json"

// 提示词工作台（prompt-studio）的实体。注册表、默认模板与版本都在数据面，
// 控制面只做代理，因此这里只有“传输中的形状”，没有存储职责。
// Opaque 字段（inputSchema、sample input、run output）保持上游原样：
// 本任务要求它们透传，转成 map 只会引入一次无意义的重新序列化。

// PromptItem 是列表里的一条提示词，不含正文。
type PromptItem struct {
	Key       string
	Group     string
	Title     map[string]string
	Summary   map[string]string
	Languages []string
	Editable  bool
	State     map[string]PromptSlotState
}

// PromptSlotState 是一个语言槽位的当前状态。
//
// VersionID 是库里的全局自增编号，只用于接口调用与并发判断；VersionNo 是该槽位
// 从 1 开始的序号，给界面显示。默认槽位没有版本，UpdatedAt/Author 是 null。
type PromptSlotState struct {
	Source    string // default | project
	VersionID int64
	VersionNo int64
	UpdatedAt *string
	Author    *string
}

// PromptCatalog 是 prompts/list 的数据。
type PromptCatalog struct {
	Prompts []PromptItem
}

// PromptPlaceholder 是注册项声明的占位符。
type PromptPlaceholder struct {
	Name        string
	Required    bool
	Description map[string]string
}

// PromptOutputContract 是注册项声明的输出结构。
type PromptOutputContract struct {
	Format       string
	RequiredKeys []string
	ItemKeys     []string
	Description  map[string]string
}

// PromptSample 是一条可直接用于试跑的样例输入。Lang 是样例输入本身的语言。
type PromptSample struct {
	ID    string
	Lang  string
	Title map[string]string
	Input json.RawMessage
}

// PromptEffective 是一个槽位当前生效的提示词。默认槽位的 UpdatedAt/Author 是 null。
type PromptEffective struct {
	Body      string
	Source    string
	VersionID int64
	VersionNo int64
	UpdatedAt *string
	Author    *string
}

// PromptDetail 是注册项全文、各语言内置默认与当前生效值。
type PromptDetail struct {
	Key            string
	Group          string
	Title          map[string]string
	Summary        map[string]string
	Languages      []string
	Syntax         string
	Placeholders   []PromptPlaceholder
	LockedSuffix   *string
	InputKind      string
	InputSchema    json.RawMessage
	OutputContract PromptOutputContract
	SampleInputs   []PromptSample
	Editable       bool
	Defaults       map[string]string
	Effective      map[string]PromptEffective
}

// PromptVersionsQuery 是 prompts/versions 的入参；Limit 为 0 表示用数据面默认值。
type PromptVersionsQuery struct {
	TenantID        string
	Key             string
	Lang            string
	Limit           int
	BeforeVersionID int64
}

// PromptVersion 是版本历史里的一行。
type PromptVersion struct {
	VersionID       int64
	VersionNo       int64
	Lang            string
	Kind            string
	Body            string
	Note            string
	Author          string
	CreatedAt       string
	ParentVersionID *int64
}

// PromptVersionPage 是新到旧的版本页。
type PromptVersionPage struct {
	Versions []PromptVersion
	HasMore  bool
}

// PromptIssue 是一条校验错误或警告，message 面向用户。
type PromptIssue struct {
	Code    string
	Message string
}

// PromptValidation 是静态校验结果；警告不阻止保存。
type PromptValidation struct {
	OK       bool
	Errors   []PromptIssue
	Warnings []PromptIssue
}

// PromptValidateInput 是 prompts/validate 的入参。校验是纯静态的，
// 不需要项目：同一注册项在任何项目下规则一致。
type PromptValidateInput struct {
	Key  string
	Lang string
	Body string
}

// PromptSaveInput 是 prompts/save 的入参。Body 与 RestoreFromVersionID 二选一。
type PromptSaveInput struct {
	TenantID             string
	Key                  string
	Lang                 string
	Body                 string
	RestoreFromVersionID int64
	Note                 string
	Author               string
	BaseVersionID        int64
}

// InvalidPromptError 是保存或校验被数据面拒绝的错误。Errors 是逐条问题，
// transport 放进响应的 data，页面可以直接展示"哪里不对"；只给一句
// "请按错误列表修改"却不下发列表，用户无从下手。
type InvalidPromptError struct {
	Message string
	Errors  []PromptIssue
}

func (e *InvalidPromptError) Error() string { return e.Message }

// Is 让 errors.Is(err, ErrInvalidInput) 成立。
func (e *InvalidPromptError) Is(target error) bool { return target == ErrInvalidInput }

// PromptSaveResult 是新版本与该槽位的生效值。
type PromptSaveResult struct {
	VersionID int64
	VersionNo int64
	CreatedAt string
	Effective PromptEffective
}

// PromptResetInput 是 prompts/reset 的入参：Key 为空表示整个项目，
// Lang 为空表示该 key 的全部语言。
type PromptResetInput struct {
	TenantID string
	Key      string
	Lang     string
	Author   string
}

// PromptResetItem 是一个被还原的槽位。
type PromptResetItem struct {
	Key       string
	Lang      string
	VersionID int64
	VersionNo int64
}

// PromptResetResult 是实际被还原的槽位；没有可还原槽位时为空数组。
type PromptResetResult struct {
	Restored []PromptResetItem
}

// PromptTestInput 是 prompts/test 的入参；Body 为空表示用当前生效值。
// Input 原样转发，形状由注册项的 inputSchema 描述。
type PromptTestInput struct {
	TenantID string
	Key      string
	Lang     string
	Body     string
	Input    any
	// Account 是登录账号（审计与限流的账号维度用），不是提示词作者。
	Account        string
	CompareDefault bool
}

// PromptContractCheck 是试跑输出对注册项输出结构的满足情况。
type PromptContractCheck struct {
	OK      bool
	Missing []string
}

// PromptRunError 是一次试跑的模型侧失败；它不改变整个请求的 HTTP 状态。
type PromptRunError struct {
	Code    string
	Message string
}

// PromptSlotRef 是试跑实际取用的模板槽位。
type PromptSlotRef struct {
	Key       string
	Lang      string
	Source    string // draft | project | default
	VersionID int64
}

// PromptTestRun 是一次试跑（草稿或内置默认）的结果。
type PromptTestRun struct {
	Label             string
	OK                bool
	ParseOK           bool
	FallbackTriggered bool
	Contract          PromptContractCheck
	Output            json.RawMessage
	RawOutput         string
	Truncated         bool
	LatencyMS         int64
	Model             string
	Usage             map[string]any
	UsedSlots         []PromptSlotRef
	Warnings          []PromptIssue
	Error             *PromptRunError
}

// PromptTestResult 是 prompts/test 的数据。
type PromptTestResult struct {
	Runs []PromptTestRun
}

// PromptGenerateInput 是 prompts/generate 的入参。
type PromptGenerateInput struct {
	TenantID    string
	Key         string
	Lang        string
	Instruction string
	BaseBody    string
	// Account 是发起改写的登录账号，用于账号维度的限流。
	Account string
}

// PromptGenerateResult 是改写结果与它的静态校验。
type PromptGenerateResult struct {
	Body       string
	Validation PromptValidation
}
