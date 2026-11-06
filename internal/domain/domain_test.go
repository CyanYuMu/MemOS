package domain

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestNewRawToken(t *testing.T) {
	t.Parallel()

	a, err := NewRawToken()
	if err != nil {
		t.Fatalf("NewRawToken: %v", err)
	}
	b, err := NewRawToken()
	if err != nil {
		t.Fatalf("NewRawToken: %v", err)
	}
	if a == b {
		t.Fatal("two tokens should differ")
	}
	if len(a) != len("sk-")+64 {
		t.Fatalf("token length = %d", len(a))
	}
	if !strings.HasPrefix(a, "sk-") {
		t.Fatalf("token = %q, want sk- prefix", a)
	}
	if HashToken(a) == HashToken(b) {
		t.Fatal("hashes should differ")
	}
	if got := HashToken(a); got != HashToken(a) {
		t.Fatal("hash must be deterministic")
	}
}

func TestTokenValidate(t *testing.T) {
	t.Parallel()

	hash := HashToken("x")
	tests := []struct {
		name string
		tok  Token
		ok   bool
	}{
		{"tenant", Token{ID: "t1", Kind: TokenKindTenant, TenantID: "cube", UserID: "u", Hash: hash}, true},
		{"admin", Token{ID: "a1", Kind: TokenKindAdmin, Hash: hash}, true},
		{"admin with tenant", Token{ID: "a1", Kind: TokenKindAdmin, TenantID: "cube", Hash: hash}, false},
		{"tenant without cube", Token{ID: "t1", Kind: TokenKindTenant, UserID: "u", Hash: hash}, false},
		{"unknown kind", Token{ID: "x", Kind: "robot", Hash: hash}, false},
		{"missing hash", Token{ID: "x", Kind: TokenKindAdmin}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Parallel()
			if err := tt.tok.Validate(); (err == nil) != tt.ok {
				t.Fatalf("Validate() err = %v, want ok=%v", err, tt.ok)
			}
		})
	}
}

func TestTokenExpired(t *testing.T) {
	t.Parallel()

	now := time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)
	if (Token{}).Expired(now) {
		t.Fatal("nil ExpiresAt 应表示永不过期")
	}
	before := now.Add(-time.Second)
	after := now.Add(time.Second)
	if (Token{ExpiresAt: &after}).Expired(now) {
		t.Fatal("未到过期时间不应过期")
	}
	if !(Token{ExpiresAt: &now}).Expired(now) {
		t.Fatal("过期时间等于 now 应视为已过期")
	}
	if !(Token{ExpiresAt: &before}).Expired(now) {
		t.Fatal("过期时间早于 now 应视为已过期")
	}
}

func TestMaskToken(t *testing.T) {
	t.Parallel()

	if got := MaskToken(""); got != "" {
		t.Errorf("empty = %q", got)
	}
	if got := MaskToken("short"); got != "*****" {
		t.Errorf("short = %q", got)
	}
	if got := MaskToken("0123456789abcdef"); got != "0123…cdef" {
		t.Errorf("long = %q", got)
	}
}

func TestErrorWrapping(t *testing.T) {
	t.Parallel()

	cause := errors.New("connection refused")
	err := Wrap(ErrUpstreamUnavailable, "search", "记忆服务暂时不可用", cause)

	if !errors.Is(err, ErrUpstreamUnavailable) {
		t.Fatal("errors.Is should match the kind")
	}
	if !errors.Is(err, cause) {
		t.Fatal("errors.Is should reach the inner cause")
	}
	if !errors.Is(KindOf(err), ErrUpstreamUnavailable) {
		t.Fatalf("KindOf = %v", KindOf(err))
	}
	if KindOf(errors.New("plain")) != nil {
		t.Fatal("plain error should have no kind")
	}
	var target *Error
	if !errors.As(err, &target) || target.Op != "search" {
		t.Fatalf("errors.As failed: %#v", target)
	}
}

