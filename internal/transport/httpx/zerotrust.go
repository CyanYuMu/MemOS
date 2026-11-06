package httpx

import (
	"context"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// ExternalVerifier 校验可信网关注入的凭据。第二个返回值为 false 表示凭据合法但没有用户身份。
type ExternalVerifier interface {
	Verify(token string) (domain.ExternalIdentity, bool, error)
}

type externalIdentityKey struct{}

// WithExternalIdentity 把网关认定的身份放进请求上下文。
func WithExternalIdentity(ctx context.Context, id domain.ExternalIdentity) context.Context {
	return context.WithValue(ctx, externalIdentityKey{}, id)
}

// ExternalIdentityFromContext 取出网关认定的身份。
func ExternalIdentityFromContext(ctx context.Context) (domain.ExternalIdentity, bool) {
	id, ok := ctx.Value(externalIdentityKey{}).(domain.ExternalIdentity)
	return id, ok
}

// ZeroTrust 校验网关注入的身份。header 缺失时直接放行，是否已登录由后续鉴权决定；
// header 存在但无效时返回 401，不降级到会话 cookie，避免网关会话失效后旧 cookie 仍然可用。
func ZeroTrust(v ExternalVerifier, header string) func(http.Handler) http.Handler {
	if header == "" {
		header = "X-Zt-Authorization"
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token := r.Header.Get(header)
			if token == "" {
				next.ServeHTTP(w, r)
				return
			}
			id, ok, err := v.Verify(token)
			if err != nil {
				Fail(w, http.StatusUnauthorized, CodeUnauthorized, "登录已过期，请重新登录")
				return
			}
			if !ok {
				next.ServeHTTP(w, r)
				return
			}
			next.ServeHTTP(w, r.WithContext(WithExternalIdentity(r.Context(), id)))
		})
	}
}
