package dashboard

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/auth"
)

// SkillGateway 是 Python Skill 管理服务端口；状态与发布事务由上游唯一裁决。
type SkillGateway interface {
	Call(context.Context, domain.SkillCall) (json.RawMessage, error)
}

type skillRoute struct {
	path       string
	action     string
	capability domain.Capability
	required   []string
	optional   []string
}

var skillRoutes = []skillRoute{
	{path: "experiments/list", action: "experiments_list", capability: domain.CapProjectView},
	{path: "experiments/detail", action: "experiment_detail", capability: domain.CapProjectView, required: []string{"experimentId"}},
	{path: "experiments/start", action: "experiment_start", capability: domain.CapProjectWrite, required: []string{"experimentId", "operationId"}},
	{path: "list", action: "list", capability: domain.CapProjectView, optional: []string{"query"}},
	{path: "detail", action: "detail", capability: domain.CapProjectView, required: []string{"skillId", "revision"}},
	{path: "evidence", action: "evidence", capability: domain.CapProjectView, required: []string{"skillId", "revision", "ruleId"}},
	{path: "evaluations/start", action: "evaluate", capability: domain.CapProjectWrite, required: []string{"skillId", "revision", "caseId", "operationId"}},
	{path: "jobs/detail", action: "job", capability: domain.CapProjectView, required: []string{"jobId"}},
	{path: "reviews/prepare", action: "review_prepare", capability: domain.CapProjectManage, required: []string{"skillId", "revision"}},
	{path: "adjudications/prepare", action: "adjudication_prepare", capability: domain.CapProjectManage, required: []string{"skillId", "revision", "assessmentId"}},
	{path: "adjudications/submit", action: "adjudicate", capability: domain.CapProjectManage, required: []string{"skillId", "revision", "packageId", "packageHash", "expectedSequence", "operationId", "reason", "judgment"}, optional: []string{"supersedes"}},
	{path: "reviews/decide", action: "review_decide", capability: domain.CapProjectManage, required: []string{"skillId", "revision", "expectedSequence", "operationId", "decision", "reason"}},
	{path: "publish", action: "publish", capability: domain.CapProjectManage, required: []string{"skillId", "revision", "packageId", "packageHash", "expectedSequence", "operationId", "reason"}},
	{path: "status", action: "status", capability: domain.CapProjectManage, required: []string{"skillId", "revision", "expectedSequence", "operationId", "action", "reason"}},
	{path: "challenges", action: "challenge", capability: domain.CapProjectManage, required: []string{"skillId", "revision", "expectedSequence", "operationId", "reason"}},
	{path: "load-preview", action: "load_preview", capability: domain.CapProjectView, required: []string{"skillId", "revision", "caseId", "mode"}},
}

var (
	skillDigest           = regexp.MustCompile(`^[a-f0-9]{64}$`)
	skillIdentifier       = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,299}$`)
	skillIDPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,79}$`)
	skillOperationPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
)

func (s *Server) registerSkills(route routeFunc, write func(string, string, auth.Rule, handler, auditPolicy)) {
	for _, spec := range skillRoutes {
		pattern := "POST /api/v1/skills/" + spec.path
		tool := "dashboard.skills." + spec.action
		rule := auth.InBodyStrict(spec.capability)
		fn := s.skillHandler(spec)
		if spec.capability == domain.CapProjectView {
			route(pattern, tool, rule, fn)
		} else {
			write(pattern, tool, rule, fn, auditPolicy{redact: skillAuditInput})
		}
	}
}

