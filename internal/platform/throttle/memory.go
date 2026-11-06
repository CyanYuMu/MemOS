package throttle

import (
	"context"
	"sync"
	"time"
)

// maxTrackedKeys 限制节流表大小，超限时丢弃最旧的条目，避免被随机用户名撑爆内存。
const maxTrackedKeys = 4096

type failure struct {
	count int
	first time.Time
}

// Memory 按用户名与来源 IP 做固定窗口的失败计数，状态只在进程内存里。
type Memory struct {
	mu     sync.Mutex
	limit  int
	window time.Duration
	now    func() time.Time
	users  map[string]*failure
	ips    map[string]*failure
}

// NewMemory 构造进程内节流：窗口从第一次失败开始计。
func NewMemory(limit int, window time.Duration) *Memory {
	return &Memory{
		limit:  limit,
		window: window,
		now:    time.Now,
		users:  make(map[string]*failure),
		ips:    make(map[string]*failure),
	}
}

func (m *Memory) Blocked(_ context.Context, user, ip string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()
	return m.over(m.users, user, now) || (ip != "" && m.over(m.ips, ip, now))
}

func (m *Memory) Fail(_ context.Context, user, ip string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.now()
	m.bump(m.users, user, now)
	if ip != "" {
		m.bump(m.ips, ip, now)
	}
}

func (m *Memory) Clear(_ context.Context, user string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.users, user)
}

func (m *Memory) Reset(_ context.Context) {
	m.mu.Lock()
	defer m.mu.Unlock()
	clear(m.users)
	clear(m.ips)
}

func (m *Memory) over(entries map[string]*failure, key string, now time.Time) bool {
	e, ok := entries[key]
	if !ok {
		return false
	}
	if now.Sub(e.first) > m.window {
		delete(entries, key)
		return false
	}
	return e.count >= m.limit
}

func (m *Memory) bump(entries map[string]*failure, key string, now time.Time) {
	if e, ok := entries[key]; ok && now.Sub(e.first) <= m.window {
		e.count++
		return
	}
	if _, exists := entries[key]; !exists && len(entries) >= maxTrackedKeys {
		evictOldest(entries)
	}
	entries[key] = &failure{count: 1, first: now}
}

func evictOldest(entries map[string]*failure) {
	var (
		oldestKey string
		oldest    time.Time
		found     bool
	)
	for k, e := range entries {
		if !found || e.first.Before(oldest) {
			oldestKey, oldest, found = k, e.first, true
		}
	}
	if found {
		delete(entries, oldestKey)
	}
}
