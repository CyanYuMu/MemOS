//go:build !linux

package piagent

// 非 linux 平台没有 cgroup v2；本地开发与测试不做内存准入拦截。
func memoryAvailable() (available, limit int64, ok bool) { return 0, 0, false }
