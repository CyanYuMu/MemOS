// Package zerotrust 校验零信任 Web 网关注入的 X-Zt-Authorization JWT。
//
// 网关在用户完成 UUAP 认证后签发 HS256 JWT，放在请求头里转给业务；
// 业务必须验证签名与时效后才能把其中的 username 当作身份。
package zerotrust

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// HeaderName 是零信任网关注入身份的请求头。
const HeaderName = "X-Zt-Authorization"

const (
	maxTokenBytes   = 8 << 10
	defaultLeeway   = time.Minute
	minSecretLength = 16
)

// ErrInvalidToken 表示签名、格式或时效校验失败。
var ErrInvalidToken = errors.New("zerotrust: invalid token")

// Verifier 用一组域名的 JWT Secret 校验网关凭据。零信任网关为每个域名分配不同密钥；
// 轮换期可以同时配置新旧两个 Secret。
type Verifier struct {
	secrets [][]byte
	now     func() time.Time
	leeway  time.Duration
}

// New 用网关平台「基本信息」里的 JWT Secret 构造校验器。
// 传入多个 Secret 时逐个验签，任一匹配即通过。
func New(secrets []string) (*Verifier, error) {
	return NewWithClock(secrets, time.Now, defaultLeeway)
}

// NewWithClock 供测试注入时钟与允许的时钟偏移。
func NewWithClock(secrets []string, now func() time.Time, leeway time.Duration) (*Verifier, error) {
	keys := make([][]byte, 0, len(secrets))
	seen := make(map[string]struct{}, len(secrets))
	for _, raw := range secrets {
		secret := strings.TrimSpace(raw)
		if len(secret) < minSecretLength {
			return nil, fmt.Errorf("zerotrust: 每个 secret 至少 %d 字节", minSecretLength)
		}
		if _, ok := seen[secret]; ok {
			continue
		}
		seen[secret] = struct{}{}
		keys = append(keys, []byte(secret))
	}
	if len(keys) == 0 {
		return nil, errors.New("zerotrust: 至少需要一个 secret")
	}
	if now == nil {
		now = time.Now
	}
	if leeway < 0 {
		leeway = 0
	}
	return &Verifier{secrets: keys, now: now, leeway: leeway}, nil
}

// Verify 校验凭据并返回身份。第二个返回值为 false 表示凭据合法但没有用户身份
// （网站在 Bypass 模式或请求命中白名单时 username 为空），调用方应按未登录处理。
func (v *Verifier) Verify(token string) (domain.ExternalIdentity, bool, error) {
	if v == nil || len(v.secrets) == 0 {
		return domain.ExternalIdentity{}, false, ErrInvalidToken
	}
	token = strings.TrimSpace(token)
	if token == "" || len(token) > maxTokenBytes {
		return domain.ExternalIdentity{}, false, invalid("token 为空或过长")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return domain.ExternalIdentity{}, false, invalid("段数不为 3")
	}

	var head struct {
		Alg string `json:"alg"`
	}
	if err := decodeSegment(parts[0], &head); err != nil {
		return domain.ExternalIdentity{}, false, invalid("header 不是合法 JSON")
	}
	if head.Alg != "HS256" {
		return domain.ExternalIdentity{}, false, invalid("alg 不是 HS256")
	}

	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return domain.ExternalIdentity{}, false, invalid("签名不是合法 base64url")
	}
	signed := []byte(parts[0] + "." + parts[1])
	verified := false
	for _, secret := range v.secrets {
		mac := hmac.New(sha256.New, secret)
		mac.Write(signed)
		if hmac.Equal(mac.Sum(nil), sig) {
			verified = true
			break
		}
	}
	if !verified {
		return domain.ExternalIdentity{}, false, invalid("签名不匹配")
	}

	var claims struct {
		Iat      int64  `json:"iat"`
		Exp      int64  `json:"exp"`
		Username string `json:"username"`
		Name     string `json:"name"`
		Email    string `json:"email"`
	}
	if err := decodeSegment(parts[1], &claims); err != nil {
		return domain.ExternalIdentity{}, false, invalid("payload 不是合法 JSON")
	}

	now := v.now()
	if claims.Exp <= 0 {
		return domain.ExternalIdentity{}, false, invalid("缺少 exp")
	}
	if now.After(time.Unix(claims.Exp, 0).Add(v.leeway)) {
		return domain.ExternalIdentity{}, false, invalid("已过期")
	}
	if claims.Iat > 0 && time.Unix(claims.Iat, 0).After(now.Add(v.leeway)) {
		return domain.ExternalIdentity{}, false, invalid("iat 在未来")
	}

	username := strings.TrimSpace(claims.Username)
	if username == "" {
		return domain.ExternalIdentity{}, false, nil
	}
	return domain.ExternalIdentity{
		Source:   "zerotrust",
		Username: username,
		Name:     strings.TrimSpace(claims.Name),
		Email:    strings.TrimSpace(claims.Email),
	}, true, nil
}

func decodeSegment(seg string, dst any) error {
	raw, err := base64.RawURLEncoding.DecodeString(seg)
	if err != nil {
		return fmt.Errorf("%w: base64: %w", ErrInvalidToken, err)
	}
	if err := json.Unmarshal(raw, dst); err != nil {
		return fmt.Errorf("%w: json: %w", ErrInvalidToken, err)
	}
	return nil
}

func invalid(reason string) error {
	return fmt.Errorf("%w: %s", ErrInvalidToken, reason)
}
