package domain

// OnboardingProgress 仅保存操作检查点，记忆业务结果仍由调用事件记录。
type OnboardingProgress struct {
	Chat     *OnboardingCheckpoint `json:"chat,omitempty"`
	Status   string                `json:"status"`
	Step     int                   `json:"step"`
	Revision int64                 `json:"revision"`
	Version  int                   `json:"version"`
}

// OnboardingCheckpoint 不包含表单内容，只关联当前账号实际创建的会话。
type OnboardingCheckpoint struct {
	Stage  string `json:"stage"`
	First  string `json:"first"`
	Second string `json:"second,omitempty"`
}