func (s *Server) skillHandler(spec skillRoute) handler {
	return func(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any) (any, error) {
		// 旧 InBodyStrict 也允许 query；新 Skill 契约只接受 JSON body。
		if r.URL.RawQuery != "" {
			return nil, domain.Invalid("skill", "Skill 参数必须放在 JSON 请求体中")
		}
		if err := validateSkillBody(spec, body); err != nil {
			return nil, err
		}
		if s.deps.Skills == nil {
			return nil, disabledCapability("Skill 管理")
		}
		var capability string
		switch spec.capability {
		case domain.CapProjectView:
			capability = "view"
		case domain.CapProjectWrite:
			capability = "write"
		case domain.CapProjectManage:
			capability = "manage"
		default:
			return nil, domain.Invalid("skill", "未知 Skill 权限")
		}
		project, _ := body["projectId"].(string) // InBodyStrict 已按该项目完成授权。
		return s.deps.Skills.Call(ctx, domain.SkillCall{Action: spec.action, Actor: p.Actor(), Capability: capability, ProjectID: project, Payload: body})
	}
}

func validateSkillBody(spec skillRoute, body map[string]any) error {
	for field := range body {
		if field != "projectId" && !slices.Contains(spec.required, field) && !slices.Contains(spec.optional, field) {
			return domain.Invalid("skill", "Skill 请求含未知字段")
		}
	}
	for _, field := range append([]string{"projectId"}, spec.required...) {
		if _, exists := body[field]; !exists {
			return domain.Invalid("skill", "Skill 请求缺少必填字段")
		}
	}
	for field, value := range body {
		if err := validateSkillField(field, value); err != nil {
			return err
		}
	}
	return nil
}

func validateSkillField(field string, value any) error {
	if field == "judgment" {
		return validateSkillJudgment(value)
	}
	if field == "supersedes" && value == nil {
		return nil
	}
	if field == "expectedSequence" {
		return validateSkillSequence(value)
	}
	text, ok := value.(string)
	if !ok || (field != "query" && strings.TrimSpace(text) == "") || strings.ContainsAny(text, "\x00\r") {
		return domain.Invalid("skill", "Skill 字段必须是非空文本")
	}
	switch field {
	case "revision", "packageHash", "assessmentId", "supersedes":
		if !skillDigest.MatchString(text) {
			return domain.Invalid("skill", "Skill 版本与审阅包必须绑定 SHA-256")
		}
	case "reason", "query":
		return validateSkillText(field, text)
	case "decision":
		if text != "needs_evidence" && text != "block" {
			return domain.Invalid("skill", "未知审阅决定")
		}
	case "action":
		if text != "pause" && text != "withdraw" {
			return domain.Invalid("skill", "未知 Skill 状态操作")
		}
	case "mode":
		if text != "normal" {
			return domain.Invalid("skill", "仅允许预览正式加载准入")
		}
	default:
		return validateSkillIdentifier(field, text)
	}
	return nil
}

func skillAuditInput(body map[string]any) map[string]any {
	// 审阅理由、候选正文和证据内容不进入控制面审计。
	result := make(map[string]any)
	for _, key := range []string{"projectId", "skillId", "revision", "caseId", "experimentId", "operationId", "expectedSequence", "decision", "action", "packageId", "packageHash"} {
		if value, ok := body[key]; ok && validateSkillField(key, value) == nil {
			result[key] = value
		}
	}
	return result
}

func validateSkillSequence(value any) error {
	number, ok := value.(float64)
	if !ok || number < 0 || number > 1<<53-1 || math.Trunc(number) != number {
		return domain.Invalid("skill", "expectedSequence 必须为非负安全整数")
	}
	return nil
}

func validateSkillText(field, text string) error {
	maximum := 2000
	if field == "query" {
		maximum = 200
	}
	if utf8.RuneCountInString(text) > maximum {
		return domain.Invalid("skill", "Skill 文本超过长度限制")
	}
	return nil
}

func validateSkillIdentifier(field, text string) error {
	pattern := skillIdentifier
	switch field {
	case "skillId":
		pattern = skillIDPattern
	case "operationId":
		pattern = skillOperationPattern
	}
	if !pattern.MatchString(text) {
		return domain.Invalid("skill", "Skill 标识格式无效")
	}
	return nil
}
