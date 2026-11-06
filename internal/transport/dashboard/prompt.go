package dashboard

import (
	"context"
	"net/http"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// 抽取 prompt 由上游按项目保存（dumemos 的 extract_prompt_configs）。
// 控制面只代理：不落第二份配置，也不改上游源码或 ConfigMap。
// 上游未配置或未部署该接口时返回 503，不伪装成空数据。

const promptsUnavailable = "上游未启用项目级 prompt 配置：请确认 memos-api 已部署 /product/*_extract_prompt 接口"

func (s *Server) promptDetail(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if s.deps.Prompts == nil {
		return nil, domain.Wrap(domain.ErrDisabled, "dashboard", promptsUnavailable, nil)
	}
	tenant := httpx.String(body["projectId"])
	cfg, err := s.deps.Prompts.GetExtractPrompt(ctx, tenant)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"projectId":       tenant,
		"prompt":          cfg.Prompt,
		"extractionTypes": cfg.ExtractionTypes,
		"updatedAt":       cfg.UpdatedAt,
		"source":          cfg.Source,
		"isDefault":       cfg.IsDefault,
	}, nil
}

func (s *Server) promptSave(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if s.deps.Prompts == nil {
		return nil, domain.Wrap(domain.ErrDisabled, "dashboard", promptsUnavailable, nil)
	}
	prompt := httpx.String(body["prompt"])
	if prompt == "" {
		return nil, domain.Invalid("dashboard", "prompt 不能为空")
	}
	if err := s.deps.Prompts.SaveExtractPrompt(ctx, httpx.String(body["projectId"]), prompt); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true}, nil
}

func (s *Server) promptReset(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if s.deps.Prompts == nil {
		return nil, domain.Wrap(domain.ErrDisabled, "dashboard", promptsUnavailable, nil)
	}
	cfg, err := s.deps.Prompts.ResetExtractPrompt(ctx, httpx.String(body["projectId"]))
	if err != nil {
		return nil, err
	}
	return map[string]any{"prompt": cfg.Prompt}, nil
}

func (s *Server) promptGenerate(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if s.deps.Prompts == nil {
		return nil, domain.Wrap(domain.ErrDisabled, "dashboard", promptsUnavailable, nil)
	}
	description := httpx.String(body["description"])
	if description == "" {
		return nil, domain.Invalid("dashboard", "description 不能为空")
	}
	prompt, err := s.deps.Prompts.GenerateExtractPrompt(
		ctx, httpx.String(body["projectId"]), description,
	)
	if err != nil {
		return nil, err
	}
	return map[string]any{"prompt": prompt}, nil
}

func (s *Server) promptDebug(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if s.deps.Prompts == nil {
		return nil, domain.Wrap(domain.ErrDisabled, "dashboard", promptsUnavailable, nil)
	}
	types := httpx.StringList(body["extractionTypes"])
	if len(types) == 0 {
		types = httpx.StringList(body["extraction_types"])
	}
	result, err := s.deps.Prompts.DebugExtract(
		ctx, httpx.String(body["projectId"]), promptMessages(body["messages"]), types,
	)
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(result.Results))
	for _, item := range result.Results {
		records = append(records, map[string]any{"type": item.Type, "content": item.Content})
	}
	return map[string]any{
		"extractionTypes": result.ExtractionTypes,
		"results":         records,
	}, nil
}

func promptMessages(raw any) []domain.PromptMessage {
	items, _ := raw.([]any)
	out := make([]domain.PromptMessage, 0, len(items))
	for _, item := range items {
		message, ok := item.(map[string]any)
		if !ok {
			continue
		}
		role := httpx.String(message["role"])
		if role == "" {
			role = "user"
		}
		out = append(out, domain.PromptMessage{Role: role, Content: httpx.String(message["content"])})
	}
	return out
}

// models：模型清单由上游从自己进程的环境变量读出，控制面不再读部署 ConfigMap。
func (s *Server) models(
	ctx context.Context, _ domain.Principal, _ *http.Request, _ map[string]any,
) (any, error) {
	if s.deps.Models == nil {
		return nil, domain.Wrap(domain.ErrDisabled, "dashboard",
			"上游未启用模型清单接口：请确认 memos-api 已部署 /product/models", nil)
	}
	items, err := s.deps.Models.Models(ctx)
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(items))
	for _, item := range items {
		records = append(records, map[string]any{
			"role":   item.Role,
			"model":  item.Model,
			"spec":   item.Spec,
			"source": item.Source,
			"group":  item.Group,
		})
	}
	return map[string]any{"records": records, "total": len(records)}, nil
}
