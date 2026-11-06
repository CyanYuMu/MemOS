package domain

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"
)

// TokenKind 区分租户 Token 与管理 Token。两种 Token 不混用。
type TokenKind string

const (
	TokenKindTenant TokenKind = "tenant"
	TokenKindAdmin  TokenKind = "admin"
)

// RawTokenPrefix 是 token 明文的固定前缀，便于识别与日志掩码；控制台的 keyPrefix 也取自它。
const RawTokenPrefix = "sk-"

// Token 是 API Token 的元数据。明文只在签发时返回一次，这里只保存哈希。
type Token struct {
	ID       string
	Kind     TokenKind
	TenantID string
	UserID   string
	Hash     [32]byte
	Prefix   string // 明文前 8 位，列表展示用
	// Raw 是明文，只为「有写权限的成员在控制台再复制一次」而保存（迁移 0024）。
	// 历史行与旧网关导入的 Key 为空——那时明文已经不可追溯；列表会显示前缀掩码并
	// 提示重新生成。空值绝不能被当成「明文就是空串」下发给前端。
	Raw        string
	Label      string
	CreatedAt  time.Time
	LastUsedAt *time.Time
	RevokedAt  *time.Time
	// ExpiresAt 是过期时间；nil 表示永不过期（启动配置里的静态 Token）。
	ExpiresAt *time.Time
	// CreatedByAccountID 只用于审计；管理 Token 创建则为空串。
	CreatedByAccountID string
}

// Validate 检查令牌类型与归属字段的一致性。
func (t Token) Validate() error {
	if t.ID == "" {
		return Invalid("token", "token id 不能为空")
	}
	if t.Hash == ([32]byte{}) {
		return Invalid("token", "token 哈希不能为空")
	}
	switch t.Kind {
	case TokenKindTenant:
		if t.TenantID == "" || t.UserID == "" {
			return Invalid("token", "租户 token 必须有 tenant_id 与 user_id")
		}
	case TokenKindAdmin:
		if t.TenantID != "" {
			return Invalid("token", "管理 token 不能绑定租户")
		}
	default:
		return Invalid("token", "未知的 token 类型")
	}
	return nil
}

// Active 表示 token 未被吊销。
func (t Token) Active() bool { return t.RevokedAt == nil }

// Expired 表示 token 在 now 时刻已过期。ExpiresAt 为 nil 表示永不过期；
// 过期时间等于 now 视为已过期，避免边界上的“多用一个瞬间”。
func (t Token) Expired(now time.Time) bool {
	return t.ExpiresAt != nil && !now.Before(*t.ExpiresAt)
}

// NewRawToken 生成 32 字节随机 token（hex 编码，带 sk- 前缀）。
func NewRawToken() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", Wrap(ErrInvalidInput, "token", "无法生成随机 token", err)
	}
	return RawTokenPrefix + hex.EncodeToString(buf), nil
}

// HashToken 返回 token 的 SHA-256 哈希。token 本身是 256 位随机值，
// 不需要逐条 salt；确定性哈希才允许按哈希建索引查找。
func HashToken(raw string) [32]byte {
	return sha256.Sum256([]byte(raw))
}

// TokenPrefix 取明文前 8 个字符用于列表展示。
func TokenPrefix(raw string) string {
	if len(raw) <= 8 {
		return raw
	}
	return raw[:8]
}

// MaskToken 掩码 token：长度 ≤ 12 时全掩码，否则保留前 4 与后 4。
func MaskToken(raw string) string {
	if raw == "" {
		return ""
	}
	if len(raw) <= 12 {
		return strings.Repeat("*", len(raw))
	}
	return raw[:4] + "…" + raw[len(raw)-4:]
}
