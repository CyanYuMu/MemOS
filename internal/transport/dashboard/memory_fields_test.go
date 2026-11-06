package dashboard

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestMemoryDetailPreservesReferenceFields(t *testing.T) {
	t.Parallel()
	updated, _ := domain.ParseUTC("2026-10-31T01:02:03Z")
	got := memoryJSON(domain.Memory{ID: "memory-1", Key: "午餐计划", Content: "明天在家做饭", NodeType: "UserMemory", Status: "activated", Tags: []string{"午餐"}, UpdatedAt: updated})
	for key, want := range map[string]string{"memoryKey": "午餐计划", "memoryType": "UserMemory", "status": "activated", "updateTime": "2026-10-31 09:02:03"} {
		if got[key] != want {
			t.Errorf("%s=%v, want %s", key, got[key], want)
		}
	}
	if tags, ok := got["tags"].([]string); !ok || len(tags) != 1 || tags[0] != "午餐" {
		t.Errorf("tags=%v", got["tags"])
	}
}

func TestMemoryActivityRequiresLoginAndProject(t *testing.T) {
	t.Parallel()
	srv := newTestServer(t)
	for _, path := range []string{"/api/dashboard/project/memory/activity", "/api/dashboard/project/memory/events"} {
		request := httptest.NewRequestWithContext(context.Background(), http.MethodPost, path, strings.NewReader(`{"projectId":"cube-a","callIds":["c1"]}`))
		rec := httptest.NewRecorder()
		srv.Handler().ServeHTTP(rec, request)
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("unauthenticated status=%d", rec.Code)
		}
		missing := call(t, srv, http.MethodPost, path, `{"callIds":["c1"]}`)
		if missing.Code != http.StatusBadRequest {
			t.Fatalf("missing project status=%d body=%s", missing.Code, missing.Body.String())
		}
	}
}

func TestMemoryProjectionPreservesRecallScoreWithoutInventingProfileScore(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name    string
		score   float64
		present bool
	}{
		{name: "recall", score: 0.673, present: true},
		{name: "profile_without_score", present: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			got, present := memoryJSON(domain.Memory{ID: "historical", Relativity: tc.score})["relativity"]
			if present != tc.present || (present && got != tc.score) {
				t.Fatalf("relativity=%v present=%v", got, present)
			}
		})
	}
}
