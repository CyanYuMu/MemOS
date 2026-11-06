package auth

import (
	"context"
	"net/http"
	"path/filepath"
	"testing"

	"github.com/getkin/kin-openapi/openapi3"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/onboarding"
)

type guideStore struct{}

func (guideStore) GetOnboarding(context.Context, string, string) (domain.OnboardingProgress, error) {
	return domain.OnboardingProgress{Status: "pending", Version: 1}, nil
}

func TestOnboardingInputAndContract(t *testing.T) {
	t.Parallel()
	doc, err := openapi3.NewLoader().LoadFromFile(filepath.Join("..", "..", "..", "api", "openapi.onboarding.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if err := doc.Validate(t.Context()); err != nil {
		t.Fatal(err)
	}
	if doc.Paths.Len() != 1 || doc.Paths.Find("/api/auth/onboarding").Get == nil || doc.Paths.Find("/api/auth/onboarding").Put == nil {
		t.Fatal("missing guide contract")
	}
	h := newHarness(t, func(d *Deps) { d.Onboarding = onboarding.New(guideStore{}) })
	h.sessions.principal.Roles = map[string]domain.Role{"p": domain.RoleOwner}
	for _, tc := range []struct {
		body string
		want int
	}{
		{`{"projectId":"p","action":"start"}`, 400},
		{`{"projectId":"p","action":"start","revision":-1}`, 400},
		{`{"projectId":"p","action":"start","revision":2}`, 409},
		{`{"projectId":"other","action":"start","revision":0}`, 403},
		{`{"projectId":"p","action":"start","revision":0}`, 200},
	} {
		r := h.do(t, http.MethodPut, "/api/auth/onboarding", tc.body, header("Content-Type", "application/json"))
		if r.Code != tc.want {
			t.Fatalf("%s: %d %s", tc.body, r.Code, r.Body.String())
		}
	}
}

func (guideStore) SaveOnboarding(context.Context, string, string, domain.OnboardingProgress) error {
	return nil
}

func TestOnboardingExternalIdentityAndAuthorization(t *testing.T) {
	t.Parallel()
	h := newHarness(t, func(d *Deps) { d.Onboarding = onboarding.New(guideStore{}) })
	h.sessions.principal.SessionID = ""
	h.sessions.principal.Roles = map[string]domain.Role{"p": domain.RoleOwner}
	for _, tc := range []struct {
		path string
		want int
	}{{"/api/auth/onboarding?projectId=p", 200}, {"/api/auth/onboarding?projectId=other", 403}, {"/api/auth/onboarding", 400}} {
		r := h.do(t, http.MethodGet, tc.path, "")
		if r.Code != tc.want {
			t.Fatalf("%s: %d %s", tc.path, r.Code, r.Body.String())
		}
	}
	h.sessions.authErr = domain.ErrUnauthorized
	if r := h.do(t, http.MethodGet, "/api/auth/onboarding?projectId=p", ""); r.Code != 401 {
		t.Fatalf("anonymous: %d", r.Code)
	}
}

func (guideStore) GetChatConversation(context.Context, string, string, string) (domain.ChatConversation, error) {
	return domain.ChatConversation{}, nil
}
