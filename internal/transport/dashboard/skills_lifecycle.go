package dashboard

import (
	"slices"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func validateSkillDigests(value any) error {
	values, ok := value.([]any)
	if !ok || len(values) > 200 {
		return domain.Invalid("skill", "Skill 证据必须是摘要数组")
	}
	seen := make(map[string]bool)
	for _, value := range values {
		text, valid := value.(string)
		if !valid || !skillDigest.MatchString(text) || seen[text] {
			return domain.Invalid("skill", "Skill 证据摘要无效或重复")
		}
		seen[text] = true
	}
	return nil
}

// 校验每个动作自己的字段关系；宽泛 decision 枚举不能扩大旧审阅接口权限。
func validateSkillLifecycleBody(action string, body map[string]any) error {
	decision, _ := body["decision"].(string)
	allowed := map[string][]string{
		"review_decide":     {"needs_evidence", "block"},
		"impact_review":     {"restore", "retain", "supersede"},
		"challenge_resolve": {"addressed", "upheld", "superseded"},
	}
	if values, exists := allowed[action]; exists && !slices.Contains(values, decision) {
		return domain.Invalid("skill", "此操作不接受该审阅决定")
	}
	if action != "impact_review" && action != "challenge_resolve" {
		return nil
	}
	packageID, _ := body["packageId"].(string)
	if !skillDigest.MatchString(packageID) {
		return domain.Invalid("skill", "生命周期操作必须绑定固定包摘要")
	}
	replacement := body["replacementRevision"]
	needsReplacement := decision == "supersede" || decision == "superseded"
	if needsReplacement != (replacement != nil) {
		return domain.Invalid("skill", "替代决定与新版本不一致")
	}
	// 元素类型与重复项已由逐字段检查确认；业务证据资格仍由 Python 裁定。
	roots, _ := body["rootIds"].([]any)
	validations, _ := body["validationIds"].([]any)
	evidence, _ := body["evidenceIds"].([]any)
	if (decision == "restore" && len(validations) == 0) ||
		(decision == "addressed" && len(evidence) == 0) ||
		(decision == "supersede" && (len(roots) > 0 || len(validations) > 0)) {
		return domain.Invalid("skill", "该决定缺少或混入不适用的证据引用")
	}
	return nil
}
