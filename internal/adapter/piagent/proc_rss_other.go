//go:build !linux

package piagent

// processRSS 只有 linux 实现；其他平台返回 0。
func processRSS(int) int64 { return 0 }
