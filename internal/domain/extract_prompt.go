package domain

// ExtractPrompt 是项目级抽取 prompt 配置，由上游保存、控制面只代理。
// Prompt 是完整模板，必须包含 ${conversation} 占位符。
type ExtractPrompt struct {
	Prompt          string
	ExtractionTypes []string
	UpdatedAt       string
	Source          string
	IsDefault       bool
}

// PromptMessage 是调试抽取的一条对话消息。
type PromptMessage struct {
	Role    string
	Content string
}

// ExtractDebugResult 是干跑抽取的一条结果。
type ExtractDebugResult struct {
	Type    string
	Content string
}

// ExtractDebug 是干跑抽取的响应；不写图库。
type ExtractDebug struct {
	ExtractionTypes []string
	Results         []ExtractDebugResult
}

// ModelInfo 是上游当前进程实际配置的一个模型。
type ModelInfo struct {
	Role   string
	Model  string
	Spec   string
	Source string
	Group  string
}
