package httpx

import (
	"strconv"
	"strings"
)

// 请求体解码后的取值辅助。Dashboard、admin、REST 三个入站面共用同一套语义：
// 类型不符时返回零值或调用方给的默认值，不做隐式转换猜测。

// String 取字符串并去掉首尾空白；非字符串返回空串。
func String(value any) string {
	text, _ := value.(string)
	return strings.TrimSpace(text)
}

// Int 取整数；JSON 数字解出来是 float64，也接受数字字符串。
func Int(value any, def int) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	case string:
		if parsed, err := strconv.Atoi(strings.TrimSpace(typed)); err == nil {
			return parsed
		}
	}
	return def
}

// Bool 取布尔；接受 JSON 布尔、常见字符串词表与数字。
func Bool(value any, def bool) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		switch strings.ToLower(strings.TrimSpace(typed)) {
		case "true", "1", "yes", "on":
			return true
		case "false", "0", "no", "off":
			return false
		}
	case float64:
		return typed != 0
	}
	return def
}

// StringList 取字符串数组；单个字符串视为一项，空项被丢弃。
func StringList(value any) []string {
	switch typed := value.(type) {
	case string:
		if typed = strings.TrimSpace(typed); typed == "" {
			return nil
		}
		return []string{typed}
	case []string:
		return append([]string(nil), typed...)
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			if text, ok := item.(string); ok {
				if text = strings.TrimSpace(text); text != "" {
					out = append(out, text)
				}
			}
		}
		return out
	}
	return nil
}
