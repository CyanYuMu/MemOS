package httpx

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/zerotrust"
)

const ztTestSecret = "0123456789abcdef-httpx"

func ztToken(t *testing.T, username string, exp time.Time) string {
	t.Helper()
	enc := base64.RawURLEncoding
	head, err := json.Marshal(map[string]string{"alg": "HS256", "typ": "JWT"})
	if err != nil {
		t.Fatalf("marshal header: %v", err)
	}
	body, err := json.Marshal(map[string]any{
		"iat": time.Now().Unix(), "exp": exp.Unix(), "username": username,
	})
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	signing := enc.EncodeToString(head) + "." + enc.EncodeToString(body)
	mac := hmac.New(sha256.New, []byte(ztTestSecret))
	mac.Write([]byte(signing))
	return signing + "." + enc.EncodeToString(mac.Sum(nil))
}

func mustVerifier(t *testing.T) *zerotrust.Verifier {
	t.Helper()
	v, err := zerotrust.New([]string{ztTestSecret})
	if err != nil {
		t.Fatalf("zerotrust.New: %v", err)
	}
	return v
}

func TestZeroTrustMiddleware(t *testing.T) {
	t.Parallel()
	v := mustVerifier(t)

	tests := []struct {
		name     string
		header   string
		want     int
		wantUser string
	}{
		{name: "missing header passes through", want: http.StatusOK},
		{name: "valid identity", header: ztToken(t, "alice", time.Now().Add(time.Minute)), want: http.StatusOK, wantUser: "alice"},
		{name: "invalid token", header: "not-a-jwt", want: http.StatusUnauthorized},
		{name: "expired token", header: ztToken(t, "alice", time.Now().Add(-time.Hour)), want: http.StatusUnauthorized},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			var gotUser string
			h := ZeroTrust(v, "X-Zt-Authorization")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if id, ok := ExternalIdentityFromContext(r.Context()); ok {
					gotUser = id.Username
				}
				if c := CredentialsOf(r); tc.wantUser != "" && (c.External == nil || c.External.Username != tc.wantUser) {
					t.Fatalf("Credentials.External = %+v", c.External)
				}
				w.WriteHeader(http.StatusOK)
			}))

			req := httptest.NewRequestWithContext(context.Background(), http.MethodGet, "/", nil)
			if tc.header != "" {
				req.Header.Set("X-Zt-Authorization", tc.header)
			}
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)

			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d", rec.Code, tc.want)
			}
			if gotUser != tc.wantUser {
				t.Fatalf("username = %q, want %q", gotUser, tc.wantUser)
			}
		})
	}
}
