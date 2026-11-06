//go:build unix

package piagent

import (
	"errors"
	"fmt"
	"os"
	"syscall"
)

// errLockBusy 表示会话锁被另一个进程（或本进程的另一代）持有。调用方据此判断
// 是「还有一个 pi 在写这份会话」，而不是普通的 IO 失败。
var errLockBusy = errors.New("piagent: 会话锁被占用")

// fileLock 是会话级排他锁。持锁进程退出（含被 kill）后内核自动释放，
// 因此不需要清理 stale lock 文件。
type fileLock struct {
	path string
	file *os.File
}

func acquireFileLock(path string) (*fileLock, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("piagent: open session lock: %w", err)
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, errLockBusy
		}
		return nil, fmt.Errorf("piagent: flock session lock: %w", err)
	}
	return &fileLock{path: path, file: f}, nil
}

func (l *fileLock) release() {
	if l == nil || l.file == nil {
		return
	}
	_ = syscall.Flock(int(l.file.Fd()), syscall.LOCK_UN)
	_ = l.file.Close()
	l.file = nil
}
