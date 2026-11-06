//go:build !linux && !darwin

package piagent

import "syscall"

// 目标平台只有 linux 与 darwin；这里保留可编译的兜底，不承诺进程组语义。
func sysProcAttr() *syscall.SysProcAttr { return nil }

func killGroup(int) {}
