// Package page 提供内存切片分页，页码从 1 开始。
package page

// Slice 返回第 p 页、每页 size 条；越界返回空切片而不是 nil。
func Slice[T any](items []T, p, size int) []T {
	if size <= 0 {
		return []T{}
	}
	start := (p - 1) * size
	if start < 0 || start >= len(items) {
		return []T{}
	}
	return items[start:min(start+size, len(items))]
}
