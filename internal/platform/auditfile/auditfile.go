// Package auditfile 提供审计归档用的追加式 JSONL 文件。
//
// 与进程日志分开：审计文件保存完整正文，不截断；按大小轮转，但默认不按
// 份数或天数删除（MaxBackups/MaxAge 为 0），由运维按磁盘容量决定保留策略。
package auditfile

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"gopkg.in/natefinch/lumberjack.v2"
)

// ErrClosed 表示写入器已经关闭。
var ErrClosed = errors.New("auditfile: writer is closed")

// Options 是审计文件的轮转参数。MaxBackups 与 MaxAgeDays 为 0 表示不删除。
type Options struct {
	Path       string
	MaxSizeMB  int
	MaxBackups int
	MaxAgeDays int
	Compress   bool
}

// Writer 串行追加整行，保证并发调用下一条记录不会被拆开。
type Writer struct {
	mu     sync.Mutex
	lj     *lumberjack.Logger
	closed bool
}

// New 打开审计文件；目录不存在时创建。启动期打开一次，让权限、只读挂载和
// 磁盘满在进程启动时暴露，而不是等到第一条审计才发现。
func New(opts Options) (*Writer, error) {
	if opts.Path == "" {
		return nil, errors.New("auditfile: path is empty")
	}
	dir := filepath.Dir(opts.Path)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("create audit dir %s: %w", dir, err)
	}
	//nolint:gosec // 路径来自部署清单的固定挂载点，不是用户输入。
	f, err := os.OpenFile(opts.Path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o640)
	if err != nil {
		return nil, fmt.Errorf("open audit file %s: %w", opts.Path, err)
	}
	_ = f.Close()
	if opts.MaxSizeMB <= 0 {
		opts.MaxSizeMB = 1024
	}
	return &Writer{lj: &lumberjack.Logger{
		Filename:   opts.Path,
		MaxSize:    opts.MaxSizeMB,
		MaxBackups: opts.MaxBackups,
		MaxAge:     opts.MaxAgeDays,
		Compress:   opts.Compress,
	}}, nil
}

// WriteLine 追加一行；缺换行时补上。一次 Write 保证整行原子落盘。
func (w *Writer) WriteLine(line []byte) error {
	if w == nil {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return ErrClosed
	}
	if len(line) == 0 || line[len(line)-1] != '\n' {
		line = append(append(make([]byte, 0, len(line)+1), line...), '\n')
	}
	_, err := w.lj.Write(line)
	return err
}

// Close 关闭文件，可重复调用。
func (w *Writer) Close() error {
	if w == nil {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.closed {
		return nil
	}
	w.closed = true
	return w.lj.Close()
}
