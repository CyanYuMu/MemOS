//go:build linux

package piagent

import (
	"bytes"
	"os"
	"strconv"
	"strings"
	"syscall"
)

// sweepOrphans 回收上一代进程遗留的 `pi --mode rpc`。只看同 UID 的进程：
// 同 Pod 内只有一个 memosd，不会误杀其他用户的进程。
func sweepOrphans() int {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return 0
	}
	uid := os.Getuid()
	killed := 0
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || pid <= 1 {
			continue
		}
		info, err := entry.Info()
		if err != nil {
			continue
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || int(stat.Uid) != uid {
			continue
		}
		cmdline, err := os.ReadFile("/proc/" + entry.Name() + "/cmdline")
		if err != nil || len(cmdline) == 0 {
			continue
		}
		args := strings.Join(strings.Split(string(bytes.TrimRight(cmdline, "\x00")), "\x00"), " ")
		if !strings.Contains(args, "--mode rpc") {
			continue
		}
		if err := syscall.Kill(-pid, syscall.SIGKILL); err != nil {
			_ = syscall.Kill(pid, syscall.SIGKILL)
		}
		killed++
	}
	return killed
}
