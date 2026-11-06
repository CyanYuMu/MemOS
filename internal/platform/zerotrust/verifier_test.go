package zerotrust

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

const testSecret = "0123456789abcdef-zerotrust"

type testClaims struct {
	Iat      int64  `json:"iat,omitempty"`
	Exp      int64  `json:"exp"`
	Username string `json:"username,omitempty"`
	Name     string `json:"name,omitempty"`
	Email    string `json:"email,omitempty"`
}

func sign(t *testing.T, secret, alg string, claims testClaims) string {
	t.Helper()
	if alg == "" {
		alg = "HS256"
	}
	head, err := json.Marshal(map[string]string{"alg": alg, "typ": "JWT"})
	if err != nil {
		t.Fatalf("marshal header: %v", err)
	}
	body, err := json.Marshal(claims)
	if err != nil {
		t.Fatalf("marshal claims: %v", err)
	}
	enc := base64.RawURLEncoding
	signing := enc.EncodeToString(head) + "." + enc.EncodeToString(body)
	if alg != "HS256" {
		return signing + "." + enc.EncodeToString([]byte("none"))
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(signing))
	return signing + "." + enc.EncodeToString(mac.Sum(nil))
}

func newTestVerifier(t *testing.T, now time.Time) *Verifier {
	t.Helper()
	v, err := NewWithClock([]string{testSecret}, func() time.Time { return now }, time.Minute)
	if err != nil {
		t.Fatalf("NewWithClock: %v", err)
	}
	return v
}

func TestVerify(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_700_000_000, 0)
	v := newTestVerifier(t, now)

	tests := []struct {
		name       string
		token      string
		wantUser   string
		wantOK     bool
		wantErr    bool
		wantErrSub string
	}{
		{
			name: "valid",
			token: sign(t, testSecret, "", testClaims{
				Iat: now.Unix(), Exp: now.Add(time.Minute).Unix(), Username: "Alice.Zhang", Name: "张三", Email: "alice@example.com",
			}),
			wantUser: "Alice.Zhang",
			wantOK:   true,
		},
		{
			name: "expired",
			token: sign(t, testSecret, "", testClaims{
				Iat: now.Add(-2 * time.Minute).Unix(), Exp: now.Add(-2 * time.Minute).Unix(), Username: "alice",
			}),
			wantErr:    true,
			wantErrSub: "已过期",
		},
		{
			name: "wrong secret",
			token: sign(t, "another-secret-0000000000", "", testClaims{
				Iat: now.Unix(), Exp: now.Add(time.Minute).Unix(), Username: "alice",
			}),
			wantErr:    true,
			wantErrSub: "签名不匹配",
		},
		{
			name: "alg none",
			token: sign(t, testSecret, "none", testClaims{
				Iat: now.Unix(), Exp: now.Add(time.Minute).Unix(), Username: "alice",
			}),
			wantErr:    true,
			wantErrSub: "alg",
		},
		{
			name:       "malformed",
			token:      "not-a-jwt",
			wantErr:    true,
			wantErrSub: "段数",
		},
		{
			name: "missing exp",
			token: sign(t, testSecret, "", testClaims{
				Iat: now.Unix(), Username: "alice",
			}),
			wantErr:    true,
			wantErrSub: "exp",
		},
		{
			name: "issued in the future",
			token: sign(t, testSecret, "", testClaims{
				Iat: now.Add(5 * time.Minute).Unix(), Exp: now.Add(10 * time.Minute).Unix(), Username: "alice",
			}),
			wantErr:    true,
			wantErrSub: "iat",
		},
		{
			name: "no username",
			token: sign(t, testSecret, "", testClaims{
				Iat: now.Unix(), Exp: now.Add(time.Minute).Unix(),
			}),
			wantOK: false,
		},
		{
			name:       "oversized",
			token:      strings.Repeat("a", maxTokenBytes+1) + ".b.c",
			wantErr:    true,
			wantErrSub: "过长",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			identity, ok, err := v.Verify(tc.token)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("want error, got identity=%+v ok=%v", identity, ok)
				}
				if !errors.Is(err, ErrInvalidToken) {
					t.Fatalf("error = %v，应以 ErrInvalidToken 包装", err)
				}
				if !strings.Contains(err.Error(), tc.wantErrSub) {
					t.Fatalf("error = %v, want substring %q", err, tc.wantErrSub)
				}
				if ok {
					t.Fatalf("ok 应为 false")
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tc.wantOK)
			}
			if tc.wantOK {
				if identity.Username != tc.wantUser || identity.Source != "zerotrust" || identity.Name != "张三" {
					t.Fatalf("identity = %+v", identity)
				}
			}
		})
	}
}

func TestNewRejectsShortSecret(t *testing.T) {
	t.Parallel()
	if _, err := New([]string{"too-short"}); err == nil {
		t.Fatal("短 secret 应被拒绝")
	}
	if _, err := New(nil); err == nil {
		t.Fatal("空 secret 列表应被拒绝")
	}
	if _, err := New([]string{testSecret, "short"}); err == nil {
		t.Fatal("列表里任一短 secret 都应被拒绝")
	}
}

func TestVerifyAcceptsRotatedSecrets(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_700_000_000, 0)
	const nextSecret = "fedcba9876543210-rotated"
	v, err := NewWithClock([]string{testSecret, nextSecret}, func() time.Time { return now }, time.Minute)
	if err != nil {
		t.Fatalf("NewWithClock: %v", err)
	}

	for _, secret := range []string{testSecret, nextSecret} {
		token := sign(t, secret, "", testClaims{
			Iat: now.Unix(), Exp: now.Add(time.Minute).Unix(), Username: "alice",
		})
		identity, ok, err := v.Verify(token)
		if err != nil || !ok || identity.Username != "alice" {
			t.Fatalf("secret %q 应通过: identity=%+v ok=%v err=%v", secret, identity, ok, err)
		}
	}

	unknown := sign(t, "another-secret-0000000000", "", testClaims{
		Iat: now.Unix(), Exp: now.Add(time.Minute).Unix(), Username: "alice",
	})
	if _, _, err := v.Verify(unknown); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("未知密钥应被拒绝: %v", err)
	}
}
