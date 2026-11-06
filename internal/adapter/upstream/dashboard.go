package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"encoding/json/jsontext"
	jsonv2 "encoding/json/v2"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const (
	// defaultDashboardMax 只防上游无限吐；正常的一页（含 148 MB 的偏好桶）远低于它。
	defaultDashboardMax int64 = 512 << 20
	maxErrorBytes       int64 = 64 << 10
	maxDashboardPage          = 100
)

var errDashboardTooLarge = errors.New("dashboard response exceeds limit")

// Dashboard 调 /product/get_memory_dashboard 并流式解码：不在内存里持有整个响应，
// 目标结构之外的成员（metadata.sources、embedding 等）由 jsontext 逐 token 跳过。
func (c *Client) Dashboard(ctx context.Context, q domain.DashboardQuery) (domain.DashboardPage, error) {
	totalKey, ok := dashboardTotalKeys[q.Bucket]
	if !ok {
		return domain.DashboardPage{}, domain.Invalid("dashboard", "未知的记忆桶")
	}
	if q.TenantID == "" {
		return domain.DashboardPage{}, domain.Invalid("dashboard", "缺少租户条件")
	}
	page := max(q.Page, 1)
	size := min(max(q.PageSize, 1), maxDashboardPage)
	payload := map[string]any{
		"mem_cube_id":         q.TenantID,
		"page":                page,
		"page_size":           size,
		"include_preference":  q.Bucket == domain.DashboardBucketPref,
		"include_tool_memory": q.Bucket == domain.DashboardBucketTool,
		// 契约 v2 默认不带 sources；一行偏好页曾因此到 148 MB。
		"include_sources": false,
	}
	if len(q.Filter) > 0 {
		payload["filter"] = q.Filter
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return domain.DashboardPage{}, fmt.Errorf("upstream: marshal dashboard request: %w", err)
	}
	env, err := c.streamDashboard(ctx, body)
	if err != nil {
		return domain.DashboardPage{}, err
	}
	if env.Code != nil && *env.Code != http.StatusOK {
		return domain.DashboardPage{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream", safeMessage(env.Message), nil)
	}
	return env.Data.page(q.Bucket, q.TenantID, totalKey), nil
}

var dashboardTotalKeys = map[domain.DashboardBucket]string{
	domain.DashboardBucketText: "total_text_nodes",
	domain.DashboardBucketPref: "total_preference_nodes",
	domain.DashboardBucketTool: "total_tool_nodes",
}

func (c *Client) streamDashboard(ctx context.Context, body []byte) (dashboardEnvelope, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/product/get_memory_dashboard", bytes.NewReader(body))
	if err != nil {
		return dashboardEnvelope{}, fmt.Errorf("upstream: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		c.log.WarnContext(ctx, "upstream unreachable", slog.String("error", err.Error()))
		return dashboardEnvelope{}, domain.Wrap(domain.ErrUpstreamUnavailable, "upstream", "暂时连不上记忆服务，请稍后重试。", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBytes))
		return dashboardEnvelope{}, mapHTTPError(resp.StatusCode, raw)
	}
	var env dashboardEnvelope
	err = jsonv2.UnmarshalRead(&cappedReader{r: resp.Body, max: c.dashboardMax}, &env, jsontext.AllowInvalidUTF8(true))
	switch {
	case err == nil:
		return env, nil
	case errors.Is(err, errDashboardTooLarge):
		c.log.WarnContext(ctx, "dashboard response too large", slog.Int64("limit_bytes", c.dashboardMax))
		return dashboardEnvelope{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务响应过大，已拒绝", err)
	default:
		return dashboardEnvelope{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务返回了无法解析的响应", err)
	}
}

// cappedReader 在累计读取超过 max 时返回 errDashboardTooLarge，而不是静默截断。
type cappedReader struct {
	r   io.Reader
	max int64
	n   int64
}

func (c *cappedReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	if c.n > c.max {
		return n, errDashboardTooLarge
	}
	return n, err
}

type dashboardEnvelope struct {
	Code    *int          `json:"code"`
	Message string        `json:"message"`
	Data    dashboardData `json:"data"`
}

type dashboardData struct {
	TextMem    groupList      `json:"text_mem"`
	PrefMem    groupList      `json:"pref_mem"`
	ToolMem    groupList      `json:"tool_mem"`
	Statistics map[string]any `json:"statistics"`
}

type dashboardGroup struct {
	CubeID   string            `json:"cube_id"`
	Memories []dashboardMemory `json:"memories"`
}

type dashboardMemory struct {
	ID       lenientString     `json:"id"`
	Memory   lenientString     `json:"memory"`
	Metadata dashboardMetadata `json:"metadata"`
}

// dashboardMetadata 是 metadata 的白名单。没列出的成员（sources、embedding、history 等）不会被物化。
type dashboardMetadata struct {
	MemoryType        lenientString `json:"memory_type"`
	Status            lenientString `json:"status"`
	CreatedAt         lenientString `json:"created_at"`
	UserID            lenientString `json:"user_id"`
	MemCubeID         lenientString `json:"mem_cube_id"`
	CubeID            lenientString `json:"cube_id"`
	Key               lenientString `json:"key"`
	OSSURL            lenientString `json:"oss_url"`
	TemplateID        lenientString `json:"template_id"`
	ProfileTemplateID lenientString `json:"profile_template_id"`
	ProfileInstanceID lenientString `json:"profile_instance_id"`
}

// groupList 只接受数组；桶没被请求时上游可能给 null 或别的形状，一律当空处理。
type groupList []dashboardGroup

func (g *groupList) UnmarshalJSONFrom(dec *jsontext.Decoder) error {
	if dec.PeekKind() != '[' {
		return dec.SkipValue()
	}
	return jsonv2.UnmarshalDecode(dec, (*[]dashboardGroup)(g))
}

// lenientString 接受字符串、数字、布尔，null 与复合值当空串，避免一个字段的形状异常拖垮整页。
type lenientString string

func (s *lenientString) UnmarshalJSONFrom(dec *jsontext.Decoder) error {
	switch dec.PeekKind() {
	case '"':
		var v string
		if err := jsonv2.UnmarshalDecode(dec, &v); err != nil {
			return err
		}
		*s = lenientString(v)
		return nil
	case '0', 't', 'f':
		raw, err := dec.ReadValue()
		if err != nil {
			return err
		}
		*s = lenientString(raw)
		return nil
	default:
		return dec.SkipValue()
	}
}

func (d dashboardData) page(bucket domain.DashboardBucket, tenant, totalKey string) domain.DashboardPage {
	var groups groupList
	switch bucket {
	case domain.DashboardBucketText:
		groups = d.TextMem
	case domain.DashboardBucketPref:
		groups = d.PrefMem
	case domain.DashboardBucketTool:
		groups = d.ToolMem
	}
	rows := make([]domain.DashboardRow, 0, 16)
	seen := make(map[string]struct{})
	for i := range groups {
		if groups[i].CubeID != "" && groups[i].CubeID != tenant {
			continue
		}
		for j := range groups[i].Memories {
			row, keep := groups[i].Memories[j].row(tenant)
			if !keep {
				continue
			}
			if _, dup := seen[row.ID]; dup {
				continue
			}
			seen[row.ID] = struct{}{}
			rows = append(rows, row)
		}
	}
	total := len(rows)
	if n, ok := d.Statistics[totalKey].(float64); ok && n >= 0 {
		total = int(n)
	}
	return domain.DashboardPage{Rows: rows, Total: total}
}

// row 投影一条记忆；软删除、没有 id、或元数据声明了别的租户时不保留。
func (m dashboardMemory) row(tenant string) (domain.DashboardRow, bool) {
	md := m.Metadata
	if m.ID == "" || md.Status == "deleted" {
		return domain.DashboardRow{}, false
	}
	owner := firstNonEmpty(string(md.MemCubeID), string(md.CubeID))
	if owner != "" && owner != tenant {
		return domain.DashboardRow{}, false
	}
	return domain.DashboardRow{
		ID:         string(m.ID),
		Content:    string(m.Memory),
		NodeType:   string(md.MemoryType),
		CreatedAt:  string(md.CreatedAt),
		UserID:     string(md.UserID),
		TemplateID: firstNonEmpty(string(md.TemplateID), string(md.ProfileTemplateID)),
		InstanceID: string(md.ProfileInstanceID),
	}, true
}
