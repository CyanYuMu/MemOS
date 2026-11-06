//go:build !linux

package piagent

// 孤儿清扫依赖 /proc，只有 linux 实现；其他平台是无操作。
func sweepOrphans() int { return 0 }
