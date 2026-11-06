package dashboard

import (
	"context"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	pageutil "github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/page"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/identity"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

const maxTokenExpireDays = 3650

// maxKeyLabelRunes 是 Key 展示名上限，与项目名保持一致的 64 字符量级。
const maxKeyLabelRunes = 64

func (s *Server) keyList(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	if projectID == "" {
		return nil, domain.Invalid("dashboard", "缺少 projectId")
	}
	tokens, err := s.deps.Tokens.ListTokens(ctx, projectID)
	if err != nil {
		return nil, err
	}
	// 明文只给能写这个项目的人：API Key 是租户级凭据，能复制就能绕过控制台角色直接
	// 写记忆。viewer 能看列表（知道有哪些 Key、上次何时用过），但只拿前缀掩码。
	canCopy := p.RequireCap(projectID, domain.CapProjectWrite) == nil
	page, size := pageOf(body)
	records := make([]map[string]any, 0, len(tokens))
	for i := range tokens {
		if tokens[i].RevokedAt != nil {
			continue
		}
		records = append(records, keyJSON(tokens[i], canCopy))
	}
	return map[string]any{"records": pageutil.Slice(records, page, size), "total": len(records)}, nil
}

func (s *Server) keyCreate(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	label := strings.TrimSpace(httpx.String(body["keyName"]))
	if projectID == "" || label == "" {
		return nil, domain.Invalid("dashboard", "projectId 与 keyName 不能为空")
	}
	expireDays, err := tokenExpireDays(body)
	if err != nil {
		return nil, err
	}
	var expiresAt *time.Time
	if expireDays > 0 {
		expiry := time.Now().Add(time.Duration(expireDays) * 24 * time.Hour)
		expiresAt = &expiry
	}
	raw, tok, err := s.deps.Tokens.IssueTokenWithExpiry(ctx, identity.TokenIssueInput{
		TenantID:  projectID,
		Label:     label,
		CreatedBy: p.AccountID,
		ExpiresAt: expiresAt,
	})
	if err != nil {
		return nil, err
	}
	out := keyJSONIssued(tok)
	out["apiKey"] = raw // 明文只在这里出现一次
	return out, nil
}

// tokenExpireDays 解析可选的 expireDays：缺省或 0 表示永不过期，只接受
// 1..3650 的整数。前端误传字符串或小数时返回 400，而不是静默签发永不过期的 Key。
func tokenExpireDays(body map[string]any) (int, error) {
	raw, present := body["expireDays"]
	if !present || raw == nil {
		return 0, nil
	}
	var days int
	switch value := raw.(type) {
	case float64:
		if value != math.Trunc(value) || value < 0 || value > maxTokenExpireDays {
			return 0, domain.Invalid("dashboard", "expireDays 必须是 0..3650 的整数")
		}
		days = int(value)
	case int:
		days = value
	case string:
		parsed, err := strconv.Atoi(strings.TrimSpace(value))
		if err != nil {
			return 0, domain.Invalid("dashboard", "expireDays 必须是 0..3650 的整数")
		}
		days = parsed
	default:
		return 0, domain.Invalid("dashboard", "expireDays 必须是 0..3650 的整数")
	}
	if days < 0 || days > maxTokenExpireDays {
		return 0, domain.Invalid("dashboard", "expireDays 必须是 0..3650 的整数")
	}
	return days, nil
}

func (s *Server) keyRevoke(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	if err := s.deps.Tokens.RevokeToken(ctx, r.PathValue("id")); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

// keyRename 修改 Key 的展示名。明文与哈希都不变，改名后仍然可用；
// 列表里的 `keyName` 就是这个名字，便于区分同一项目的多把 Key。
func (s *Server) keyRename(ctx context.Context, p domain.Principal, r *http.Request, body map[string]any) (any, error) {
	label := strings.TrimSpace(httpx.String(body["keyName"]))
	if label == "" {
		return nil, domain.Invalid("dashboard", "keyName 不能为空")
	}
	if len([]rune(label)) > maxKeyLabelRunes {
		return nil, domain.Invalid("dashboard", "keyName 最长 64 个字符")
	}
	id := r.PathValue("id")
	if err := s.deps.Tokens.RenameToken(ctx, id, label); err != nil {
		return nil, err
	}
	tok, err := s.deps.Tokens.GetToken(ctx, id)
	if err != nil {
		return nil, err
	}
	return keyJSON(tok, p.RequireCap(tok.TenantID, domain.CapProjectWrite) == nil), nil
}

// keyCopy 返回密钥明文，供「列表里再复制一次」。
//
// 明文落库是本部署的取舍（迁移 0024）：内网控制台、能读库的人本来就有等价权限。
// 但接口仍按项目写权限鉴权，viewer 不能靠它把只读角色升级成写入能力。
func (s *Server) keyCopy(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	id := strings.TrimSpace(httpx.String(body["apiKeyId"]))
	if id == "" {
		return nil, domain.Invalid("dashboard", "缺少 apiKeyId")
	}
	tok, err := s.deps.Tokens.GetToken(ctx, id)
	if err != nil {
		return nil, err
	}
	if err := p.RequireCap(tok.TenantID, domain.CapProjectWrite); err != nil {
		return nil, err
	}
	if tok.Raw == "" {
		return nil, domain.Wrap(domain.ErrConflict, "dashboard",
			"这把密钥签发于「只存哈希」时期，明文已无法找回；请重新生成", nil)
	}
	return map[string]any{"apiKey": tok.Raw}, nil
}

// keyJSON 是列表/改名响应的形状：**始终只给前缀掩码**，明文不随列表批量下发。
// `canCopy` 只回答一个问题：点「复制」按钮时能不能通过 keys/copy 取到明文
// （需要项目写权限 + 库里留有明文）。
func keyJSON(t domain.Token, canCopy bool) map[string]any {
	expireTime := ""
	if t.ExpiresAt != nil {
		expireTime = domain.FormatBeijing(*t.ExpiresAt)
	}
	out := map[string]any{
		"id":         t.ID,
		"keyName":    t.Label,
		"apiKey":     maskKey(t.Prefix),
		"canCopy":    canCopy && t.Raw != "",
		"createTime": domain.FormatBeijing(t.CreatedAt),
		"expireTime": expireTime,
	}
	// 上次使用时间是判断「这把 Key 还在不在用」的唯一信号：长期没用的可以先吊销，
	// 轮换时也能看出新 Key 有没有真的接上。从没用过就不下发，前端显示「从未使用」。
	if t.LastUsedAt != nil {
		out["lastUsedAt"] = domain.FormatBeijing(*t.LastUsedAt)
	}
	return out
}

// keyJSONIssued 是创建响应的形状：这是明文唯一一次出现在响应体里（弹窗展示 + 复制）。
// 之后前端只能按 id 调 keys/copy 逐把取，避免列表一次吐出全部凭据。
func keyJSONIssued(t domain.Token) map[string]any {
	out := keyJSON(t, true)
	if t.Raw != "" {
		out["apiKey"] = t.Raw
		out["canCopy"] = true
	}
	return out
}

func maskKey(prefix string) string {
	if prefix == "" {
		return ""
	}
	return prefix + "…"
}
