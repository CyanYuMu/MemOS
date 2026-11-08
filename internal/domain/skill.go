package domain

// SkillCall 的身份字段只能由已鉴权的控制台请求上下文构造。
// Payload 保存 P3 camelCase 契约；规范候选的 snake_case 正文不在 Go 重写。
type SkillCall struct {
	Action     string
	Actor      string
	Capability string
	ProjectID  string
	Payload    map[string]any
}

// SkillServiceError 只携带可公开的信息，不保存上游 message 或任意 details。
type SkillServiceError struct {
	Status          int
	Code            string
	CurrentSequence *int64
}

func (e *SkillServiceError) Error() string {
	switch e.Status {
	case 400, 422:
		return "Skill 请求未通过校验"
	case 403:
		return "无权执行此 Skill 操作"
	case 404:
		return "Skill 记录不存在，请刷新后重试"
	case 409:
		return "Skill 状态已变化或不满足操作条件，请重新读取后检查"
	default:
		return "Skill 服务暂不可用，请稍后重试"
	}
}

// Is 与控制台既有错误映射、审计类型兼容。
func (e *SkillServiceError) Is(target error) bool {
	switch e.Status {
	case 400, 422:
		return target == ErrInvalidInput
	case 403:
		return target == ErrForbidden
	case 404:
		return target == ErrNotFound
	case 409:
		return target == ErrConflict
	default:
		return target == ErrUpstreamUnavailable
	}
}
