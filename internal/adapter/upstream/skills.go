package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const skillResponseMaxBytes = 8 << 20

var skillErrorCode = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

// SkillGateway 只连接部署时配置的独立 Skill 服务，绝不接受浏览器提供的 URL。
type SkillGateway struct {
	baseURL string
	token   string
	http    *http.Client
}

// NewSkillGateway 的配置由启动期 config.Validate 校验，禁用重定向以免服务凭据外发。
func NewSkillGateway(baseURL, token string) *SkillGateway {
	return &SkillGateway{baseURL: strings.TrimRight(baseURL, "/"), token: token, http: &http.Client{
		Timeout:       30 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

// Call 每次只发一个请求。写入的重试/幂等由用户操作与 Python operationId 裁定。
func (g *SkillGateway) Call(ctx context.Context, call domain.SkillCall) (json.RawMessage, error) {
	if err := validateSkillCall(call); err != nil {
		return nil, err
	}
	raw, err := json.Marshal(call.Payload)
	if err != nil {
		return nil, domain.Invalid("skill", "Skill 请求无法编码")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.baseURL+"/v1/skills/"+call.Action, bytes.NewReader(raw))
	if err != nil {
		return nil, skillUnavailable()
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+g.token)
	req.Header.Set("X-Skill-Actor", call.Actor)
	req.Header.Set("X-Skill-Capability", call.Capability)
	req.Header.Set("X-Skill-Project", call.ProjectID)
	resp, err := g.http.Do(req)
	if err != nil {
		return nil, skillUnavailable()
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, skillResponseMaxBytes+1))
	if err != nil || len(body) > skillResponseMaxBytes {
		return nil, skillUnavailable()
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusAccepted {
		return nil, decodeSkillError(resp.StatusCode, body)
	}
	var envelope struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil || len(envelope.Data) == 0 || bytes.Equal(bytes.TrimSpace(envelope.Data), []byte("null")) {
		return nil, skillUnavailable()
	}
	return envelope.Data, nil
}

func validateSkillCall(call domain.SkillCall) error {
	switch call.Action {
	case "list", "detail", "evidence", "evaluate", "run", "job", "job_reconcile", "review_prepare", "review_decide", "publish", "status", "challenge", "load_preview", "experiments_list", "experiment_detail", "experiment_start", "adjudication_prepare", "adjudicate", "impact_scan", "impact_prepare", "impact_review", "challenge_resolve":
	default:
		return domain.Invalid("skill", "未知 Skill 操作")
	}
	if call.Capability != "view" && call.Capability != "write" && call.Capability != "manage" {
		return domain.Invalid("skill", "无效 Skill 权限")
	}
	for _, value := range []string{call.Actor, call.ProjectID} {
		if value == "" || strings.ContainsAny(value, "\r\n\x00") {
			return domain.Invalid("skill", "缺少可信 Skill 身份")
		}
	}
	if project, ok := call.Payload["projectId"].(string); !ok || project != call.ProjectID {
		return domain.Invalid("skill", "Skill 项目不匹配")
	}
	return nil
}

func skillUnavailable() *domain.SkillServiceError {
	return &domain.SkillServiceError{Status: http.StatusServiceUnavailable, Code: "service_unavailable"}
}

func decodeSkillError(status int, body []byte) *domain.SkillServiceError {
	result := skillUnavailable()
	switch status {
	case 400, 403, 404, 409, 422, 503:
		result.Status = status
	default:
		return result // 401 是服务凭据故障，不能让浏览器错误地退出登录。
	}
	var envelope struct {
		Error struct {
			Code    string `json:"code"`
			Details struct {
				CurrentSequence *int64 `json:"currentSequence"`
			} `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &envelope) != nil {
		return result
	}
	if skillErrorCode.MatchString(envelope.Error.Code) {
		result.Code = envelope.Error.Code
	}
	if sequence := envelope.Error.Details.CurrentSequence; sequence != nil && *sequence >= 0 && *sequence <= 1<<53-1 {
		result.CurrentSequence = sequence
	}
	return result
}
