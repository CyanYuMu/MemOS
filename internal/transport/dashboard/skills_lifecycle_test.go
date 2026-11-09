package dashboard

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"
)

func TestLifecycleDigestArrayBound(t *testing.T) {
	values := make([]any, 200)
	for index := range values {
		values[index] = fmt.Sprintf("%064x", index)
	}
	if err := validateSkillDigests(values); err != nil {
		t.Fatalf("200 摘要应允许：%v", err)
	}
	if validateSkillDigests(append(values, fmt.Sprintf("%064x", 200))) == nil {
		t.Fatal("201 摘要应拒绝")
	}
}

func lifecycleTestBody(action string) map[string]any {
	body := map[string]any{"projectId": "cube-a", "skillId": "fixed-skill", "revision": strings.Repeat("a", 64)}
	if action == "impact_prepare" {
		return body
	}
	body["expectedSequence"] = float64(7)
	body["operationId"] = "lifecycle-1"
	body["reason"] = "用户授权的助手代办；仅提交已核对的固定证据"
	if action == "impact_scan" {
		return body
	}
	body["packageId"] = strings.Repeat("b", 64)
	body["packageHash"] = strings.Repeat("c", 64)
	body["replacementRevision"] = nil
	if action == "impact_review" {
		body["decision"] = "restore"
		body["rootIds"] = []any{strings.Repeat("d", 64)}
		body["validationIds"] = []any{strings.Repeat("e", 64)}
	} else {
		body["decision"] = "addressed"
		body["challengeId"] = strings.Repeat("d", 64)
		body["evidenceIds"] = []any{strings.Repeat("e", 64)}
	}
	return body
}

func TestLifecycleRoutesRequireManageAndTrustedActor(t *testing.T) {
	routes := map[string]string{"lifecycle/scan": "impact_scan", "lifecycle/prepare": "impact_prepare", "lifecycle/review": "impact_review", "challenges/resolve": "challenge_resolve"}
	for path, action := range routes {
		t.Run(action, func(t *testing.T) {
			srv := newTestServer(t)
			gateway := &captureSkillGateway{}
			srv.deps.Skills = gateway
			raw, err := json.Marshal(lifecycleTestBody(action))
			if err != nil {
				t.Fatal(err)
			}
			rec := call(t, srv, http.MethodPost, "/api/v1/skills/"+path, string(raw))
			if rec.Code != 200 || len(gateway.calls) != 1 || gateway.calls[0].Action != action || gateway.calls[0].Capability != "manage" || gateway.calls[0].Actor == "" {
				t.Fatalf("status=%d body=%s calls=%v", rec.Code, rec.Body.String(), gateway.calls)
			}
			srv.deps.Accounts = viewerAccounts{}
			rec = callAs(t, srv, viewerToken, http.MethodPost, "/api/v1/skills/"+path, string(raw))
			if rec.Code != 403 || len(gateway.calls) != 1 {
				t.Fatalf("viewer status=%d calls=%d", rec.Code, len(gateway.calls))
			}
		})
	}
}

func TestLifecycleRejectsForgedFactsAndInvalidBindings(t *testing.T) {
	cases := []struct {
		name, path, action string
		change             func(map[string]any)
	}{
		{"actor", "lifecycle/scan", "impact_scan", func(b map[string]any) { b["actor"] = "user" }},
		{"observations", "lifecycle/scan", "impact_scan", func(b map[string]any) { b["observations"] = []any{} }},
		{"unsafe-sequence", "lifecycle/scan", "impact_scan", func(b map[string]any) { b["expectedSequence"] = float64(1 << 54) }},
		{"duplicate-roots", "lifecycle/review", "impact_review", func(b map[string]any) { b["rootIds"] = []any{strings.Repeat("d", 64), strings.Repeat("d", 64)} }},
		{"bad-evidence", "challenges/resolve", "challenge_resolve", func(b map[string]any) { b["evidenceIds"] = []any{"private/path"} }},
		{"missing-validation", "lifecycle/review", "impact_review", func(b map[string]any) { b["validationIds"] = []any{} }},
		{"missing-evidence", "challenges/resolve", "challenge_resolve", func(b map[string]any) { b["evidenceIds"] = []any{} }},
		{"wrong-decision", "lifecycle/review", "impact_review", func(b map[string]any) { b["decision"] = "addressed" }},
		{"old-route-cannot-restore", "reviews/decide", "impact_scan", func(b map[string]any) { b["decision"] = "restore" }},
		{"missing-replacement", "lifecycle/review", "impact_review", func(b map[string]any) { b["decision"] = "supersede" }},
		{"restore-with-replacement", "lifecycle/review", "impact_review", func(b map[string]any) { b["replacementRevision"] = strings.Repeat("f", 64) }},
		{"unbound-package", "lifecycle/review", "impact_review", func(b map[string]any) { b["packageId"] = "unbound" }},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			srv := newTestServer(t)
			gateway := &captureSkillGateway{}
			srv.deps.Skills = gateway
			body := lifecycleTestBody(tt.action)
			tt.change(body)
			raw, err := json.Marshal(body)
			if err != nil {
				t.Fatal(err)
			}
			rec := call(t, srv, http.MethodPost, "/api/v1/skills/"+tt.path, string(raw))
			if rec.Code != 400 || len(gateway.calls) != 0 {
				t.Fatalf("status=%d body=%s calls=%v", rec.Code, rec.Body.String(), gateway.calls)
			}
		})
	}
}

func TestLifecycleRetainAndSupersedeHaveDistinctBindings(t *testing.T) {
	for _, decision := range []string{"retain", "supersede"} {
		srv := newTestServer(t)
		gateway := &captureSkillGateway{}
		srv.deps.Skills = gateway
		body := lifecycleTestBody("impact_review")
		body["decision"] = decision
		body["rootIds"] = []any{}
		body["validationIds"] = []any{}
		if decision == "supersede" {
			body["replacementRevision"] = strings.Repeat("f", 64)
		}
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		rec := call(t, srv, http.MethodPost, "/api/v1/skills/lifecycle/review", string(raw))
		if rec.Code != 200 || len(gateway.calls) != 1 {
			t.Fatalf("%s status=%d body=%s", decision, rec.Code, rec.Body.String())
		}
	}
}

func TestNewTaskJudgmentsPreserveOutcomeDistinctions(t *testing.T) {
	for _, decision := range []string{"healthy_confirmed", "unresolved_confirmed"} {
		body := judgmentTestBody()
		judgment, ok := body["judgment"].(map[string]any)
		if !ok {
			t.Fatal("fixture shape")
		}
		judgment["taskDecision"] = decision
		if err := validateSkillJudgment(judgment); err != nil {
			t.Errorf("%s: %v", decision, err)
		}
	}
}
