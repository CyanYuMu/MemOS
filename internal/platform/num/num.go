// Package num 提供共用的整数边界处理，避免每个包各写一份 clamp。
package num

// Clamp 把 v 限制在 [lo, hi]。
func Clamp(v, lo, hi int) int { return min(max(v, lo), hi) }

// WithDefault 在 v 为 0 时取 def，再把结果限制在 [lo, hi]。
func WithDefault(v, def, lo, hi int) int {
	if v == 0 {
		v = def
	}
	return Clamp(v, lo, hi)
}
