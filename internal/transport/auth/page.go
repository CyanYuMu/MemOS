package auth

import (
	"html/template"
	"net/http"
	"strings"
)

// 登录页自带样式，不引外部资源；错误文案只来自固定字符串，不回显输入。
var loginPage = template.Must(template.New("login").Parse(`<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · Memory Console</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f4f5f7;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#1f2329}
main{width:360px;max-width:92vw;background:#fff;border-radius:12px;padding:32px 28px;box-shadow:0 8px 30px rgba(0,0,0,.08)}
h1{margin:0 0 4px;font-size:20px}
p.sub{margin:0 0 20px;color:#646a73}
label{display:block;margin:14px 0 6px;font-weight:600}
input{width:100%;padding:10px 12px;border:1px solid #d0d3d9;border-radius:8px;font:inherit}
input:focus{outline:2px solid #3370ff;outline-offset:-1px;border-color:#3370ff}
button{margin-top:22px;width:100%;padding:10px 12px;border:0;border-radius:8px;background:#3370ff;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:hover{background:#245bdb}
.err{margin:0 0 4px;padding:9px 12px;border-radius:8px;background:#fdecea;color:#b42318}
</style>
</head>
<body>
<main>
<h1>Memory Console</h1>
<p class="sub">使用账号登录控制台</p>
{{if .Error}}<p class="err" role="alert">{{.Error}}</p>{{end}}
<form method="post" action="/__login" autocomplete="on">
{{if .Next}}<input type="hidden" name="next" value="{{.Next}}">{{end}}
<label for="username">账号</label>
<input id="username" name="username" type="text" autocomplete="username" autocapitalize="none" spellcheck="false" required autofocus>
<label for="password">口令</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">登录</button>
</form>
</main>
</body>
</html>
`))

type loginView struct {
	Error string
	Next  string
}

// 页面里出现的错误文案都在这里，调用方只能选，不能拼接。
const (
	msgLoginFailed = "用户名或口令错误"
	msgRateLimited = "尝试次数过多，请稍后再试"
	msgLoginBroken = "登录暂时不可用，请稍后再试"
)

func setPageHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Content-Type", "text/html; charset=utf-8")
	h.Set("Cache-Control", "no-store")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
}

func writeLoginPage(w http.ResponseWriter, status int, errText, next string) {
	setPageHeaders(w)
	w.WriteHeader(status)
	_ = loginPage.Execute(w, loginView{Error: errText, Next: safeNext(next)})
}

// safeNext 只允许站内绝对路径，挡住 //evil.com 与 /\evil.com 这类协议相对跳转。
// 控制字符也拒绝：/%09/evil.com 解码后是 "/\t/evil.com"，部分浏览器会剥掉 TAB
// 再归一成 "//evil.com"。
func safeNext(raw string) string {
	raw = strings.TrimSpace(raw)
	isAbsolutePath := strings.HasPrefix(raw, "/")
	isProtocolRelative := strings.HasPrefix(raw, "//") || strings.HasPrefix(raw, "/\\")
	if !isAbsolutePath || isProtocolRelative {
		return "/"
	}
	for _, r := range raw {
		if r < 0x20 || r == 0x7f {
			return "/"
		}
	}
	return raw
}
