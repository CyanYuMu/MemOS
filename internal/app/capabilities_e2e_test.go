package app

import (
	"net/http"
	"testing"
)

// 图库与 ConfigMap 都没配置时，对应路由必须是 503；之前图库是“非 nil 的 nil 接口”，会 panic 成 500。
func TestE2EUnconfiguredCapabilitiesAre503(t *testing.T) {
	t.Parallel()
	e := newE2E(t)
	seedWorld(t, e)
	alice := e.loginAs("alice")
	for label, got := range map[string]result{
		"graph":     alice.get("/api/dashboard/graph/memories?tenant=cube-a"),
		"event tab": alice.post("/api/dashboard/project/memory/list", `{"projectId":"cube-a","memoryDetailType":"event_mem"}`),
		"models":    alice.get("/api/dashboard/models"),
	} {
		expect(t, label, got, http.StatusServiceUnavailable)
	}
}
