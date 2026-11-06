//go:build !unix

package piagent

import "errors"

// 非 unix 平台没有 flock；目标平台是 linux/amd64，这里只保证可编译。
var errLockBusy = errors.New("piagent: 会话锁被占用")

type fileLock struct {
	path string
}

func acquireFileLock(string) (*fileLock, error) { return &fileLock{}, nil }

func (l *fileLock) release() {}
