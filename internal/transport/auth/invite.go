package auth

import (
	"context"
	"errors"
	"html/template"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// InviteError 是落地页的展示状态，文案固定，不回显 token。
type inviteView struct {
	Project   string
	Role      string
	ExpiresAt string
	Already   bool
	Error     string
}

var invitePage = template.Must(template.New("invite").Parse(`<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>加入项目 · Memory Console</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f4f5f7;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1f2329}
main{width:400px;max-width:92vw;background:#fff;border-radius:12px;padding:32px 28px;box-shadow:0 8px 30px rgba(0,0,0,.08)}
h1{margin:0 0 6px;font-size:20px}
p{margin:8px 0;color:#646a73}
dl{margin:18px 0;display:grid;grid-template-columns:auto 1fr;gap:8px 16px}
dt{color:#646a73}
dd{margin:0;font-weight:600}
button{width:100%;padding:10px 12px;border:0;border-radius:8px;background:#3370ff;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:hover{background:#245bdb}
a.enter{display:block;text-align:center;padding:10px;border-radius:8px;background:#3370ff;color:#fff;font-weight:600;text-decoration:none}
.err{margin:0 0 8px;padding:9px 12px;border-radius:8px;background:#fdecea;color:#b42318}
</style>
</head>
<body>
<main>
<h1>{{if .Error}}无法加入项目{{else if .Already}}你已加入该项目{{else}}加入项目{{end}}</h1>
{{if .Error}}<p class="err" role="alert">{{.Error}}</p>{{else}}
<p>你被邀请加入下面的项目空间：</p>
<dl>
<dt>项目</dt><dd>{{.Project}}</dd>
<dt>角色</dt><dd>{{.Role}}</dd>
<dt>有效期至</dt><dd>{{.ExpiresAt}}</dd>
</dl>
{{if .Already}}<a class="enter" href="/">进入项目</a>{{else}}
<form method="post" action="" id="join">
<button type="submit">加入项目</button>
</form>
<p>点击按钮后才会加入；链接被打开不会自动加入。</p>
{{end}}{{end}}
</main>
</body>
</html>
`))

const (
	msgInviteInvalid = "邀请无效或已过期"
	msgInviteLogin   = "请先登录后再加入项目"
)

// invitePageHeaders 覆盖登录页的 CSP。确认页没有脚本：加入必须由用户点击，
// 不允许打开链接就自动提交（否则聊天工具预抓取或误点都会直接产生成员关系）。
func invitePageHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Content-Type", "text/html; charset=utf-8")
	h.Set("Cache-Control", "no-store")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("Content-Security-Policy",
		"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
}

func writeInvitePage(w http.ResponseWriter, status int, view inviteView) {
	invitePageHeaders(w)
	w.WriteHeader(status)
	_ = invitePage.Execute(w, view)
}

// inviteGET 只渲染确认页，绝不消费 token：聊天工具会预抓取链接，
// 在用户点击前消费会让链接失效。真正的兑换在 POST。
func (s *Server) inviteGET(w http.ResponseWriter, r *http.Request) {
	token := r.PathValue("token")
	p, err := s.deps.Sessions.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		s.inviteNeedsLogin(w, r, token, err)
		return
	}
	preview, err := s.deps.Invites.PreviewInvite(r.Context(), p, token)
	if err != nil {
		writeInvitePage(w, http.StatusNotFound, inviteView{Error: msgInviteInvalid})
		return
	}
	writeInvitePage(w, http.StatusOK, inviteView{
		Project:   projectName(preview.Tenant),
		Role:      roleLabel(preview.Invite.Role),
		ExpiresAt: beijingTime(preview.Invite.ExpiresAt),
		Already:   preview.AlreadyMember,
	})
}

// invitePOST 兑换邀请；GET 与 POST 用同一个 URL，表单 action="" 即可。
func (s *Server) invitePOST(w http.ResponseWriter, r *http.Request) {
	token := r.PathValue("token")
	// 兑换是唯一接受空 body 的写接口，零信任模式下依赖网关注入的身份头而不是
	// 会话 cookie，SameSite 保护不到，必须额外拒绝跨站表单提交。
	if !httpx.SameOriginPost(r) {
		writeInvitePage(w, http.StatusForbidden, inviteView{Error: msgInviteInvalid})
		return
	}
	p, err := s.deps.Sessions.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		s.inviteNeedsLogin(w, r, token, err)
		return
	}
	inv, err := s.deps.Invites.RedeemInvite(r.Context(), p, token)
	s.auditInvite(r.Context(), inviteAudit{Username: p.Username, IP: remoteIP(r), Invite: inv, Err: err})
	if err != nil {
		status := http.StatusNotFound
		if errors.Is(err, domain.ErrForbidden) {
			status = http.StatusForbidden
		}
		writeInvitePage(w, status, inviteView{Error: msgInviteInvalid})
		return
	}
	setPageHeaders(w)
	http.Redirect(w, r, "/", http.StatusSeeOther)
}

// inviteNeedsLogin：账密模式跳登录页并带上回跳；零信任必选模式说明需经网关访问。
func (s *Server) inviteNeedsLogin(w http.ResponseWriter, r *http.Request, token string, err error) {
	if !errors.Is(err, domain.ErrUnauthorized) {
		writeInvitePage(w, http.StatusForbidden, inviteView{Error: msgInviteInvalid})
		return
	}
	if s.deps.DisablePassword {
		writeInvitePage(w, http.StatusForbidden, inviteView{Error: "请通过统一认证网关访问该邀请链接"})
		return
	}
	setPageHeaders(w)
	target := "/__login?next=" + url.QueryEscape("/__invite/"+token)
	http.Redirect(w, r, target, http.StatusSeeOther)
}

// inviteAudit 是邀请兑换的审计入参。
type inviteAudit struct {
	Username string
	IP       string
	Invite   domain.ProjectInvite
	Err      error
}

// auditInvite 记录兑换结果；Input 只含邀请 ID/项目/角色，绝不含 token 或 hash。
func (s *Server) auditInvite(ctx context.Context, in inviteAudit) {
	input := map[string]any{"username": in.Username}
	if in.Invite.ID != "" {
		input["invite_id"] = in.Invite.ID
		input["tenant_id"] = in.Invite.TenantID
		input["role"] = string(in.Invite.Role)
	}
	s.auditWithInput(ctx, authAudit{Tool: "auth.invite.redeem", Username: in.Username, IP: in.IP, Input: input, Err: in.Err})
}

func projectName(t domain.Tenant) string {
	if strings.TrimSpace(t.Name) != "" {
		return t.Name
	}
	return t.ID
}

func roleLabel(r domain.Role) string {
	switch r {
	case domain.RoleOwner:
		return "管理员（owner）"
	case domain.RoleEditor:
		return "编辑者（editor）"
	default:
		return "查看者（viewer）"
	}
}

// beijingTime 用北京时间展示有效期；控制台其余展示口径一致。
func beijingTime(t time.Time) string {
	return t.In(time.FixedZone("CST", 8*3600)).Format("2006-01-02 15:04")
}
