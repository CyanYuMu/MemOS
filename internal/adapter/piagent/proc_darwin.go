//go:build darwin

package piagent

import "syscall"

// darwin 没有 Pdeathsig；父进程被强杀后的残留由下次启动的孤儿清扫兜底
// （仅 linux 实现，darwin 开发机不做全局杀进程）。
func sysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{Setpgid: true}
}

func killGroup(pgid int) {
	if pgid <= 0 {
		return
	}
	_ = syscall.Kill(-pgid, syscall.SIGKILL)
}
