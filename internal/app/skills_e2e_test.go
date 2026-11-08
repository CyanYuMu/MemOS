package app

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
)

// 使用真实 SQLite 会话、项目角色、控制台 HTTP 和 Skill HTTP 代理验证装配。
func TestE2ESkillManagementCapabilities(t *testing.T) {
	var mu sync.Mutex
	calls := []map[string]string{}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if r.Header.Get("Authorization") != "Bearer backend-only" || r.Header.Get("X-Skill-Project") != "cube-a" || body["projectId"] != "cube-a" {
			t.Error("untrusted credentials or project")
		}
		mu.Lock()
		calls = append(calls, map[string]string{"actor": r.Header.Get("X-Skill-Actor"), "capability": r.Header.Get("X-Skill-Capability"), "path": r.URL.Path})
		mu.Unlock()
		if r.URL.Path == "/v1/skills/publish" {
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"error":{"code":"state_conflict","message":"http://private/secret","details":{"currentSequence":3}}}`))
			return
		}
		_, _ = w.Write([]byte(`{"data":{"jobId":"job-1","status":"queued","candidate":{"rule_id":"r1"}}}`))
	}))
	defer upstream.Close()
	e := newE2EConfig(t, func(cfg *config.Config) { cfg.Skill = config.SkillConfig{BaseURL: upstream.URL, Token: "backend-only"} })
	seedWorld(t, e)
	alice, bob, carol := e.loginAs("alice"), e.loginAs("bob"), e.loginAs("carol")
	for _, c := range []*e2eClient{alice, bob, carol} {
		c.headers = map[string]string{"X-Skill-Actor": "forged-owner", "X-Skill-Capability": "manage"}
	}
	base := `"projectId":"cube-a","skillId":"fixture","revision":"` + strings.Repeat("a", 64) + `"`
	evaluate := "{" + base + `,"caseId":"dev-fixture","operationId":"eval-1"}`
	review := "{" + base + "}"
	publish := "{" + base + `,"expectedSequence":0,"operationId":"publish-1","reason":"已审阅","packageId":"review-1","packageHash":"` + strings.Repeat("b", 64) + `"}`
	expect(t, "viewer read", alice.post("/api/v1/skills/list", `{"projectId":"cube-a"}`), http.StatusOK)
	expect(t, "viewer cannot evaluate", alice.post("/api/v1/skills/evaluations/start", evaluate), http.StatusForbidden)
	expect(t, "editor evaluate", bob.post("/api/v1/skills/evaluations/start", evaluate), http.StatusOK)
	expect(t, "editor cannot prepare", bob.post("/api/v1/skills/reviews/prepare", review), http.StatusForbidden)
	expect(t, "editor cannot publish", bob.post("/api/v1/skills/publish", publish), http.StatusForbidden)
	expect(t, "owner prepare", carol.post("/api/v1/skills/reviews/prepare", review), http.StatusOK)
	conflict := carol.post("/api/v1/skills/publish", publish)
	expect(t, "owner conflict", conflict, http.StatusConflict)
	if conflict.obj()["errorCode"] != "state_conflict" || conflict.obj()["currentSequence"] != float64(3) || strings.Contains(conflict.raw, "private") {
		t.Fatalf("unsafe conflict: %s", conflict.raw)
	}
	expect(t, "foreign project", alice.post("/api/v1/skills/list", `{"projectId":"cube-b"}`), http.StatusForbidden)
	expect(t, "explicit project", alice.post("/api/v1/skills/list", `{}`), http.StatusBadRequest)

	expect(t, "viewer experiments", alice.post("/api/v1/skills/experiments/list", `{"projectId":"cube-a"}`), http.StatusOK)
	expect(t, "viewer experiment detail", alice.post("/api/v1/skills/experiments/detail", `{"projectId":"cube-a","experimentId":"experiment-1"}`), http.StatusOK)
	experiment := `{"projectId":"cube-a","experimentId":"experiment-1","operationId":"start-1"}`
	expect(t, "viewer cannot start experiment", alice.post("/api/v1/skills/experiments/start", experiment), http.StatusForbidden)
	expect(t, "editor start experiment", bob.post("/api/v1/skills/experiments/start", experiment), http.StatusOK)
	expect(t, "foreign experiment", alice.post("/api/v1/skills/experiments/detail", `{"projectId":"cube-b","experimentId":"experiment-1"}`), http.StatusForbidden)
	mu.Lock()
	defer mu.Unlock()
	if len(calls) != 7 {
		t.Fatalf("calls=%v: authorization failed or mutation retried", calls)
	}
	for i, want := range []struct{ actor, capability string }{{"alice", "view"}, {"bob", "write"}, {"carol", "manage"}, {"carol", "manage"}, {"alice", "view"}, {"alice", "view"}, {"bob", "write"}} {
		if calls[i]["actor"] != want.actor || calls[i]["capability"] != want.capability {
			t.Fatalf("call %d=%v", i, calls[i])
		}
	}
}
