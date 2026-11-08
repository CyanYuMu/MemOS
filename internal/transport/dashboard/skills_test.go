package dashboard

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type captureSkillGateway struct {
	calls []domain.SkillCall
	err   error
}

func (g *captureSkillGateway) Call(_ context.Context, c domain.SkillCall) (json.RawMessage, error) {
	g.calls = append(g.calls, c)
	return json.RawMessage(`{"jobId":"job-1","candidate":{"rule_id":"r1"}}`), g.err
}

func TestSkillRoutesStrictProjectAndTrustedActor(t *testing.T) {
	srv := newTestServer(t)
	gateway := &captureSkillGateway{}
	srv.deps.Skills = gateway
	req := httptest.NewRequestWithContext(context.Background(), http.MethodPost, "/api/v1/skills/evaluations/start", strings.NewReader(skillTestBody("evaluate")))
	req.Header.Set("Authorization", "Bearer "+adminToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Skill-Actor", "forged")
	req.Header.Set("X-Skill-Capability", "view")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != 200 || len(gateway.calls) != 1 {
		t.Fatalf("status=%d body=%s calls=%v", rec.Code, rec.Body.String(), gateway.calls)
	}
	got := gateway.calls[0]
	if got.Actor != domain.AdminTokenUsername || got.Capability != "write" || got.ProjectID != "cube-a" || got.Action != "evaluate" {
		t.Fatalf("untrusted actor or wrong scope: %+v", got)
	}
	if !strings.Contains(rec.Body.String(), `"rule_id":"r1"`) {
		t.Fatal("canonical payload rewritten")
	}
	srv.deps.Accounts = sessionAccounts{}
	missing := callAs(t, srv, sessionToken, http.MethodPost, "/api/v1/skills/list", `{}`)
	foreign := callAs(t, srv, sessionToken, http.MethodPost, "/api/v1/skills/list", `{"projectId":"cube-b"}`)
	if missing.Code != 400 || foreign.Code != 403 || len(gateway.calls) != 1 {
		t.Fatalf("missing=%d foreign=%d", missing.Code, foreign.Code)
	}
}

func TestSkillRoutesCapabilities(t *testing.T) {
	paths := []struct {
		path, action string
		write        bool
	}{
		{"list", "list", false},
		{"experiments/list", "experiments_list", false},
		{"experiments/detail", "experiment_detail", false},
		{"experiments/start", "experiment_start", true},
		{"detail", "detail", false},
		{"evidence", "evidence", false},
		{"jobs/detail", "job", false},
		{"load-preview", "load_preview", false},
		{"evaluations/start", "evaluate", true},
		{"reviews/prepare", "review_prepare", true},
		{"reviews/decide", "review_decide", true},
		{"publish", "publish", true},
		{"status", "status", true},
		{"challenges", "challenge", true},
	}
	for _, tt := range paths {
		t.Run(tt.path, func(t *testing.T) {
			srv := newTestServer(t)
			gateway := &captureSkillGateway{}
			srv.deps.Skills = gateway
			rec := call(t, srv, http.MethodPost, "/api/v1/skills/"+tt.path, skillTestBody(tt.action))
			if rec.Code != 200 {
				t.Fatalf("admin=%d %s", rec.Code, rec.Body.String())
			}
			srv.deps.Accounts = viewerAccounts{}
			rec = callAs(t, srv, viewerToken, http.MethodPost, "/api/v1/skills/"+tt.path, skillTestBody(tt.action))
			want := 200
			if tt.write {
				want = 403
			}
			if rec.Code != want {
				t.Fatalf("viewer=%d want=%d %s", rec.Code, want, rec.Body.String())
			}
		})
	}
}

func TestSkillRoutesRejectUnknownAndInvalidInput(t *testing.T) {
	for _, entry := range []struct{ path, body string }{
		{"list", `{"projectId":"cube-a","actor":"forged"}`},
		{"list", `{"projectId":"cube-a","path":"/tmp/oracle"}`},
		{"experiments/start", `{"projectId":"cube-a","experimentId":"experiment-1","operationId":"op-1","path":"/tmp/oracle"}`},
		{"experiments/start", `{"projectId":"cube-a","experimentId":"experiment-1","operationId":"op-1","report":{"status":"passed"}}`},
		{"experiments/detail", `{"projectId":"cube-a"}`},
		{"detail", `{"projectId":"cube-a","skillId":"x","revision":"latest"}`},
		{"evaluations/start", strings.ReplaceAll(skillTestBody("evaluate"), `"operationId":"op-1"`, `"operationId":""`)},
		{"status", strings.ReplaceAll(skillTestBody("status"), `"expectedSequence":0`, `"expectedSequence":1.5`)},
		{"status", strings.ReplaceAll(skillTestBody("status"), `"reason":"人工检查"`, `"reason":"  "`)},
		{"status", strings.ReplaceAll(skillTestBody("status"), `"action":"pause"`, `"action":"approve"`)},
		{"load-preview", strings.ReplaceAll(skillTestBody("load_preview"), `"mode":"normal"`, `"mode":"candidate_eval"`)},
	} {
		srv := newTestServer(t)
		gateway := &captureSkillGateway{}
		srv.deps.Skills = gateway
		rec := call(t, srv, http.MethodPost, "/api/v1/skills/"+entry.path, entry.body)
		if rec.Code != 400 || len(gateway.calls) != 0 {
			t.Errorf("accepted %s: %d %s", entry.body, rec.Code, rec.Body.String())
		}
	}
}

func TestSkillUnavailableAndConflict(t *testing.T) {
	srv := newTestServer(t)
	if rec := call(t, srv, http.MethodPost, "/api/v1/skills/list", `{"projectId":"cube-a"}`); rec.Code != 503 {
		t.Fatalf("disabled=%d", rec.Code)
	}
	sequence := int64(4)
	srv.deps.Skills = &captureSkillGateway{err: &domain.SkillServiceError{Status: 409, Code: "sequence_conflict", CurrentSequence: &sequence}}
	rec := call(t, srv, http.MethodPost, "/api/v1/skills/publish", skillTestBody("publish"))
	if rec.Code != 409 || !strings.Contains(rec.Body.String(), `"currentSequence":4`) || !strings.Contains(rec.Body.String(), `"errorCode":"sequence_conflict"`) {
		t.Fatalf("conflict=%d %s", rec.Code, rec.Body.String())
	}
}

func skillTestBody(action string) string {
	base := `"projectId":"cube-a"`
	if action == "list" || action == "experiments_list" {
		return "{" + base + "}"
	}
	if action == "experiment_detail" {
		return "{" + base + `,"experimentId":"experiment-1"}`
	}
	if action == "experiment_start" {
		return "{" + base + `,"experimentId":"experiment-1","operationId":"op-1"}`
	}
	if action == "job" {
		return "{" + base + `,"jobId":"job-1"}`
	}
	base += `,"skillId":"mock-fixture","revision":"` + strings.Repeat("a", 64) + `"`
	switch action {
	case "evidence":
		base += `,"ruleId":"r1"`
	case "evaluate":
		base += `,"caseId":"dev-mock","operationId":"op-1"`
	case "load_preview":
		base += `,"caseId":"dev-mock","mode":"normal"`
	case "review_decide", "publish", "status", "challenge":
		base += `,"expectedSequence":0,"operationId":"op-1","reason":"人工检查"`
		switch action {
		case "review_decide":
			base += `,"decision":"needs_evidence"`
		case "publish":
			base += `,"packageId":"review-1","packageHash":"` + strings.Repeat("b", 64) + `"`
		case "status":
			base += `,"action":"pause"`
		}
	}
	return "{" + base + "}"
}

func TestSkillProjectCannotComeFromQuery(t *testing.T) {
	srv := newTestServer(t)
	gateway := &captureSkillGateway{}
	srv.deps.Skills = gateway
	rec := call(t, srv, http.MethodPost, "/api/v1/skills/list?projectId=cube-a", `{}`)
	if rec.Code != 400 || len(gateway.calls) != 0 {
		t.Fatalf("query project accepted: %d %s", rec.Code, rec.Body.String())
	}
}

func TestSkillAuditDoesNotStoreUnvalidatedMetadata(t *testing.T) {
	raw, err := json.Marshal(skillAuditInput(map[string]any{
		"projectId": "cube-a", "operationId": map[string]any{"token": "private-secret"},
		"skillId": "fixture", "reason": "reviewer-private-notes", "path": "/private/oracle",
		"revision": strings.Repeat("a", 64), "expectedSequence": float64(2),
	}))
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{"private-secret", "reviewer-private-notes", "/private/oracle"} {
		if strings.Contains(string(raw), secret) {
			t.Fatalf("audit leaked unvalidated data: %s", raw)
		}
	}
	if !strings.Contains(string(raw), `"expectedSequence":2`) {
		t.Fatalf("lost concurrency metadata: %s", raw)
	}
}

func TestSkillPythonFieldBounds(t *testing.T) {
	for _, body := range []string{
		`{"projectId":"cube-a","query":"` + strings.Repeat("搜", 201) + `"}`,
		strings.ReplaceAll(skillTestBody("detail"), `"skillId":"mock-fixture"`, `"skillId":"Uppercase"`),
		strings.ReplaceAll(skillTestBody("evaluate"), `"operationId":"op-1"`, `"operationId":"op:invalid"`),
		strings.ReplaceAll(skillTestBody("evaluate"), `"operationId":"op-1"`, `"operationId":"`+strings.Repeat("x", 129)+`"`),
		strings.ReplaceAll(skillTestBody("status"), `"reason":"人工检查"`, `"reason":"`+strings.Repeat("a", 2001)+`"`),
	} {
		var value map[string]any
		if err := json.Unmarshal([]byte(body), &value); err != nil {
			t.Fatal(err)
		}
		spec := skillRoute{optional: []string{"query", "skillId", "revision", "caseId", "operationId", "expectedSequence", "action", "reason"}}
		if err := validateSkillBody(spec, value); err == nil {
			t.Fatal("Python-invalid field accepted")
		}
	}
	for _, value := range []struct{ field, text string }{{"reason", strings.Repeat("字", 2000)}, {"query", ""}} {
		if err := validateSkillField(value.field, value.text); err != nil {
			t.Fatalf("valid Unicode field rejected: %s %v", value.field, err)
		}
	}
}
