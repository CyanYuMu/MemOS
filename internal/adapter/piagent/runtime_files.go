package piagent

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// pendingMessage 是受理队列的一条记录。只有 accepted 记录带正文；完成、取消、
// 失败只追加一条同 id 的状态记录，读取时按 id 折叠。
type pendingMessage struct {
	ID     string    `json:"id"`
	Text   string    `json:"text,omitempty"`
	At     time.Time `json:"at"`
	Status string    `json:"status"`
}

const (
	pendingAccepted  = "accepted"
	pendingDone      = "done"
	pendingCancelled = "cancelled"
	pendingFailed    = "failed"
	pendingAbandoned = "abandoned"
	// pendingRetry 表示这一轮没执行完（容量满/进程退出/超时被杀），留给重启后的
	// needs_retry，不要当成终态丢掉。
	pendingRetry = "retry"
)

var emptyHistoryDoc = json.RawMessage(`{"messages":[],"total":0,"start":0}`)

func (d agentDirs) lockPath(sessionID string) string {
	return filepath.Join(d.runtime, sessionID+".lock")
}

func (d agentDirs) historyPath() string {
	return filepath.Join(d.runtime, "history.json")
}

func (d agentDirs) pendingPath() string {
	return filepath.Join(d.runtime, "pending.jsonl")
}

// loadPending 读取受理队列并折叠状态，返回仍未完成的消息。读取后按未完成集合
// 重写文件，避免长期会话把它撑大。
func loadPending(path string) []pendingMessage {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	type entry struct {
		msg  pendingMessage
		live bool
	}
	order := make([]*entry, 0, 8)
	index := make(map[string]*entry)
	scanner := bufio.NewScanner(bytes.NewReader(body))
	scanner.Buffer(make([]byte, 64<<10), 1<<20)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		var msg pendingMessage
		if err := json.Unmarshal(line, &msg); err != nil || msg.ID == "" {
			continue
		}
		item := index[msg.ID]
		if item == nil {
			item = &entry{msg: msg, live: msg.Status == pendingAccepted}
			index[msg.ID] = item
			order = append(order, item)
			continue
		}
		if msg.Text != "" {
			item.msg.Text = msg.Text
		}
		switch msg.Status {
		case pendingAccepted, pendingRetry:
			item.live = true
		case pendingDone, pendingCancelled, pendingFailed, pendingAbandoned:
			item.live = false
		}
	}
	out := make([]pendingMessage, 0, len(order))
	for _, item := range order {
		if item.live {
			out = append(out, item.msg)
		}
	}
	if len(out) != len(order) {
		_ = rewritePending(path, out)
	}
	return out
}

// appendPending 先 fsync 再返回：Submit 只有在这一步成功之后才回 202。
func appendPending(path string, msg pendingMessage) error {
	body, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("piagent: encode pending: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("piagent: open pending: %w", err)
	}
	if _, err := f.Write(append(body, '\n')); err != nil {
		_ = f.Close()
		return fmt.Errorf("piagent: write pending: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("piagent: sync pending: %w", err)
	}
	return f.Close()
}

// appendPendingBatch 一次写入多条状态记录（同一次 fsync）：中断整条队列时
// 不该产生几十次磁盘同步。
func appendPendingBatch(path string, msgs []pendingMessage, status string) error {
	if len(msgs) == 0 {
		return nil
	}
	var buf bytes.Buffer
	for _, msg := range msgs {
		body, err := json.Marshal(pendingMessage{ID: msg.ID, Status: status})
		if err != nil {
			return fmt.Errorf("piagent: encode pending: %w", err)
		}
		buf.Write(body)
		buf.WriteByte('\n')
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("piagent: open pending: %w", err)
	}
	if _, err := f.Write(buf.Bytes()); err != nil {
		_ = f.Close()
		return fmt.Errorf("piagent: write pending: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("piagent: sync pending: %w", err)
	}
	return f.Close()
}

func rewritePending(path string, msgs []pendingMessage) error {
	var buf bytes.Buffer
	for _, msg := range msgs {
		body, err := json.Marshal(msg)
		if err != nil {
			return err
		}
		buf.Write(body)
		buf.WriteByte('\n')
	}
	return writeFileAtomic(path, buf.Bytes())
}

// readHistory 返回上一轮结束时落下的 get_messages 原始结果。文件不存在或损坏时
// 返回错误，由调用方回退到空历史。
func readHistory(path string) (json.RawMessage, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(bytes.TrimSpace(body)) == 0 {
		return nil, errors.New("piagent: 历史快照为空")
	}
	if !json.Valid(body) {
		return nil, errors.New("piagent: 历史快照不是合法 JSON")
	}
	return json.RawMessage(body), nil
}

func writeHistory(path string, raw json.RawMessage) error {
	if len(raw) == 0 || !json.Valid(raw) {
		return errors.New("piagent: 历史快照不是合法 JSON")
	}
	return writeFileAtomic(path, raw)
}
