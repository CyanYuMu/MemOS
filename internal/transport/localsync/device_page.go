package localsync

import (
	"errors"
	"html/template"
	"net/http"
	"net/url"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	svc "github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/localsync"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

const maxDeviceFormBytes = 8 << 10

var devicePageTemplate = template.Must(template.New("device").Parse(`<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>授权本地插件 · Memory Console</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f4f5f7;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1f2329}
main{width:420px;max-width:92vw;background:#fff;border-radius:12px;padding:32px 28px;box-shadow:0 8px 30px rgba(0,0,0,.08)}
h1{margin:0 0 6px;font-size:20px}
p{margin:8px 0;color:#646a73}
dl{margin:16px 0;display:grid;grid-template-columns:auto 1fr;gap:8px 16px}
dt{color:#646a73}
dd{margin:0;font-weight:600}
label{display:block;margin:14px 0 6px;font-weight:600}
input,select{width:100%;padding:10px 12px;border:1px solid #d0d3d9;border-radius:8px;font:inherit}
button{margin-top:12px;width:100%;padding:10px 12px;border:0;border-radius:8px;background:#3370ff;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.deny{background:#fff;color:#b42318;border:1px solid #f2b8b5}
.actions{display:flex;gap:10px}
.actions form{flex:1}
.err{margin:0 0 8px;padding:9px 12px;border-radius:8px;background:#fdecea;color:#b42318}
.note{margin-top:16px;font-size:12px;color:#8f959e}
</style>
</head>
<body>
<main>
{{if .Error}}<h1>无法授权</h1><p class="err" role="alert">{{.Error}}</p>
{{else if .Done}}<h1>{{.Title}}</h1><p>{{.Message}}</p>
{{else}}<h1>授权本地插件</h1>
<p>本地插件请求向控制台上传记忆副本。授权后，插件只能向所选项目上传，不会写回你的本地数据。</p>
<dl>
<dt>设备名</dt><dd>{{.DeviceLabel}}</dd>
<dt>类型</dt><dd>{{.AgentKind}}</dd>
<dt>验证码</dt><dd>{{.UserCode}}</dd>
</dl>
{{if .Projects}}<form method="post" action="/__device">
<input type="hidden" name="userCode" value="{{.UserCode}}">
<input type="hidden" name="allow" value="true">
<label for="tenantId">项目</label>
<select id="tenantId" name="tenantId">
{{range .Projects}}<option value="{{.ID}}">{{.Name}}（{{.Role}}）</option>{{end}}
</select>
<label for="label">设备名</label>
<input id="label" name="label" type="text" maxlength="128" value="{{.DeviceLabel}}">
<div class="actions">
<button type="submit">授权上传</button>
</div>
</form>
<form method="post" action="/__device">
<input type="hidden" name="userCode" value="{{.UserCode}}">
<input type="hidden" name="allow" value="false">
<button class="deny" type="submit">拒绝</button>
</form>
{{else}}<p class="err">你还没有可授权的项目（需要 editor 及以上角色）。</p>{{end}}
<p class="note">授权范围只包括上传；控制台不会把云端记忆写回本地。删除云端副本也不会删除本地数据。</p>
{{end}}
</main>
</body>
</html>
`))

type deviceProject struct {
	ID   string
	Name string
	Role string
}

type deviceView struct {
	Error       string
	Done        bool
	Title       string
	Message     string
	DeviceLabel string
	AgentKind   string
	UserCode    string
	Projects    []deviceProject
}

func (s *Server) devicePage(w http.ResponseWriter, r *http.Request) {
	if !s.deps.Enabled {
		writeDevicePage(w, http.StatusServiceUnavailable, deviceView{Error: "本地同步能力未启用"})
		return
	}
	p, err := s.deps.Accounts.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		s.deviceNeedsLogin(w, r)
		return
	}
	code := strings.TrimSpace(r.URL.Query().Get("code"))
	if code == "" {
		writeDevicePage(w, http.StatusBadRequest, deviceView{Error: "缺少授权码，请从插件重新打开授权链接"})
		return
	}
	preview, err := s.deps.Service.PreviewDeviceAuthorize(r.Context(), code)
	if err != nil {
		writeDevicePage(w, http.StatusNotFound, deviceView{Error: "授权码不存在或已过期"})
		return
	}
	projects, err := s.authorizableProjects(r, p)
	if err != nil {
		writeDevicePage(w, http.StatusInternalServerError, deviceView{Error: "暂时无法读取项目列表"})
		return
	}
	agentKind := preview.AgentKind
	if agentKind == "" {
		agentKind = "本地插件"
	}
	writeDevicePage(w, http.StatusOK, deviceView{
		DeviceLabel: preview.DeviceLabel,
		AgentKind:   agentKind,
		UserCode:    preview.UserCode,
		Projects:    projects,
	})
}

// authorizableProjects 列出账号可以授权的项目。判权在 service 里按成员表做：
// 平台管理员的 Principal.Roles 恒为空，不能在这里直接用映射过滤。
func (s *Server) authorizableProjects(r *http.Request, p domain.Principal) ([]deviceProject, error) {
	opts, err := s.deps.Service.AuthorizableProjects(r.Context(), p.AccountID)
	if err != nil {
		return nil, err
	}
	projects := make([]deviceProject, 0, len(opts))
	for _, o := range opts {
		projects = append(projects, deviceProject{ID: o.ID, Name: o.Name, Role: string(o.Role)})
	}
	return projects, nil
}

