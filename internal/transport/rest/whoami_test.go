package rest

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type fakeTenants struct{ name string }

func (f fakeTenants) Get(_ context.Context, id string) (domain.Tenant, error) {
	if id != "cube-a" {
		return domain.Tenant{}, domain.Wrap(domain.ErrNotFound, "get_tenant", id, nil)
	}
	return domain.Tenant{ID: id, Name: f.name}, nil
}

func TestWhoami(t *testing.T) {
	srv := newTestServer(t, &fakeUpstream{}, nil)
	srv.deps.Tenants = fakeTenants{name: "demo-project"}

	req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/memory/whoami", nil)
	req.Header.Set("Authorization", "Bearer "+testToken)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var got map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got["user_id"] != "alice" || got["project_id"] != "cube-a" || got["project_name"] != "demo-project" {
		t.Fatalf("body = %v", got)
	}

	cases := []struct {
		name   string
		method string
		token  string
		status int
	}{
		{"无 token", http.MethodGet, "", http.StatusUnauthorized},
		{"写方法", http.MethodPost, testToken, http.StatusMethodNotAllowed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequestWithContext(context.Background(), tc.method, "/memory/whoami", nil)
			if tc.token != "" {
				req.Header.Set("Authorization", "Bearer "+tc.token)
			}
			rec := httptest.NewRecorder()
			srv.Handler().ServeHTTP(rec, req)
			if rec.Code != tc.status {
				t.Fatalf("status = %d, want %d (%s)", rec.Code, tc.status, rec.Body.String())
			}
		})
	}
}
