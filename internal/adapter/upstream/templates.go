package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 画像模板接口属于 cloud 风格：成功 code 为 0，不存在是 code 404（HTTP 仍是 200）。
func checkCloudEnvelope(env envelope) error {
	if env.Code == nil || *env.Code == 0 || *env.Code == http.StatusOK {
		return nil
	}
	if *env.Code == http.StatusNotFound {
		return domain.NotFound("upstream", "模板不存在")
	}
	return domain.Wrap(domain.ErrUpstreamRejected, "upstream", safeMessage(env.Message), nil)
}

func (c *Client) postCloud(ctx context.Context, path string, payload any) (envelope, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return envelope{}, fmt.Errorf("upstream: marshal request: %w", err)
	}
	return c.doChecked(ctx, checkedRequest{Method: http.MethodPost, Path: path, Body: body, Check: checkCloudEnvelope})
}

type rawTemplate struct {
	ID                string         `json:"id"`
	ProfileTemplateID string         `json:"profile_template_id"`
	Name              string         `json:"name"`
	Metadata          map[string]any `json:"metadata"`
	BoundCount        int            `json:"bound_count"`
	UpdatedAt         any            `json:"updated_at"`
}

func (r rawTemplate) toDomain() domain.ProfileTemplate {
	updated, _ := r.UpdatedAt.(string)
	return domain.ProfileTemplate{
		ID:         firstNonEmpty(r.ProfileTemplateID, r.ID),
		Name:       r.Name,
		Metadata:   r.Metadata,
		BoundCount: r.BoundCount,
		UpdatedAt:  updated,
	}
}

// ListProfileTemplates 调 /product/list_profile_template；租户边界由 mem_cube_id 保证（上游按 user_name 校验归属）。
func (c *Client) ListProfileTemplates(
	ctx context.Context,
	tenantID string,
	page,
	size int,
) (domain.ProfileTemplatePage, error) {
	env, err := c.postCloud(ctx, "/product/list_profile_template",
		map[string]any{"mem_cube_id": tenantID, "page": max(page, 1), "page_size": min(max(size, 1), 200)})
	if err != nil {
		return domain.ProfileTemplatePage{}, err
	}
	var data struct {
		List  []rawTemplate `json:"profile_template_list"`
		Total int           `json:"total"`
	}
	if err := decodeData(env, &data); err != nil {
		return domain.ProfileTemplatePage{}, err
	}
	out := domain.ProfileTemplatePage{Items: make([]domain.ProfileTemplate, 0, len(data.List)), Total: data.Total}
	for i := range data.List {
		out.Items = append(out.Items, data.List[i].toDomain())
	}
	return out, nil
}

// GetProfileTemplate 调 /product/get_profile_template；不属于该租户或不存在都返回 ErrNotFound。
func (c *Client) GetProfileTemplate(ctx context.Context, tenantID, id string) (domain.ProfileTemplate, error) {
	env, err := c.postCloud(ctx, "/product/get_profile_template",
		map[string]any{"mem_cube_id": tenantID, "profile_template_id": id})
	if err != nil {
		return domain.ProfileTemplate{}, err
	}
	var raw rawTemplate
	if err := decodeData(env, &raw); err != nil {
		return domain.ProfileTemplate{}, err
	}
	return raw.toDomain(), nil
}

// AddProfileTemplate 调 /product/add_profile_template，返回新模板 ID。
func (c *Client) AddProfileTemplate(
	ctx context.Context,
	tenantID,
	name string,
	metadata map[string]any,
) (string, error) {
	env, err := c.postCloud(ctx, "/product/add_profile_template",
		map[string]any{"mem_cube_id": tenantID, "name": name, "metadata": metadata})
	if err != nil {
		return "", err
	}
	return templateID(env)
}

// UpdateProfileTemplate 调 /product/update_profile_template；只带需要改的字段，
// 仅改名时不带 metadata，上游才会保留原有属性树。
func (c *Client) UpdateProfileTemplate(
	ctx context.Context,
	tenantID,
	id string,
	in domain.ProfileTemplateUpdate,
) (string, error) {
	payload := map[string]any{"mem_cube_id": tenantID, "profile_template_id": id}
	if in.Name != nil {
		payload["name"] = *in.Name
	}
	if len(in.Metadata) > 0 {
		payload["metadata"] = in.Metadata
	}
	env, err := c.postCloud(ctx, "/product/update_profile_template", payload)
	if err != nil {
		return "", err
	}
	if got, err := templateID(env); err == nil && got != "" {
		return got, nil
	}
	return id, nil
}

// DeleteProfileTemplate 调 /product/delete_profile_template；上游会把已绑定实例置为 detached。
func (c *Client) DeleteProfileTemplate(ctx context.Context, tenantID, id string) error {
	_, err := c.postCloud(ctx, "/product/delete_profile_template",
		map[string]any{"mem_cube_id": tenantID, "profile_template_id": id})
	return err
}

func templateID(env envelope) (string, error) {
	var data struct {
		ProfileTemplateID string `json:"profile_template_id"`
		ID                string `json:"id"`
	}
	if err := decodeData(env, &data); err != nil {
		return "", err
	}
	return firstNonEmpty(data.ProfileTemplateID, data.ID), nil
}

func decodeData(env envelope, dst any) error {
	if len(env.Data) == 0 || string(env.Data) == "null" {
		return domain.NotFound("upstream", "模板不存在")
	}
	if err := json.Unmarshal(env.Data, dst); err != nil {
		return domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务返回了无法解析的响应", err)
	}
	return nil
}
