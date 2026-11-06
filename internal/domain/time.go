package domain

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// UTCTime 是真 UTC 时间。上游契约 v2 保证所有时间是真实 UTC，控制面不再区分
// “假 UTC / 真 UTC”，也不再按记忆类型挑解析方式。
type UTCTime struct {
	time.Time
}

// utcLayouts 覆盖带偏移的标准写法，以及历史网关偶尔漏掉偏移的写法（按 UTC 解释）。
var utcLayouts = []string{
	time.RFC3339Nano,
	"2006-01-02T15:04:05",
	"2006-01-02 15:04:05",
}

// ParseUTC 解析上游时间；空串返回零值且不报错。
func ParseUTC(s string) (UTCTime, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return UTCTime{}, nil
	}
	var lastErr error
	for _, layout := range utcLayouts {
		parsed, err := time.Parse(layout, s)
		if err != nil {
			lastErr = err
			continue
		}
		return UTCTime{parsed.UTC()}, nil
	}
	return UTCTime{}, fmt.Errorf("parse utc time %q: %w", s, lastErr)
}

// String 便于日志与调试，统一输出 RFC3339（零值为空串）。
func (t UTCTime) String() string {
	if t.IsZero() {
		return ""
	}
	return t.UTC().Format(time.RFC3339Nano)
}

// MarshalJSON 输出 RFC3339 + Z；零值输出 null。
func (t UTCTime) MarshalJSON() ([]byte, error) {
	if t.IsZero() {
		return []byte("null"), nil
	}
	return json.Marshal(t.UTC().Format(time.RFC3339Nano))
}

// ClockLayout 是控制台展示时间的格式。
const ClockLayout = "2006-01-02 15:04:05"

var beijing = time.FixedZone("CST", 8*3600)

// BeijingClock 把记忆时间戳换成控制台展示的北京时间。上游给的是真 UTC，
// 这里只做展示时区转换；解析失败时原样返回，不让一行脏数据拖垮整页。
func BeijingClock(raw string) string {
	utc, err := ParseUTC(raw)
	if err != nil || utc.IsZero() {
		return raw
	}
	return FormatBeijing(utc.Time)
}

// BeijingClockOf 与 BeijingClock 同口径，输入是已解析的时间。
func BeijingClockOf(t UTCTime) string {
	if t.IsZero() {
		return ""
	}
	return FormatBeijing(t.Time)
}

// FormatBeijing 把真 UTC 时间格式化成控制台展示的北京时间。
func FormatBeijing(t time.Time) string { return t.In(beijing).Format(ClockLayout) }

// ParseBeijingMinute 解析前端 datetime-local 或 "YYYY-MM-DD HH:mm[:ss]" 形式的北京时间，精度到分钟。
func ParseBeijingMinute(s string) (time.Time, bool) {
	s = strings.Replace(strings.TrimSpace(s), "T", " ", 1)
	if len(s) < len("2006-01-02 15:04") {
		return time.Time{}, false
	}
	t, err := time.ParseInLocation("2006-01-02 15:04", s[:len("2006-01-02 15:04")], beijing)
	return t.UTC(), err == nil
}

// BeijingDay 返回 UTC 时刻所在的北京日期 "YYYY-MM-DD"。
func BeijingDay(t time.Time) string { return t.In(beijing).Format(time.DateOnly) }

// BeijingDayStart 返回北京日期 "YYYY-MM-DD" 当天零点对应的 UTC 时刻。
func BeijingDayStart(day string) (time.Time, bool) {
	t, err := time.ParseInLocation(time.DateOnly, day, beijing)
	return t.UTC(), err == nil
}