func (s *Server) deviceApprove(w http.ResponseWriter, r *http.Request) {
	if !s.deps.Enabled {
		writeDevicePage(w, http.StatusServiceUnavailable, deviceView{Error: "本地同步能力未启用"})
		return
	}
	if !httpx.SameOriginPost(r) {
		writeDevicePage(w, http.StatusForbidden, deviceView{Error: "拒绝跨站提交"})
		return
	}
	p, err := s.deps.Accounts.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err != nil {
		s.deviceNeedsLogin(w, r)
		return
	}
	in, asJSON, err := readApproveInput(w, r)
	if err != nil {
		if asJSON {
			s.fail(w, r, err)
			return
		}
		writeDevicePage(w, http.StatusBadRequest, deviceView{Error: "请求格式不正确"})
		return
	}
	err = s.deps.Service.ApproveDeviceCode(r.Context(), p, in)
	if asJSON {
		if err != nil {
			s.fail(w, r, err)
			return
		}
		httpx.OK(w, map[string]any{"allow": in.Allow, "tenantId": in.TenantID})
		return
	}
	if err != nil {
		writeDevicePage(w, devicePageStatus(err), deviceView{Error: devicePageMessage(err)})
		return
	}
	title, message := "已拒绝授权", "插件不会上传任何数据。可以关闭此页面。"
	if in.Allow {
		title, message = "已授权", "请回到插件继续；上传默认关闭，需要在插件里逐层开启。"
	}
	writeDevicePage(w, http.StatusOK, deviceView{Done: true, Title: title, Message: message})
}

func readApproveInput(w http.ResponseWriter, r *http.Request) (svc.ApproveInput, bool, error) {
	r.Body = http.MaxBytesReader(w, r.Body, maxDeviceFormBytes)
	if strings.HasPrefix(strings.ToLower(r.Header.Get("Content-Type")), "application/json") {
		var body struct {
			UserCode string `json:"userCode"`
			TenantID string `json:"tenantId"`
			Label    string `json:"label"`
			Allow    bool   `json:"allow"`
		}
		if err := httpx.DecodeJSON(r, &body); err != nil {
			return svc.ApproveInput{}, true, err
		}
		return svc.ApproveInput{
			UserCode: body.UserCode, TenantID: body.TenantID, Label: body.Label, Allow: body.Allow,
		}, true, nil
	}
	if err := r.ParseForm(); err != nil {
		return svc.ApproveInput{}, false, httpx.ErrBadJSON
	}
	return svc.ApproveInput{
		UserCode: r.PostForm.Get("userCode"),
		TenantID: r.PostForm.Get("tenantId"),
		Label:    r.PostForm.Get("label"),
		Allow:    httpx.Bool(r.PostForm.Get("allow"), false),
	}, false, nil
}

func devicePageStatus(err error) int {
	switch {
	case errors.Is(err, domain.ErrNotFound), errors.Is(err, domain.ErrConflict):
		return http.StatusNotFound
	case errors.Is(err, domain.ErrForbidden), errors.Is(err, domain.ErrUnauthorized):
		return http.StatusForbidden
	case errors.Is(err, domain.ErrInvalidInput):
		return http.StatusBadRequest
	default:
		return http.StatusInternalServerError
	}
}

func devicePageMessage(err error) string {
	switch {
	case errors.Is(err, domain.ErrNotFound), errors.Is(err, domain.ErrConflict):
		return "授权码不存在、已过期或已被使用"
	case errors.Is(err, domain.ErrForbidden), errors.Is(err, domain.ErrUnauthorized):
		return "你没有该项目的编辑权限（至少需要 editor）"
	case errors.Is(err, domain.ErrInvalidInput):
		return "请求参数不正确"
	default:
		return "授权暂时不可用，请稍后再试"
	}
}

// deviceNeedsLogin：账密模式跳登录页并带回跳；统一认证模式给出明确提示。
func (s *Server) deviceNeedsLogin(w http.ResponseWriter, r *http.Request) {
	if s.deps.DisablePassword {
		writeDevicePage(w, http.StatusForbidden, deviceView{Error: "请通过统一认证网关访问授权页"})
		return
	}
	next := "/__device"
	if code := strings.TrimSpace(r.URL.Query().Get("code")); code != "" {
		next += "?code=" + url.QueryEscape(code)
	}
	http.Redirect(w, r, "/__login?next="+url.QueryEscape(next), http.StatusSeeOther)
}

func writeDevicePage(w http.ResponseWriter, status int, view deviceView) {
	writePageHeaders(w)
	w.WriteHeader(status)
	_ = devicePageTemplate.Execute(w, view)
}

// writePageHeaders 与登录/邀请页同一套保护：无脚本、无外部资源、不缓存。
func writePageHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Content-Type", "text/html; charset=utf-8")
	h.Set("Cache-Control", "no-store")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
}
