package httpx

import (
	"net/http"
	"strings"
)

// LoggedOutCookie 是 /__logout 写入的“已主动退出”标记。
//
// 为什么需要它：零信任网关在每次请求上重新注入身份，UUAP 会话又常常还没失效，
// 只清本地会话时用户会在下一个请求里被静默登回来。标记是会话级 cookie（关闭浏览器
// 即失效），在 /__relogin 之前拦住所有请求，让“退出登录”这个动作在界面上真实生效。
const LoggedOutCookie = "memos_logged_out"

// LoggedOutGate 必须在 ZeroTrust 之前执行：它一旦放行，后面的 ZeroTrust 仍会把
// 网关注入的身份带进来。
func LoggedOutGate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := r.Cookie(LoggedOutCookie); err != nil {
			next.ServeHTTP(w, r)
			return
		}
		// 重新登录与重复登出本身要能走通，否则用户被永久困在退出页。
		switch r.URL.Path {
		case "/__relogin", "/__logout":
			next.ServeHTTP(w, r)
			return
		}
		// 浏览器导航给一个可读的页面；XHR/静态资源给 401，前端不会误以为已登录。
		if r.Method == http.MethodGet && strings.Contains(r.Header.Get("Accept"), "text/html") {
			writeLoggedOutPage(w)
			return
		}
		Fail(w, http.StatusUnauthorized, CodeUnauthorized, "已退出登录，请重新登录")
	})
}

// writeLoggedOutPage 是自包含的极小页面：不引用构建产物，退出后即使前端资源
// 还没加载也能看到状态。
func writeLoggedOutPage(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Content-Type", "text/html; charset=utf-8")
	h.Set("Cache-Control", "no-store")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>已退出登录</title>
<style>body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0;background:#f6f7f9;color:#1f2329}
main{text-align:center;padding:32px}h1{font-size:20px;margin:0 0 8px}p{margin:0 0 20px;color:#646a73}
a{display:inline-block;padding:8px 20px;border-radius:8px;background:#3370ff;color:#fff;text-decoration:none}</style>
</head><body><main><h1>已退出登录</h1><p>当前浏览器不会再自动进入控制台。</p>
<a href="/__relogin">重新登录</a></main></body></html>`))
}
