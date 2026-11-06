package upstream

import (
	"context"
	"encoding/json"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// promptData 对应上游 prompt 接口的 data 形状。
type promptData struct {
	Prompt          string   `json:"prompt"`
	ExtractionTypes []string `json:"extraction_types"`
	UpdatedAt       *string  `json:"updated_at"`
	Source          string   `json:"source"`
	IsDefault       bool     `json:"is_default"`
}

func (d promptData) toDomain() domain.ExtractPrompt {
	updated := ""
	if d.UpdatedAt != nil {
		updated = *d.UpdatedAt
	}
	return domain.ExtractPrompt{
		Prompt:          d.Prompt,
		ExtractionTypes: d.ExtractionTypes,
		UpdatedAt:       updated,
		Source:          d.Source,
		IsDefault:       d.IsDefault,
	}
}

func decodePrompt(env envelope, dst any) error {
	if len(env.Data) == 0 || string(env.Data) == "null" {
		return domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务没有返回 prompt 配置", nil)
	}
	if err := json.Unmarshal(env.Data, dst); err != nil {
		return domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务返回了无法解析的 prompt 配置", err)
	}
	return nil
}

// GetExtractPrompt 读取项目级抽取 prompt；未保存过时上游返回内置默认值。
func (c *Client) GetExtractPrompt(ctx context.Context, tenantID string) (domain.ExtractPrompt, error) {
	env, err := c.postCloud(ctx, "/product/get_extract_prompt",
		map[string]any{"mem_cube_id": tenantID})
	if err != nil {
		return domain.ExtractPrompt{}, err
	}
	var data promptData
	if err := decodePrompt(env, &data); err != nil {
		return domain.ExtractPrompt{}, err
	}
	return data.toDomain(), nil
}

// SaveExtractPrompt 保存项目级抽取 prompt；上游校验 ${conversation} 占位符。
func (c *Client) SaveExtractPrompt(ctx context.Context, tenantID, prompt string) error {
	_, err := c.postCloud(ctx, "/product/save_extract_prompt",
		map[string]any{"mem_cube_id": tenantID, "prompt": prompt})
	return err
}

// ResetExtractPrompt 删除项目保存的 prompt，返回内置默认值。
func (c *Client) ResetExtractPrompt(ctx context.Context, tenantID string) (domain.ExtractPrompt, error) {
	env, err := c.postCloud(ctx, "/product/reset_extract_prompt",
		map[string]any{"mem_cube_id": tenantID})
	if err != nil {
		return domain.ExtractPrompt{}, err
	}
	var data promptData
	if err := decodePrompt(env, &data); err != nil {
		return domain.ExtractPrompt{}, err
	}
	return data.toDomain(), nil
}

// GenerateExtractPrompt 让上游用当前模型重写 prompt，但不保存。
func (c *Client) GenerateExtractPrompt(ctx context.Context, tenantID, description string) (string, error) {
	env, err := c.postCloud(ctx, "/product/generate_extract_prompt",
		map[string]any{"mem_cube_id": tenantID, "description": description})
	if err != nil {
		return "", err
	}
	var data promptData
	if err := decodePrompt(env, &data); err != nil {
		return "", err
	}
	return data.Prompt, nil
}

// DebugExtract 用项目当前 prompt 干跑一次抽取；上游不写图库与向量库。
func (c *Client) DebugExtract(
	ctx context.Context, tenantID string, messages []domain.PromptMessage, types []string,
) (domain.ExtractDebug, error) {
	payload := map[string]any{"mem_cube_id": tenantID}
	if len(messages) > 0 {
		items := make([]map[string]string, 0, len(messages))
		for _, m := range messages {
			items = append(items, map[string]string{"role": m.Role, "content": m.Content})
		}
		payload["messages"] = items
	}
	if len(types) > 0 {
		payload["extraction_types"] = types
	}
	env, err := c.postCloud(ctx, "/product/debug_extract", payload)
	if err != nil {
		return domain.ExtractDebug{}, err
	}
	var data struct {
		ExtractionTypes []string `json:"extraction_types"`
		Results         []struct {
			Type    string `json:"type"`
			Content string `json:"content"`
		} `json:"results"`
	}
	if err := decodePrompt(env, &data); err != nil {
		return domain.ExtractDebug{}, err
	}
	out := domain.ExtractDebug{ExtractionTypes: data.ExtractionTypes}
	for _, r := range data.Results {
		out.Results = append(out.Results, domain.ExtractDebugResult{Type: r.Type, Content: r.Content})
	}
	return out, nil
}

// Models 读取上游进程实际配置的模型清单（来自部署环境变量）。
func (c *Client) Models(ctx context.Context) ([]domain.ModelInfo, error) {
	env, err := c.doChecked(ctx, checkedRequest{Method: "GET", Path: "/product/models", Check: checkCloudEnvelope})
	if err != nil {
		return nil, err
	}
	var data struct {
		Records []struct {
			Role   string `json:"role"`
			Model  string `json:"model"`
			Spec   string `json:"spec"`
			Source string `json:"source"`
			Group  string `json:"group"`
		} `json:"records"`
	}
	if err := decodePrompt(env, &data); err != nil {
		return nil, err
	}
	out := make([]domain.ModelInfo, 0, len(data.Records))
	for _, r := range data.Records {
		out = append(out, domain.ModelInfo{
			Role: r.Role, Model: r.Model, Spec: r.Spec, Source: r.Source, Group: r.Group,
		})
	}
	return out, nil
}
