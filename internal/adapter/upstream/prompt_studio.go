package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 提示词工作台的数据面接口（dumemos 的 /product/prompts/*）。
//
// 这批接口的错误体是 `{code: "<稳定字符串>", message, data}`，与控制面信封的
// 整数 code 不是一回事，因此不能复用 mapHTTPError：只看 HTTP 状态码会丢掉
// 版本冲突里的当前版本号与限流里的等待秒数，也无法区分模型失败与模型超时。

// promptErrorBody 是提示词接口的错误响应。
//
// 版本冲突里的当前版本在数据面的实际形状是 `data.current.version_id`（联调确认）；
// 同时接受扁平的 `current_version_id`，避免对方换个摆放方式就丢掉版本号。
type promptErrorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Data    struct {
		CurrentVersionID int64 `json:"current_version_id"`
		Current          *struct {
			VersionID int64 `json:"version_id"`
			VersionNo int64 `json:"version_no"`
		} `json:"current"`
		RetryAfterSec int64 `json:"retry_after_sec"`
		Errors        []struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"errors"`
	} `json:"data"`
}

// currentVersionNo 取冲突里当前版本的展示序号。
func (b promptErrorBody) currentVersionNo() int64 {
	if b.Data.Current != nil {
		return b.Data.Current.VersionNo
	}
	return 0
}

// currentVersionID 取冲突里的当前版本号；两种形状都认。
func (b promptErrorBody) currentVersionID() int64 {
	if b.Data.CurrentVersionID > 0 {
		return b.Data.CurrentVersionID
	}
	if b.Data.Current != nil {
		return b.Data.Current.VersionID
	}
	return 0
}

// promptHTTPError 把数据面的 4xx/5xx 映射成控制面的领域错误。
// message 只是内部原因，对外文案在这里定稿：上游错误体面向用户，
// 但控制面不假设它一定可安全展示，缺失时给本模块的兜底文案。
func promptHTTPError(status int, raw []byte) error {
	var body promptErrorBody
	_ = json.Unmarshal(raw, &body)
	message := promptText(body.Message, "提示词操作没有成功，请稍后重试")
	switch status {
	case http.StatusBadRequest:
		if len(body.Data.Errors) > 0 {
			issues := make([]domain.PromptIssue, 0, len(body.Data.Errors))
			for _, item := range body.Data.Errors {
				issues = append(issues, domain.PromptIssue{Code: item.Code, Message: item.Message})
			}
			return &domain.InvalidPromptError{Message: message, Errors: issues}
		}
		return domain.Invalid("prompt", message)
	case http.StatusNotFound:
		// 只有带稳定 code 的 404 才是"这条提示词不存在"；路由本身没部署时
		// FastAPI 回的是 {"detail":"Not Found"}，那是控制面侧的问题，不能
		// 让用户以为是自己选错了提示词。
		switch body.Code {
		case "unknown_prompt_key":
			return domain.NotFound("prompt", "这条提示词不存在，请刷新页面后重试")
		case "unknown_version":
			// 回滚目标不存在，或根本不属于这个项目：统一按不存在处理，不泄露归属。
			return domain.NotFound("prompt", "这个历史版本不存在，请刷新版本历史后再试")
		}
		return mapHTTPError(status, raw)
	case http.StatusConflict:
		return &domain.VersionConflictError{
			CurrentVersionID: body.currentVersionID(),
			CurrentVersionNo: body.currentVersionNo(),
		}
	case http.StatusRequestEntityTooLarge:
		return domain.Wrap(domain.ErrTooLarge, "prompt", "内容太长，请精简后再试", nil)
	case http.StatusTooManyRequests:
		wait := time.Duration(body.Data.RetryAfterSec) * time.Second
		if wait <= 0 {
			wait = time.Second
		}
		return &domain.BusyError{Wait: wait, Message: "当前请求太多，请稍后重试"}
	case http.StatusBadGateway:
		return domain.Wrap(domain.ErrModelFailed, "prompt", "模型调用失败，请稍后重试", nil)
	case http.StatusGatewayTimeout:
		return domain.Wrap(domain.ErrModelTimeout, "prompt", "模型响应超时，请稍后重试", nil)
	default:
		return mapHTTPError(status, raw)
	}
}

// promptText 取上游文案，超长或为空时用兜底值；不透传上游地址与堆栈。
func promptText(message, fallback string) string {
	message = safeMessage(message)
	if message == "" || message == "记忆服务拒绝了该请求" {
		return fallback
	}
	return message
}

// checkPromptEnvelope 是 2xx 的判据：沿用 cloud 风格（code 为 0/200 或缺省）。
func checkPromptEnvelope(env envelope) error {
	if env.Code == nil || *env.Code == 0 || *env.Code == http.StatusOK {
		return nil
	}
	if *env.Code == http.StatusNotFound {
		return domain.NotFound("prompt", "这条提示词不存在，请刷新页面后重试")
	}
	return domain.Wrap(domain.ErrUpstreamRejected, "prompt", safeMessage(env.Message), nil)
}

func (c *Client) postPrompt(ctx context.Context, path string, payload any) (json.RawMessage, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("upstream: marshal prompt request: %w", err)
	}
	env, err := c.doChecked(ctx, checkedRequest{
		Method:     http.MethodPost,
		Path:       path,
		Body:       body,
		Check:      checkPromptEnvelope,
		CheckError: promptHTTPError,
	})
	if err != nil {
		return nil, err
	}
	if len(env.Data) == 0 || string(env.Data) == "null" {
		return nil, domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务没有返回提示词数据", nil)
	}
	return env.Data, nil
}

// PromptGateway 让 Client 以提示词工作台的端口出现。Client 同时是记忆、知识库
// 与画像模板的客户端，直接在它上面加 List/Save/Reset/Test 会让读代码的人
// 分不清是哪一组动作。
type PromptGateway struct{ c *Client }

// NewPromptGateway 包装上游客户端。
func NewPromptGateway(c *Client) *PromptGateway { return &PromptGateway{c: c} }

type promptStateWire struct {
	Source    string  `json:"source"`
	VersionID int64   `json:"version_id"`
	VersionNo int64   `json:"version_no"`
	UpdatedAt *string `json:"updated_at"`
	Author    *string `json:"author"`
}

type promptItemWire struct {
	Key       string                     `json:"key"`
	Group     string                     `json:"group"`
	Title     map[string]string          `json:"title"`
	Summary   map[string]string          `json:"summary"`
	Languages []string                   `json:"languages"`
	Editable  bool                       `json:"editable"`
	State     map[string]promptStateWire `json:"state"`
}

type promptPlaceholderWire struct {
	Name        string            `json:"name"`
	Required    bool              `json:"required"`
	Description map[string]string `json:"description"`
}

type promptOutputContractWire struct {
	Format       string            `json:"format"`
	RequiredKeys []string          `json:"required_keys"`
	ItemKeys     []string          `json:"item_keys"`
	Description  map[string]string `json:"description"`
}

type promptSampleWire struct {
	ID    string            `json:"id"`
	Lang  string            `json:"lang"`
	Title map[string]string `json:"title"`
	Input json.RawMessage   `json:"input"`
}

type promptEffectiveWire struct {
	Body      string  `json:"body"`
	Source    string  `json:"source"`
	VersionID int64   `json:"version_id"`
	VersionNo int64   `json:"version_no"`
	UpdatedAt *string `json:"updated_at"`
	Author    *string `json:"author"`
}

func (w promptStateWire) toDomain() domain.PromptSlotState {
	return domain.PromptSlotState{
		Source: w.Source, VersionID: w.VersionID, VersionNo: w.VersionNo,
		UpdatedAt: w.UpdatedAt, Author: w.Author,
	}
}

func (w promptEffectiveWire) toDomain() domain.PromptEffective {
	return domain.PromptEffective{
		Body: w.Body, Source: w.Source, VersionID: w.VersionID, VersionNo: w.VersionNo,
		UpdatedAt: w.UpdatedAt, Author: w.Author,
	}
}

// List 返回项目的提示词清单。
func (g *PromptGateway) List(ctx context.Context, tenantID string) (domain.PromptCatalog, error) {
	data, err := g.c.postPrompt(ctx, "/product/prompts/list", map[string]any{"mem_cube_id": tenantID})
	if err != nil {
		return domain.PromptCatalog{}, err
	}
	var wire struct {
		Prompts []promptItemWire `json:"prompts"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptCatalog{}, promptDecodeError(err)
	}
	out := domain.PromptCatalog{Prompts: make([]domain.PromptItem, 0, len(wire.Prompts))}
	for _, item := range wire.Prompts {
		state := make(map[string]domain.PromptSlotState, len(item.State))
		for lang, slot := range item.State {
			state[lang] = slot.toDomain()
		}
		out.Prompts = append(out.Prompts, domain.PromptItem{
			Key:       item.Key,
			Group:     item.Group,
			Title:     item.Title,
			Summary:   item.Summary,
			Languages: item.Languages,
			Editable:  item.Editable,
			State:     state,
		})
	}
	return out, nil
}

// Detail 返回一条提示词的注册项、默认值与生效值。
func (g *PromptGateway) Detail(ctx context.Context, tenantID, key string) (domain.PromptDetail, error) {
	data, err := g.c.postPrompt(ctx, "/product/prompts/detail",
		map[string]any{"mem_cube_id": tenantID, "key": key})
	if err != nil {
		return domain.PromptDetail{}, err
	}
	var wire struct {
		promptItemWire
		Syntax         string                         `json:"syntax"`
		Placeholders   []promptPlaceholderWire        `json:"placeholders"`
		LockedSuffix   *string                        `json:"locked_suffix"`
		InputKind      string                         `json:"input_kind"`
		InputSchema    json.RawMessage                `json:"input_schema"`
		OutputContract promptOutputContractWire       `json:"output_contract"`
		SampleInputs   []promptSampleWire             `json:"sample_inputs"`
		Defaults       map[string]string              `json:"defaults"`
		Effective      map[string]promptEffectiveWire `json:"effective"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptDetail{}, promptDecodeError(err)
	}
	out := domain.PromptDetail{
		Key:          wire.Key,
		Group:        wire.Group,
		Title:        wire.Title,
		Summary:      wire.Summary,
		Languages:    wire.Languages,
		Syntax:       wire.Syntax,
		LockedSuffix: wire.LockedSuffix,
		InputKind:    wire.InputKind,
		InputSchema:  wire.InputSchema,
		OutputContract: domain.PromptOutputContract{
			Format:       wire.OutputContract.Format,
			RequiredKeys: wire.OutputContract.RequiredKeys,
			ItemKeys:     wire.OutputContract.ItemKeys,
			Description:  wire.OutputContract.Description,
		},
		Editable: wire.Editable,
		Defaults: wire.Defaults,
	}
	out.Placeholders = make([]domain.PromptPlaceholder, 0, len(wire.Placeholders))
	for _, item := range wire.Placeholders {
		out.Placeholders = append(out.Placeholders, domain.PromptPlaceholder{
			Name: item.Name, Required: item.Required, Description: item.Description,
		})
	}
	out.SampleInputs = make([]domain.PromptSample, 0, len(wire.SampleInputs))
	for _, item := range wire.SampleInputs {
		out.SampleInputs = append(out.SampleInputs, domain.PromptSample{
			ID: item.ID, Lang: item.Lang, Title: item.Title, Input: item.Input,
		})
	}
	out.Effective = make(map[string]domain.PromptEffective, len(wire.Effective))
	for lang, slot := range wire.Effective {
		out.Effective[lang] = slot.toDomain()
	}
	return out, nil
}

// Versions 返回一个槽位的历史版本。
func (g *PromptGateway) Versions(
	ctx context.Context, in domain.PromptVersionsQuery,
) (domain.PromptVersionPage, error) {
	payload := map[string]any{
		"mem_cube_id": in.TenantID,
		"key":         in.Key,
		"lang":        in.Lang,
	}
	if in.Limit > 0 {
		payload["limit"] = in.Limit
	}
	if in.BeforeVersionID > 0 {
		payload["before_version_id"] = in.BeforeVersionID
	}
	data, err := g.c.postPrompt(ctx, "/product/prompts/versions", payload)
	if err != nil {
		return domain.PromptVersionPage{}, err
	}
	var wire struct {
		Versions []struct {
			VersionID       int64  `json:"version_id"`
			VersionNo       int64  `json:"version_no"`
			Lang            string `json:"lang"`
			Kind            string `json:"kind"`
			Body            string `json:"body"`
			Note            string `json:"note"`
			Author          string `json:"author"`
			CreatedAt       string `json:"created_at"`
			ParentVersionID *int64 `json:"parent_version_id"`
		} `json:"versions"`
		HasMore bool `json:"has_more"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptVersionPage{}, promptDecodeError(err)
	}
	out := domain.PromptVersionPage{
		Versions: make([]domain.PromptVersion, 0, len(wire.Versions)),
		HasMore:  wire.HasMore,
	}
	for _, item := range wire.Versions {
		out.Versions = append(out.Versions, domain.PromptVersion{
			VersionID:       item.VersionID,
			VersionNo:       item.VersionNo,
			Lang:            item.Lang,
			Kind:            item.Kind,
			Body:            item.Body,
			Note:            item.Note,
			Author:          item.Author,
			CreatedAt:       item.CreatedAt,
			ParentVersionID: item.ParentVersionID,
		})
	}
	return out, nil
}

// Validate 静态校验草稿，不落库。
func (g *PromptGateway) Validate(
	ctx context.Context, in domain.PromptValidateInput,
) (domain.PromptValidation, error) {
	data, err := g.c.postPrompt(ctx, "/product/prompts/validate", map[string]any{
		"key": in.Key, "lang": in.Lang, "body": in.Body,
	})
	if err != nil {
		return domain.PromptValidation{}, err
	}
	return decodePromptValidation(data)
}

func decodePromptValidation(data json.RawMessage) (domain.PromptValidation, error) {
	var wire struct {
		OK     bool `json:"ok"`
		Errors []struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"errors"`
		Warnings []struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"warnings"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptValidation{}, promptDecodeError(err)
	}
	out := domain.PromptValidation{
		OK:       wire.OK,
		Errors:   make([]domain.PromptIssue, 0, len(wire.Errors)),
		Warnings: make([]domain.PromptIssue, 0, len(wire.Warnings)),
	}
	for _, item := range wire.Errors {
		out.Errors = append(out.Errors, domain.PromptIssue{Code: item.Code, Message: item.Message})
	}
	for _, item := range wire.Warnings {
		out.Warnings = append(out.Warnings, domain.PromptIssue{Code: item.Code, Message: item.Message})
	}
	return out, nil
}

// Save 保存一个槽位的新版本；Body 与 RestoreFromVersionID 二选一。
func (g *PromptGateway) Save(
	ctx context.Context, in domain.PromptSaveInput,
) (domain.PromptSaveResult, error) {
	payload := map[string]any{
		"mem_cube_id":     in.TenantID,
		"key":             in.Key,
		"lang":            in.Lang,
		"author":          in.Author,
		"base_version_id": in.BaseVersionID,
	}
	if in.RestoreFromVersionID > 0 {
		payload["restore_from_version_id"] = in.RestoreFromVersionID
	} else {
		payload["body"] = in.Body
	}
	if in.Note != "" {
		payload["note"] = in.Note
	}
	data, err := g.c.postPrompt(ctx, "/product/prompts/save", payload)
	if err != nil {
		return domain.PromptSaveResult{}, err
	}
	var wire struct {
		VersionID int64               `json:"version_id"`
		VersionNo int64               `json:"version_no"`
		CreatedAt string              `json:"created_at"`
		Effective promptEffectiveWire `json:"effective"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptSaveResult{}, promptDecodeError(err)
	}
	effective := wire.Effective.toDomain()
	versionNo := wire.VersionNo
	if versionNo == 0 {
		// 数据面只在 effective 里给序号时，顶层也用同一个值，响应形状保持稳定。
		versionNo = effective.VersionNo
	}
	return domain.PromptSaveResult{
		VersionID: wire.VersionID,
		VersionNo: versionNo,
		CreatedAt: wire.CreatedAt,
		Effective: effective,
	}, nil
}

// Reset 把槽位还原成内置默认；Key 或 Lang 为空表示范围更大。
func (g *PromptGateway) Reset(
	ctx context.Context, in domain.PromptResetInput,
) (domain.PromptResetResult, error) {
	payload := map[string]any{"mem_cube_id": in.TenantID, "author": in.Author}
	if in.Key != "" {
		payload["key"] = in.Key
	}
	if in.Lang != "" {
		payload["lang"] = in.Lang
	}
	data, err := g.c.postPrompt(ctx, "/product/prompts/reset", payload)
	if err != nil {
		return domain.PromptResetResult{}, err
	}
	// 数据面返回 {"reset": [...]}（spec 第 6 节）。
	var wire struct {
		Reset []struct {
			Key       string `json:"key"`
			Lang      string `json:"lang"`
			VersionID int64  `json:"version_id"`
			VersionNo int64  `json:"version_no"`
		} `json:"reset"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptResetResult{}, promptDecodeError(err)
	}
	out := domain.PromptResetResult{Restored: make([]domain.PromptResetItem, 0, len(wire.Reset))}
	for _, item := range wire.Reset {
		out.Restored = append(out.Restored, domain.PromptResetItem{
			Key: item.Key, Lang: item.Lang, VersionID: item.VersionID, VersionNo: item.VersionNo,
		})
	}
	return out, nil
}

// Test 用真实抽取路径试跑草稿；数据面不写任何存储。
func (g *PromptGateway) Test(
	ctx context.Context, in domain.PromptTestInput,
) (domain.PromptTestResult, error) {
	payload := map[string]any{
		"mem_cube_id":     in.TenantID,
		"key":             in.Key,
		"lang":            in.Lang,
		"compare_default": in.CompareDefault,
	}
	if in.Body != "" {
		payload["body"] = in.Body
	}
	if in.Input != nil {
		payload["input"] = in.Input
	}
	data, err := g.c.postPrompt(ctx, "/product/prompts/test", payload)
	if err != nil {
		return domain.PromptTestResult{}, err
	}
	var wire struct {
		Runs []struct {
			Label             string `json:"label"`
			OK                bool   `json:"ok"`
			ParseOK           bool   `json:"parse_ok"`
			FallbackTriggered bool   `json:"fallback_triggered"`
			Contract          struct {
				OK      bool     `json:"ok"`
				Missing []string `json:"missing"`
			} `json:"contract"`
			Output    json.RawMessage `json:"output"`
			RawOutput string          `json:"raw_output"`
			Truncated bool            `json:"truncated"`
			LatencyMS int64           `json:"latency_ms"`
			Model     string          `json:"model"`
			Usage     map[string]any  `json:"usage"`
			UsedSlots []struct {
				Key       string `json:"key"`
				Lang      string `json:"lang"`
				Source    string `json:"source"`
				VersionID int64  `json:"version_id"`
			} `json:"used_slots"`
			Warnings []struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"warnings"`
			Error *struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		} `json:"runs"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptTestResult{}, promptDecodeError(err)
	}
	out := domain.PromptTestResult{Runs: make([]domain.PromptTestRun, 0, len(wire.Runs))}
	for i := range wire.Runs {
		item := &wire.Runs[i]
		run := domain.PromptTestRun{
			Label:             item.Label,
			OK:                item.OK,
			ParseOK:           item.ParseOK,
			FallbackTriggered: item.FallbackTriggered,
			Contract: domain.PromptContractCheck{
				OK:      item.Contract.OK,
				Missing: append([]string{}, item.Contract.Missing...),
			},
			Output:    item.Output,
			RawOutput: item.RawOutput,
			Truncated: item.Truncated,
			LatencyMS: item.LatencyMS,
			Model:     item.Model,
			Usage:     item.Usage,
		}
		for _, slot := range item.UsedSlots {
			run.UsedSlots = append(run.UsedSlots, domain.PromptSlotRef{
				Key: slot.Key, Lang: slot.Lang, Source: slot.Source, VersionID: slot.VersionID,
			})
		}
		for _, warning := range item.Warnings {
			run.Warnings = append(run.Warnings, domain.PromptIssue{
				Code: warning.Code, Message: warning.Message,
			})
		}
		if item.Error != nil {
			run.Error = &domain.PromptRunError{Code: item.Error.Code, Message: item.Error.Message}
		}
		out.Runs = append(out.Runs, run)
	}
	return out, nil
}

// Generate 让数据面按一句话指令改写提示词，不保存。
func (g *PromptGateway) Generate(
	ctx context.Context, in domain.PromptGenerateInput,
) (domain.PromptGenerateResult, error) {
	data, err := g.c.postPrompt(ctx, "/product/prompts/generate", map[string]any{
		"mem_cube_id": in.TenantID,
		"key":         in.Key,
		"lang":        in.Lang,
		"instruction": in.Instruction,
		"base_body":   in.BaseBody,
	})
	if err != nil {
		return domain.PromptGenerateResult{}, err
	}
	var wire struct {
		Body       string          `json:"body"`
		Validation json.RawMessage `json:"validation"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return domain.PromptGenerateResult{}, promptDecodeError(err)
	}
	validation, err := decodePromptValidation(wire.Validation)
	if err != nil {
		return domain.PromptGenerateResult{}, err
	}
	return domain.PromptGenerateResult{Body: wire.Body, Validation: validation}, nil
}

func promptDecodeError(err error) error {
	return domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务返回了无法解析的提示词数据", err)
}
