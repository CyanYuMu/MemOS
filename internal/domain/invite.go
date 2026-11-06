package domain

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"
)

// InviteTokenPrefix 是邀请明文的前缀，便于日志与人工辨认。
const InviteTokenPrefix = "inv_"

// inviteTokenBytes 是邀请 token 的随机字节数；hex 后长度 64。
const inviteTokenBytes = 32

// ProjectInvite 是一条项目邀请。明文 token 不保存在结构里，只有 SHA-256。
type ProjectInvite struct {
	ID                 string
	TenantID           string
	Role               Role
	TokenHash          [32]byte
	CreatedByAccountID string
	CreatedAt          time.Time
	ExpiresAt          time.Time
	UsedAt             *time.Time
	UsedByAccountID    string
	RevokedAt          *time.Time
}

// InviteStatus 是邀请的对外状态，判定顺序 revoked → used → expired → active。
type InviteStatus string

const (
	InviteActive  InviteStatus = "active"
	InviteUsed    InviteStatus = "used"
	InviteExpired InviteStatus = "expired"
	InviteRevoked InviteStatus = "revoked"
)

// Status 用调用方传入的时间判定状态；expires_at 等于 now 视为过期。
func (i ProjectInvite) Status(now time.Time) InviteStatus {
	switch {
	case i.RevokedAt != nil:
		return InviteRevoked
	case i.UsedAt != nil:
		return InviteUsed
	case !now.Before(i.ExpiresAt):
		return InviteExpired
	default:
		return InviteActive
	}
}

// NewInviteToken 生成 "inv_" + 32 字节 crypto/rand 的 hex；失败直接返回错误，
// 不降级到可预测的值。
func NewInviteToken() (string, error) {
	buf := make([]byte, inviteTokenBytes)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("invite token: %w", err)
	}
	return InviteTokenPrefix + hex.EncodeToString(buf), nil
}

// HashInviteToken 返回邀请明文的 SHA-256；与 token 一样用确定性哈希查库。
func HashInviteToken(raw string) [32]byte { return sha256.Sum256([]byte(raw)) }

// RedeemInviteInput 是一次兑换在存储层一个事务里要做的全部事情。
// SessionID 为空表示主体没有会话（零信任外部身份），当前项目写账号级偏好。
type RedeemInviteInput struct {
	InviteID  string
	TenantID  string
	Role      Role
	AccountID string
	SessionID string
	Now       time.Time
}
