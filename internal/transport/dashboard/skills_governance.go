package dashboard

import (
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

var skillJudgmentEnums = map[string][]string{
	"taskDecision":    {"repair_confirmed", "stop_confirmed", "failed", "needs_evidence"},
	"historyDecision": {"retain", "infra_explained"},
	"usageDecision":   {"correct", "misused", "not_used", "unknown"},
	"applicability":   {"in_scope", "out_of_scope", "unknown"},
	"violations":      {"none", "serious", "unknown"},
}

func invalidSkillJudgment() error {
	return domain.Invalid("skill", "人工裁决字段无效")
}

// 这里只检查传输结构；证据身份、分支前提与最终补丁由数据面事务核对。
func validateSkillJudgment(value any) error {
	body, ok := value.(map[string]any)
	if !ok || len(body) != 9 {
		return invalidSkillJudgment()
	}
	for key, allowed := range skillJudgmentEnums {
		text, valid := body[key].(string)
		if !valid || !slices.Contains(allowed, text) {
			return invalidSkillJudgment()
		}
	}
	boundary, exists := body["boundary"]
	if !exists {
		return invalidSkillJudgment()
	}
	if boundary != nil {
		text, valid := boundary.(string)
		if !valid || !slices.Contains([]string{"not_applicable", "needs_evidence", "unresolved"}, text) {
			return invalidSkillJudgment()
		}
	}
	if err := validateSkillField("reason", body["reason"]); err != nil {
		return err
	}
	if err := validateSkillEvidenceRefs(body["eventRefs"], false); err != nil {
		return err
	}
	return validateSkillBranchClaims(body["branches"])
}

func validateSkillEvidenceRefs(value any, nonempty bool) error {
	items, ok := value.([]any)
	if !ok || len(items) > 100 || (nonempty && len(items) == 0) {
		return invalidSkillJudgment()
	}
	seen := make(map[string]bool)
	for _, item := range items {
		text, valid := item.(string)
		if !valid || strings.TrimSpace(text) == "" || utf8.RuneCountInString(text) > 300 || strings.ContainsAny(text, "\x00\r\n") || seen[text] {
			return invalidSkillJudgment()
		}
		seen[text] = true
	}
	return nil
}

func validateSkillBranchClaims(value any) error {
	items, ok := value.([]any)
	if !ok || len(items) > 100 {
		return invalidSkillJudgment()
	}
	for _, item := range items {
		claim, valid := item.(map[string]any)
		if !valid || len(claim) != 5 {
			return invalidSkillJudgment()
		}
		if err := validateSkillEvidenceRefs([]any{claim["branchId"]}, true); err != nil {
			return err
		}
		if err := validateSkillField("reason", claim["reason"]); err != nil {
			return err
		}
		for _, name := range []string{"conditionRefs", "actionRefs", "checkRefs"} {
			if err := validateSkillEvidenceRefs(claim[name], true); err != nil {
				return err
			}
		}
	}
	return nil
}
