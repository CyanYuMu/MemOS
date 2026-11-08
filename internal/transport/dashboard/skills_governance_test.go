package dashboard

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func judgmentTestBody() map[string]any {
	return map[string]any{
		"projectId": "cube-a", "skillId": "fixed-skill", "revision": strings.Repeat("a", 64),
		"packageId": strings.Repeat("b", 64), "packageHash": strings.Repeat("c", 64),
		"expectedSequence": float64(2), "operationId": "judgment-1", "reason": "Synthetic test only",
		"supersedes": nil,
		"judgment": map[string]any{
			"taskDecision": "needs_evidence", "historyDecision": "retain", "usageDecision": "unknown",
			"applicability": "unknown", "boundary": nil, "violations": "unknown",
			"reason": "Synthetic missing evidence", "eventRefs": []any{}, "branches": []any{},
		},
	}
}

func TestAdjudicationRoutesRequireManageAndPreserveTrustedIdentity(t *testing.T) {
	srv := newTestServer(t)
	gateway := &captureSkillGateway{}
	srv.deps.Skills = gateway
	body, err := json.Marshal(judgmentTestBody())
	if err != nil {
		t.Fatal(err)
	}
	rec := call(t, srv, http.MethodPost, "/api/v1/skills/adjudications/submit", string(body))
	if rec.Code != 200 || len(gateway.calls) != 1 || gateway.calls[0].Action != "adjudicate" || gateway.calls[0].Capability != "manage" {
		t.Fatalf("status=%d body=%s calls=%v", rec.Code, rec.Body.String(), gateway.calls)
	}
	srv.deps.Accounts = viewerAccounts{}
	rec = callAs(t, srv, viewerToken, http.MethodPost, "/api/v1/skills/adjudications/submit", string(body))
	if rec.Code != 403 || len(gateway.calls) != 1 {
		t.Fatalf("viewer status=%d", rec.Code)
	}
}

func TestAdjudicationRejectsUnknownNestedFieldsAndAuditOmitsJudgment(t *testing.T) {
	spec := skillRoute{required: []string{"skillId", "revision", "packageId", "packageHash", "expectedSequence", "operationId", "reason", "judgment"}, optional: []string{"supersedes"}}
	for _, field := range []string{"extra", "boundary", "branches", "eventRefs", "taskDecision"} {
		t.Run(field, func(t *testing.T) {
			body := judgmentTestBody()
			value, ok := body["judgment"].(map[string]any)
			if !ok {
				t.Fatal("fixture shape")
			}
			value[field] = map[string]any{"forged": true}
			if validateSkillBody(spec, body) == nil {
				t.Fatalf("accepted invalid %s", field)
			}
		})
	}
	body := judgmentTestBody()
	if validateSkillBody(spec, body) != nil {
		t.Fatal("valid shape rejected")
	}
	audit := skillAuditInput(body)
	if audit["judgment"] != nil || audit["reason"] != nil {
		t.Fatal("private judgement entered audit")
	}
}
