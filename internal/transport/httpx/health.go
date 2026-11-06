package httpx

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"
)

// Check 是 readiness 的一项依赖检查。
type Check func(ctx context.Context) error

type namedCheck struct {
	name  string
	check Check
}

// Readiness 收集就绪检查。注册只在启动期发生，之后只读。
type Readiness struct {
	mu     sync.RWMutex
	checks []namedCheck
}

// NewReadiness 构造一个空的就绪检查集合。
func NewReadiness() *Readiness { return &Readiness{} }

// Add 注册一项检查。同名覆盖。
func (r *Readiness) Add(name string, c Check) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i := range r.checks {
		if r.checks[i].name == name {
			r.checks[i].check = c
			return
		}
	}
	r.checks = append(r.checks, namedCheck{name: name, check: c})
}

// Handler 返回 /readyz 处理器。全部通过 200；任一失败 503。
// 响应只写检查名与 ok/failed，不写错误详情，避免泄露上游地址。
func (r *Readiness) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		ctx, cancel := context.WithTimeout(req.Context(), 3*time.Second)
		defer cancel()

		r.mu.RLock()
		checks := append([]namedCheck(nil), r.checks...)
		r.mu.RUnlock()

		results := make(map[string]string, len(checks))
		ready := true
		for _, c := range checks {
			if err := c.check(ctx); err != nil {
				results[c.name] = "failed"
				ready = false
				continue
			}
			results[c.name] = "ok"
		}

		status := http.StatusOK
		state := "ready"
		if !ready {
			status = http.StatusServiceUnavailable
			state = "not_ready"
		}
		WriteJSON(w, status, map[string]any{"status": state, "checks": results})
	})
}

// HealthHandler 返回 /healthz：进程存活即 200，不查依赖。
func HealthHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		WriteJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

// MarshalIndent 是给测试与调试用的稳定序列化。
func MarshalIndent(v any) []byte {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return nil
	}
	return b
}
