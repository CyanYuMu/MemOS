package domain

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestValidTenantID(t *testing.T) {
	t.Parallel()
	cases := []struct {
		id   string
		want bool
	}{
		{"cube-a", true},
		{"A.b_c-1", true},
		{strings.Repeat("a", 64), true},
		{strings.Repeat("a", 65), false},
		{"", false},
		{"bad id", false},
		{"a/b", false},
		{"项目", false},
	}
	for _, tc := range cases {
		if got := ValidTenantID(tc.id); got != tc.want {
			t.Errorf("ValidTenantID(%q) = %v，期望 %v", tc.id, got, tc.want)
		}
	}
}

func TestReservedTenantID(t *testing.T) {
	t.Parallel()
	if !IsReservedTenantID(PersonalTenantID("acct-1")) {
		t.Fatal("个人项目 ID 必须落在保留前缀内")
	}
	if IsReservedTenantID("team-u-x") || IsReservedTenantID("u") {
		t.Fatal("只有以保留前缀开头的 ID 才算保留")
	}
}

func TestTenantGuards(t *testing.T) {
	t.Parallel()
	now := time.Now()
	team := Tenant{ID: "team"}
	personal := Tenant{ID: "u-1", Kind: TenantKindPersonal}
	archived := Tenant{ID: "gone", Disabled: true, ArchivedAt: &now}
	disabled := Tenant{ID: "off", Disabled: true}

	cases := []struct {
		name   string
		err    error
		kind   error
		wantOK bool
	}{
		{"团队项目可停用", team.EnsureDisablable(), nil, true},
		{"个人项目不能停用", personal.EnsureDisablable(), ErrConflict, false},
		{"团队项目可归档", team.EnsureArchivable(), nil, true},
		{"个人项目不能归档", personal.EnsureArchivable(), ErrConflict, false},
		{"正常项目可修改", team.EnsureNotArchived(), nil, true},
		{"归档项目不能修改", archived.EnsureNotArchived(), ErrConflict, false},
		{"正常项目可切换", team.EnsureSelectable(), nil, true},
		{"归档项目不能切换", archived.EnsureSelectable(), ErrConflict, false},
		{"停用项目不能切换", disabled.EnsureSelectable(), ErrForbidden, false},
	}
	for _, tc := range cases {
		if tc.wantOK {
			if tc.err != nil {
				t.Errorf("%s: 期望通过，got %v", tc.name, tc.err)
			}
			continue
		}
		if !errors.Is(tc.err, tc.kind) {
			t.Errorf("%s: err = %v，期望 %v", tc.name, tc.err, tc.kind)
		}
	}
}
