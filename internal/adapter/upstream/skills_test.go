package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestSkillGatewayTrustedEnvelope(t *testing.T) {
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodPost || r.URL.Path != "/v1/skills/evaluate" {
			t.Errorf("unexpected route: %s %s", r.Method, r.URL.Path)
		}
		for name, want := range map[string]string{"Authorization": "Bearer service-only", "X-Skill-Actor": "alice", "X-Skill-Capability": "write", "X-Skill-Project": "cube-a"} {
			if got := r.Header.Get(name); got != want {
				t.Errorf("%s=%q", name, got)
			}
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body["projectId"] != "cube-a" || body["operationId"] != "op-1" {
			t.Errorf("body=%v", body)
		}
		_, _ = w.Write([]byte(`{"data":{"jobId":"job-1","status":"queued","candidate":{"rule_id":"r1"}}}`))
	}))
	defer srv.Close()
	gateway := NewSkillGateway(srv.URL, "service-only")
	data, err := gateway.Call(context.Background(), domain.SkillCall{Action: "evaluate", Actor: "alice", Capability: "write", ProjectID: "cube-a", Payload: map[string]any{"projectId": "cube-a", "operationId": "op-1"}})
	if err != nil || calls != 1 || !strings.Contains(string(data), `"rule_id":"r1"`) {
		t.Fatalf("data=%s calls=%d err=%v", data, calls, err)
	}
}

func TestSkillGatewayErrorsNeverRetryOrLeak(t *testing.T) {
	for _, status := range []int{400, 401, 403, 404, 409, 422, 503} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			calls := 0
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls++
				w.WriteHeader(status)
				_, _ = w.Write([]byte(`{"error":{"code":"sequence_conflict","message":"secret http://private-host/token","details":{"currentSequence":4,"secret":"private"}}}`))
			}))
			defer srv.Close()
			_, err := NewSkillGateway(srv.URL, "service-only").Call(context.Background(), domain.SkillCall{Action: "publish", Actor: "alice", Capability: "manage", ProjectID: "cube-a", Payload: map[string]any{"projectId": "cube-a"}})
			var se *domain.SkillServiceError
			if !errors.As(err, &se) || calls != 1 {
				t.Fatalf("calls=%d err=%v", calls, err)
			}
			if strings.Contains(err.Error(), "private") || strings.Contains(err.Error(), "secret") {
				t.Fatal("upstream message leaked")
			}
			if status == 409 && (se.Code != "sequence_conflict" || se.CurrentSequence == nil || *se.CurrentSequence != 4 || !errors.Is(err, domain.ErrConflict)) {
				t.Fatalf("conflict=%+v", se)
			}
		})
	}
}

func TestSkillGatewayRejectsRedirectAndMalformedResponses(t *testing.T) {
	leaked := false
	target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { leaked = true }))
	defer target.Close()
	for _, kind := range []string{"redirect", "malformed", "missing_data", "oversize", "unsafe_code"} {
		t.Run(kind, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch kind {
				case "redirect":
					http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
				case "malformed":
					_, _ = w.Write([]byte(`{`))
				case "missing_data":
					_, _ = w.Write([]byte(`{"ok":true}`))
				case "oversize":
					_, _ = w.Write([]byte(strings.Repeat("x", skillResponseMaxBytes+1)))
				case "unsafe_code":
					w.WriteHeader(http.StatusConflict)
					_, _ = w.Write([]byte(`{"error":{"code":"http://private/token","message":"secret"}}`))
				}
			}))
			defer srv.Close()
			_, err := NewSkillGateway(srv.URL, "service-only").Call(context.Background(), domain.SkillCall{Action: "list", Actor: "alice", Capability: "view", ProjectID: "cube-a", Payload: map[string]any{"projectId": "cube-a"}})
			if err == nil || strings.Contains(err.Error(), "private") {
				t.Fatalf("err=%v", err)
			}
		})
	}
	if leaked {
		t.Fatal("service credential followed redirect")
	}
}

func TestSkillGatewayRejectsUntrustedMetadata(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls++ }))
	defer srv.Close()
	for _, call := range []domain.SkillCall{
		{Action: "../../leak", Actor: "alice", Capability: "view", ProjectID: "cube-a"},
		{Action: "list", Actor: "alice\r\nX-Injected: yes", Capability: "view", ProjectID: "cube-a"},
		{Action: "list", Actor: "alice", Capability: "admin", ProjectID: "cube-a"},
		{Action: "list", Actor: "alice", Capability: "view", ProjectID: "cube-a", Payload: map[string]any{"projectId": "cube-b"}},
	} {
		if _, err := NewSkillGateway(srv.URL, "service-only").Call(context.Background(), call); err == nil {
			t.Fatalf("accepted %+v", call)
		}
	}
	if calls != 0 {
		t.Fatalf("untrusted requests reached upstream: %d", calls)
	}
}
