package httpx

import (
	"net/http"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// SessionCookie 是控制台会话 cookie 的名字，沿用 BFF 的约定。
const SessionCookie = "memos_console_sid"

// BearerToken 从 Authorization 头取出 Bearer token。
// 缺失、方案不是 Bearer 或 token 为空时返回空串；不做任何校验。
func BearerToken(header string) string {
	header = strings.TrimSpace(header)
	if header == "" {
		return ""
	}
	scheme, rest, found := strings.Cut(header, " ")
	if !found || !strings.EqualFold(scheme, "Bearer") {
		return ""
	}
	return strings.TrimSpace(rest)
}

// CredentialsOf 收集请求携带的凭据：Authorization 头、会话 cookie 与网关注入的外部身份。
func CredentialsOf(r *http.Request) domain.Credentials {
	c := domain.Credentials{Bearer: BearerToken(r.Header.Get("Authorization"))}
	if ck, err := r.Cookie(SessionCookie); err == nil {
		c.Session = ck.Value
	}
	if id, ok := ExternalIdentityFromContext(r.Context()); ok {
		c.External = &id
	}
	return c
}
