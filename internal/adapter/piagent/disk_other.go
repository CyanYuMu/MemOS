//go:build !unix

package piagent

// diskFree 只有 unix 实现；其他平台返回未知。
func diskFree(string) (int64, bool) { return 0, false }
