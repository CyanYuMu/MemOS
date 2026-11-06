//go:build linux

package piagent

import (
	"os"
	"strconv"
	"strings"
)

// memoryAvailable 读取 cgroup v2 的剩余内存。返回 ok=false 表示读不到或没有上限，
// 此时不做准入拦截（不能因为读不到就拒绝服务）。
func memoryAvailable() (available, limit int64, ok bool) {
	current, err := readCgroupInt("/sys/fs/cgroup/memory.current")
	if err != nil {
		return 0, 0, false
	}
	raw, err := os.ReadFile("/sys/fs/cgroup/memory.max")
	if err != nil {
		return 0, 0, false
	}
	text := strings.TrimSpace(string(raw))
	if text == "max" {
		return 0, 0, false
	}
	max, err := strconv.ParseInt(text, 10, 64)
	if err != nil || max <= 0 {
		return 0, 0, false
	}
	return max - current, max, true
}

func readCgroupInt(path string) (int64, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
}
