package domain

import "unicode/utf8"

// TruncateRunes 按字符（rune）截断，避免按字节切坏多字节字符。
// 用于审计主体名、节流键这类来自外部输入又必须限长的字段。
func TruncateRunes(s string, limit int) string {
	if limit <= 0 {
		return ""
	}
	if utf8.RuneCountInString(s) <= limit {
		return s
	}
	return string([]rune(s)[:limit])
}
