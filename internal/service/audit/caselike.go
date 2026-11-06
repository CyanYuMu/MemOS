package audit

import (
	"encoding/json"
	"regexp"
)

// caseLikePatterns 与网关 audit/extract.py 的 CASE_LIKE_PATTERNS 一致。
// 只看不拦：命中只影响审计里的 case_like 字段。
var caseLikePatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)[0-9a-f]{8}-[0-9a-f]{4}-`),
	regexp.MustCompile(`(?i)uid[:：]?\s*\d{4,}`),
	regexp.MustCompile(`A2600[0-9A-Z]{5,}`),
	regexp.MustCompile(`(?i)\b[0-9a-f]{32,64}\b`),
	regexp.MustCompile(`\b\d{1,3}(?:\.\d{1,3}){3}\b`),
	regexp.MustCompile(`(?i)(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|top|xyz)\b`),
}

// LooksCaseSpecific 判断待写入内容是否含个案标识（UUID、uid、告警号、哈希、IP、域名）。
func LooksCaseSpecific(value any) bool {
	if value == nil {
		return false
	}
	text, ok := value.(string)
	if !ok {
		raw, err := json.Marshal(value)
		if err != nil {
			return false
		}
		text = string(raw)
	}
	for _, pattern := range caseLikePatterns {
		if pattern.MatchString(text) {
			return true
		}
	}
	return false
}