func TestParseUTCNormalizesOffsets(t *testing.T) {
	t.Parallel()

	tests := []struct {
		in   string
		want string
	}{
		{"2026-10-21T12:34:56Z", "2026-10-21T12:34:56Z"},
		// 契约 v2：上游给的是真 UTC，带偏移的写法照常换算到 UTC。
		{"2026-10-21T20:34:56+08:00", "2026-10-21T12:34:56Z"},
		// 漏掉偏移的历史写法按 UTC 解释，不做任何猜测性换算。
		{"2026-10-21 12:34:56", "2026-10-21T12:34:56Z"},
	}
	for _, tt := range tests {
		got, err := ParseUTC(tt.in)
		if err != nil {
			t.Fatalf("ParseUTC(%q): %v", tt.in, err)
		}
		b, err := json.Marshal(got)
		if err != nil {
			t.Fatalf("Marshal: %v", err)
		}
		if string(b) != fmt.Sprintf("%q", tt.want) {
			t.Errorf("ParseUTC(%q) = %s, want %s", tt.in, b, tt.want)
		}
	}
	if _, err := ParseUTC("not-a-time"); err == nil {
		t.Fatal("invalid time should fail")
	}
	if got, err := ParseUTC(""); err != nil || !got.IsZero() {
		t.Fatalf("empty = %v, %v", got, err)
	}
}

func TestBeijingClock(t *testing.T) {
	t.Parallel()

	if got := BeijingClock("2026-10-21T12:00:00Z"); got != "2026-10-21 20:00:00" {
		t.Errorf("BeijingClock = %q", got)
	}
	if got := BeijingClock("not-a-time"); got != "not-a-time" {
		t.Errorf("unparseable time should pass through, got %q", got)
	}
	if got := BeijingClockOf(UTCTime{}); got != "" {
		t.Errorf("zero time = %q, want empty", got)
	}
}

func TestUTCTime(t *testing.T) {
	t.Parallel()

	got, err := ParseUTC("2026-10-21T12:00:00Z")
	if err != nil || !got.Equal(time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)) {
		t.Fatalf("ParseUTC = %v, %v", got, err)
	}
	b, _ := json.Marshal(UTCTime{})
	if string(b) != "null" {
		t.Errorf("zero time = %s, want null", b)
	}
}

func TestCallerValidate(t *testing.T) {
	t.Parallel()

	if err := (Caller{TenantID: "c", UserID: "u"}).Validate(); err != nil {
		t.Errorf("tenant caller: %v", err)
	}
	if err := (Caller{}).Validate(); !errors.Is(err, ErrUnauthorized) {
		t.Errorf("empty caller = %v", err)
	}
	if err := (Caller{Admin: true}).Validate(); err != nil {
		t.Errorf("admin caller: %v", err)
	}
}

func TestPrincipalRoles(t *testing.T) {
	t.Parallel()

	member := Principal{Roles: map[string]Role{"cube-a": RoleEditor}}
	if member.RoleIn("cube-a") != RoleEditor || member.RoleIn("cube-b") != "" {
		t.Fatalf("RoleIn 应只返回成员关系里的角色")
	}
	if err := member.Require("cube-a", RoleEditor); err != nil {
		t.Fatalf("editor 满足 editor: %v", err)
	}
	for _, tc := range []struct {
		tenant string
		least  Role
	}{{"cube-a", RoleOwner}, {"cube-b", RoleViewer}, {"", RoleViewer}} {
		if err := member.Require(tc.tenant, tc.least); !errors.Is(err, ErrForbidden) {
			t.Fatalf("Require(%q,%q) = %v, want ErrForbidden", tc.tenant, tc.least, err)
		}
	}

	admin := Principal{PlatformAdmin: true}
	if admin.RoleIn("anything") != RoleOwner || admin.Require("anything", RoleOwner) != nil {
		t.Fatalf("平台管理员对任意项目等价于 owner")
	}
	if member.RequirePlatformAdmin() == nil || admin.RequirePlatformAdmin() != nil {
		t.Fatalf("RequirePlatformAdmin 判定错误")
	}
	if Role("root").Valid() || Role("root").AtLeast(RoleViewer) {
		t.Fatalf("非法角色不应满足任何要求")
	}
}
