//go:build unix

package piagent

import (
	"math"
	"syscall"
)

// diskFree 返回该路径所在文件系统的可用字节数。读不到就返回 ok=false：
// 存储观测不该影响主流程。
func diskFree(path string) (int64, bool) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, false
	}
	free := st.Bavail * uint64(st.Bsize)
	if free > math.MaxInt64 {
		return math.MaxInt64, true
	}
	return int64(free), true
}
