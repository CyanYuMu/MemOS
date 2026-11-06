//go:build linux

package piagent

import "syscall"

// sysProcAttr 让 pi 成为新进程组的组长，Stop 时可以对整组发信号。
// Pdeathsig 保证 console-be 被强杀时内核也回收子进程。
func sysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
}

func killGroup(pgid int) {
	if pgid <= 0 {
		return
	}
	_ = syscall.Kill(-pgid, syscall.SIGKILL)
}
