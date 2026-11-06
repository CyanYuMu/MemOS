package auth

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

type keyLookup map[string]domain.Token

func (k keyLookup) GetToken(_ context.Context, id string) (domain.Token, error) {
	tok, ok := k[id]
	if !ok {
		return domain.Token{}, domain.NotFound("get_token", "Key 不存在")
	}
	return tok, nil
}

type stubAuth struct {
	p   domain.Principal
	err error
}

func (s stubAuth) Authenticate(context.Context, domain.Credentials) (domain.Principal, error) {
	return s.p, s.err
}

func principal(roles map[string]domain.Role, current string) domain.Principal {
	return domain.Principal{Kind: domain.PrincipalAccount, AccountID: "acct", Roles: roles, CurrentTenant: current}
}

func TestRuleResolve(t *testing.T) {
	t.Parallel()
	unauth := domain.Wrap(domain.ErrUnauthorized, "authenticate", "未登录", nil)
	boom := errors.New("db down")
	cases := []struct {
		name    string
		rule    Rule
		authErr error
		wantErr error
	}{
		{"公开路由遇到未登录放行并返回零值主体", Public, unauth, nil},
		{"公开路由遇到其他错误照常报错", Public, boom, boom},
		{"受保护路由未登录就是 401", Authed, unauth, domain.ErrUnauthorized},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			req := httptest.NewRequestWithContext(t.Context(), http.MethodGet, "/x", nil)
			p, err := tc.rule.Resolve(t.Context(), stubAuth{p: domain.Principal{AccountID: "x"}, err: tc.authErr}, req)
			if tc.wantErr == nil {
				if err != nil || p.AccountID != "" {
					t.Fatalf("p=%+v err=%v，期望零值主体且无错误", p, err)
				}
				return
			}
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v，期望 %v", err, tc.wantErr)
			}
		})
	}
}

// 被拒时仍返回被判权的项目：调用方先把它写进审计的 tenant_id 再返回 403，
// 事后才能看出“谁试图动哪个项目”。缺项目或项目无从确定时为空串。
func TestRuleAuthorize(t *testing.T) {
	t.Parallel()
	viewer := principal(map[string]domain.Role{"cube-a": domain.RoleViewer}, "cube-a")
	owner := principal(map[string]domain.Role{"cube-a": domain.RoleOwner}, "cube-a")
	stranger := principal(map[string]domain.Role{"cube-b": domain.RoleOwner}, "cube-b")
	noProject := principal(nil, "")
	admin := domain.Principal{Kind: domain.PrincipalAccount, PlatformAdmin: true}
	keys := keyLookup{
		"k-a":      {ID: "k-a", TenantID: "cube-a"},
		"k-global": {ID: "k-global"},
	}

	cases := []struct {
		name       string
		rule       Rule
		p          domain.Principal
		target     string
		pathID     string
		body       map[string]any
		wantTenant string
		wantErr    error
	}{
		{"登录即可", Authed, noProject, "/x", "", nil, "", nil},
		{"项目创建只看是否已认证", AuthedWith(domain.CapProjectCreate), noProject, "/x", "", nil, "", nil},
		{"平台管理员", Platform, admin, "/x", "", nil, "", nil},
		{"普通成员不是平台管理员", Platform, owner, "/x", "", nil, "", domain.ErrForbidden},

		{
			"body 里的 projectId 优先", InBody(domain.CapProjectView), viewer, "/x", "",
			map[string]any{"projectId": " cube-a "},
			"cube-a", nil,
		},
		{"其次取查询串 projectId", InBody(domain.CapProjectView), viewer, "/x?projectId=cube-a", "", nil, "cube-a", nil},
		{"都没有时落到会话当前项目", InBody(domain.CapProjectView), viewer, "/x", "", nil, "cube-a", nil},
		{"没有任何项目是参数错误", InBody(domain.CapProjectView), noProject, "/x", "", nil, "", domain.ErrInvalidInput},
		{"严格模式不回落到当前项目", InBodyStrict(domain.CapProjectWrite), owner, "/x", "", nil, "", domain.ErrInvalidInput},
		{"严格模式显式给出就放行", InBodyStrict(domain.CapProjectWrite), owner, "/x?projectId=cube-a", "", nil, "cube-a", nil},
		{"viewer 没有写能力", InBody(domain.CapProjectWrite), viewer, "/x", "", nil, "cube-a", domain.ErrForbidden},
		{"非成员被拒", InBody(domain.CapProjectView), stranger, "/x?projectId=cube-a", "", nil, "cube-a", domain.ErrForbidden},
		{"平台管理员对任意项目有效", InBody(domain.CapProjectManage), admin, "/x?projectId=cube-z", "", nil, "cube-z", nil},

		{"查询串 tenant", InQuery(domain.CapProjectView), viewer, "/x?tenant=cube-a", "", nil, "cube-a", nil},
		{"缺 tenant", InQuery(domain.CapProjectView), viewer, "/x", "", nil, "", domain.ErrInvalidInput},
		{"路径项目", InPath(domain.CapProjectManage), owner, "/x", "cube-a", nil, "cube-a", nil},
		{"路径项目缺失", InPath(domain.CapProjectManage), owner, "/x", "", nil, "", domain.ErrInvalidInput},
		{"路径项目无权限", InPath(domain.CapProjectManage), viewer, "/x", "cube-a", nil, "cube-a", domain.ErrForbidden},

		{"Key 所属项目判权", OnKey(domain.CapProjectWrite), owner, "/x", "k-a", nil, "cube-a", nil},
		{"Key 所属项目非成员", OnKey(domain.CapProjectWrite), stranger, "/x", "k-a", nil, "cube-a", domain.ErrForbidden},
		{"没有项目归属的 Key 只有平台管理员能动", OnKey(domain.CapProjectWrite), owner, "/x", "k-global", nil, "", domain.ErrForbidden},
		{"没有项目归属的 Key 平台管理员可动", OnKey(domain.CapProjectWrite), admin, "/x", "k-global", nil, "", nil},
		{"Key 不存在对普通成员表现为无权限", OnKey(domain.CapProjectWrite), owner, "/x", "nope", nil, "", domain.ErrForbidden},
		{"Key 不存在对平台管理员如实返回 404", OnKey(domain.CapProjectWrite), admin, "/x", "nope", nil, "", domain.ErrNotFound},

		{"未知规则一律拒绝", Rule{Scope: Scope(99)}, admin, "/x", "", nil, "", domain.ErrForbidden},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			req := httptest.NewRequestWithContext(t.Context(), http.MethodPost, tc.target, nil)
			if tc.pathID != "" {
				req.SetPathValue("id", tc.pathID)
			}
			got, err := tc.rule.Authorize(t.Context(), AuthzInput{Principal: tc.p, Request: req, Body: tc.body, Keys: keys})
			if tc.wantErr == nil && err != nil {
				t.Fatalf("Authorize: %v", err)
			}
			if tc.wantErr != nil && !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v，期望 %v", err, tc.wantErr)
			}
			if got != tc.wantTenant {
				t.Fatalf("判权项目 = %q，期望 %q", got, tc.wantTenant)
			}
		})
	}
}
