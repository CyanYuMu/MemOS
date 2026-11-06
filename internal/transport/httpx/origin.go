package httpx

import (
	"net/http"
	"net/url"
	"strings"
)

// SameOriginPost 拒绝浏览器发起的跨站导航型 POST，供邀请确认页与设备授权页这类
// “原生表单提交”的路由复用。非浏览器客户端通常不带 Sec-Fetch-Site 与 Origin，
// 此时放行：它们仍要携带有效身份，且不构成浏览器 CSRF。
//
// Chrome 对同源 HTML 表单提交（含 JS 触发的导航型 POST）会发 `Origin: null`。
// 但 `Origin: null` 也来自沙箱 iframe、跨源重定向等不透明来源，不能无条件放行：
// 只有同时带 `Sec-Fetch-Site: same-origin` 时才认定是同源表单。没有 Sec-Fetch
// 元数据的旧浏览器若发 `null` 会被拒绝，宁可让极端浏览器改用账号/Host 同源路径，
// 也不打开 CSRF 缺口；真正的跨站表单要么带真实来源，要么 `cross-site`/`same-site`，
// 同样在下面的检查里被拒。
func SameOriginPost(r *http.Request) bool {
	site := r.Header.Get("Sec-Fetch-Site")
	switch site {
	case "", "same-origin", "none":
	default:
		return false
	}
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	if strings.EqualFold(origin, "null") {
		return site == "same-origin"
	}
	if origin == "" {
		// 非浏览器客户端（curl、插件、测试）通常既不发 Origin 也不发 Sec-Fetch。
		return true
	}
	u, err := url.Parse(origin)
	if err != nil || u.Host == "" {
		return false
	}
	if strings.EqualFold(u.Host, r.Host) {
		return true
	}
	// 反向网关可能把 Host 改写成内部服务名；转发头由网关写入，浏览器跨站表单
	// 无法伪造。只在两者都有值且一致时放行；多级代理会带逗号列表，取第一段。
	forwarded := strings.TrimSpace(strings.Split(r.Header.Get("X-Forwarded-Host"), ",")[0])
	return forwarded != "" && strings.EqualFold(u.Host, forwarded)
}
