package dashboard

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// 提示词工作台：控制面只代理数据面的注册表、版本与试跑，不保存任何默认文本。
// 八条路由的形状见 docs/module/prompt-studio.md 与 api/openapi.dashboard.yaml。
//
// 键名规则只有一条：信封里的字段从 snake_case 转 camelCase，但 inputSchema、
// sampleInputs[].input、runs[].output 与 defaults/effective 的语言层原样透传。

// PromptStudio 是提示词工作台的用例端口，由 service/prompt 实现。
// 控制面只做代理与限额，因此这里只有八个动作，没有存储职责。
type PromptStudio interface {
	List(ctx context.Context, tenantID string) (domain.PromptCatalog, error)
	Detail(ctx context.Context, tenantID, key string) (domain.PromptDetail, error)
	Versions(ctx context.Context, in domain.PromptVersionsQuery) (domain.PromptVersionPage, error)
	Validate(ctx context.Context, in domain.PromptValidateInput) (domain.PromptValidation, error)
	Save(ctx context.Context, in domain.PromptSaveInput) (domain.PromptSaveResult, error)
	Reset(ctx context.Context, in domain.PromptResetInput) (domain.PromptResetResult, error)
	Test(ctx context.Context, in domain.PromptTestInput) (domain.PromptTestResult, error)
	Generate(ctx context.Context, in domain.PromptGenerateInput) (domain.PromptGenerateResult, error)
}

type promptCatalogJSON struct {
	Prompts []promptItemJSON `json:"prompts"`
}

type promptItemJSON struct {
	Key       string                     `json:"key"`
	Group     string                     `json:"group"`
	Title     map[string]string          `json:"title"`
	Summary   map[string]string          `json:"summary"`
	Languages []string                   `json:"languages"`
	Editable  bool                       `json:"editable"`
	State     map[string]promptStateJSON `json:"state"`
}

type promptStateJSON struct {
	Source    string  `json:"source"`
	VersionID int64   `json:"versionId"`
	VersionNo int64   `json:"versionNo"`
	UpdatedAt *string `json:"updatedAt"`
	Author    *string `json:"author"`
}

type promptPlaceholderJSON struct {
	Name        string            `json:"name"`
	Required    bool              `json:"required"`
	Description map[string]string `json:"description"`
}

type promptOutputContractJSON struct {
	Format       string            `json:"format"`
	RequiredKeys []string          `json:"requiredKeys"`
	ItemKeys     []string          `json:"itemKeys"`
	Description  map[string]string `json:"description"`
}

type promptSampleJSON struct {
	ID    string            `json:"id"`
	Lang  string            `json:"lang"`
	Title map[string]string `json:"title"`
	Input json.RawMessage   `json:"input"`
}

type promptEffectiveJSON struct {
	Body      string  `json:"body"`
	Source    string  `json:"source"`
	VersionID int64   `json:"versionId"`
	VersionNo int64   `json:"versionNo"`
	UpdatedAt *string `json:"updatedAt"`
	Author    *string `json:"author"`
}

type promptDetailJSON struct {
	Key            string                         `json:"key"`
	Group          string                         `json:"group"`
	Title          map[string]string              `json:"title"`
	Summary        map[string]string              `json:"summary"`
	Languages      []string                       `json:"languages"`
	Syntax         string                         `json:"syntax"`
	Placeholders   []promptPlaceholderJSON        `json:"placeholders"`
	LockedSuffix   *string                        `json:"lockedSuffix"`
	InputKind      string                         `json:"inputKind"`
	InputSchema    json.RawMessage                `json:"inputSchema"`
	OutputContract promptOutputContractJSON       `json:"outputContract"`
	SampleInputs   []promptSampleJSON             `json:"sampleInputs"`
	Editable       bool                           `json:"editable"`
	Defaults       map[string]string              `json:"defaults"`
	Effective      map[string]promptEffectiveJSON `json:"effective"`
}

type promptVersionJSON struct {
	VersionID       int64  `json:"versionId"`
	VersionNo       int64  `json:"versionNo"`
	Lang            string `json:"lang"`
	Kind            string `json:"kind"`
	Body            string `json:"body"`
	Note            string `json:"note"`
	Author          string `json:"author"`
	CreatedAt       string `json:"createdAt"`
	ParentVersionID *int64 `json:"parentVersionId"`
}

type promptVersionsJSON struct {
	Versions []promptVersionJSON `json:"versions"`
	HasMore  bool                `json:"hasMore"`
}

type promptIssueJSON struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type promptValidationJSON struct {
	OK       bool              `json:"ok"`
	Errors   []promptIssueJSON `json:"errors"`
	Warnings []promptIssueJSON `json:"warnings"`
}

type promptSaveJSON struct {
	VersionID int64               `json:"versionId"`
	VersionNo int64               `json:"versionNo"`
	CreatedAt string              `json:"createdAt"`
	Effective promptEffectiveJSON `json:"effective"`
}

type promptResetItemJSON struct {
	Key       string `json:"key"`
	Lang      string `json:"lang"`
	VersionID int64  `json:"versionId"`
	VersionNo int64  `json:"versionNo"`
}

type promptResetJSON struct {
	Reset []promptResetItemJSON `json:"reset"`
}

type promptContractJSON struct {
	OK      bool     `json:"ok"`
	Missing []string `json:"missing"`
}

type promptRunErrorJSON struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type promptSlotRefJSON struct {
	Key       string `json:"key"`
	Lang      string `json:"lang"`
	Source    string `json:"source"`
	VersionID int64  `json:"versionId"`
}

type promptTestRunJSON struct {
	Label             string              `json:"label"`
	OK                bool                `json:"ok"`
	ParseOK           bool                `json:"parseOk"`
	FallbackTriggered bool                `json:"fallbackTriggered"`
	Contract          promptContractJSON  `json:"contract"`
	Output            json.RawMessage     `json:"output"`
	RawOutput         string              `json:"rawOutput"`
	Truncated         bool                `json:"truncated"`
	LatencyMS         int64               `json:"latencyMs"`
	Model             string              `json:"model"`
	Usage             map[string]any      `json:"usage"`
	UsedSlots         []promptSlotRefJSON `json:"usedSlots"`
	Warnings          []promptIssueJSON   `json:"warnings"`
	Error             *promptRunErrorJSON `json:"error,omitempty"`
}

type promptTestJSON struct {
	Runs []promptTestRunJSON `json:"runs"`
}

type promptGenerateJSON struct {
	Body       string               `json:"body"`
	Validation promptValidationJSON `json:"validation"`
}

func (s *Server) promptStudioReady() error {
	if s.deps.PromptStudio == nil {
		return disabledCapability("提示词工作台")
	}
	return nil
}

// promptsList 返回项目的提示词清单（不含正文）。
func (s *Server) promptsList(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	catalog, err := s.deps.PromptStudio.List(ctx, httpx.String(body["projectId"]))
	if err != nil {
		return nil, err
	}
	out := promptCatalogJSON{Prompts: make([]promptItemJSON, 0, len(catalog.Prompts))}
	for _, item := range catalog.Prompts {
		state := make(map[string]promptStateJSON, len(item.State))
		for lang, slot := range item.State {
			state[lang] = promptStateJSON{
				Source: slot.Source, VersionID: slot.VersionID, VersionNo: slot.VersionNo,
				UpdatedAt: slot.UpdatedAt, Author: slot.Author,
			}
		}
		out.Prompts = append(out.Prompts, promptItemJSON{
			Key:       item.Key,
			Group:     item.Group,
			Title:     item.Title,
			Summary:   item.Summary,
			Languages: normalizedStrings(item.Languages),
			Editable:  item.Editable,
			State:     state,
		})
	}
	return out, nil
}

// promptsDetail 返回注册项全文、各语言默认值与生效值。
func (s *Server) promptsDetail(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	key, err := promptKeyFrom(body)
	if err != nil {
		return nil, err
	}
	detail, err := s.deps.PromptStudio.Detail(ctx, httpx.String(body["projectId"]), key)
	if err != nil {
		return nil, err
	}
	out := promptDetailJSON{
		Key:          detail.Key,
		Group:        detail.Group,
		Title:        detail.Title,
		Summary:      detail.Summary,
		Languages:    normalizedStrings(detail.Languages),
		Syntax:       detail.Syntax,
		Placeholders: make([]promptPlaceholderJSON, 0, len(detail.Placeholders)),
		LockedSuffix: detail.LockedSuffix,
		InputKind:    detail.InputKind,
		InputSchema:  detail.InputSchema,
		OutputContract: promptOutputContractJSON{
			Format:       detail.OutputContract.Format,
			RequiredKeys: normalizedStrings(detail.OutputContract.RequiredKeys),
			ItemKeys:     normalizedStrings(detail.OutputContract.ItemKeys),
			Description:  detail.OutputContract.Description,
		},
		SampleInputs: make([]promptSampleJSON, 0, len(detail.SampleInputs)),
		Editable:     detail.Editable,
		Defaults:     detail.Defaults,
		Effective:    make(map[string]promptEffectiveJSON, len(detail.Effective)),
	}
	for _, item := range detail.Placeholders {
		out.Placeholders = append(out.Placeholders, promptPlaceholderJSON{
			Name: item.Name, Required: item.Required, Description: item.Description,
		})
	}
	for _, item := range detail.SampleInputs {
		out.SampleInputs = append(out.SampleInputs, promptSampleJSON{
			ID: item.ID, Lang: item.Lang, Title: item.Title, Input: item.Input,
		})
	}
	for lang, slot := range detail.Effective {
		out.Effective[lang] = promptEffectiveJSON{
			Body: slot.Body, Source: slot.Source, VersionID: slot.VersionID, VersionNo: slot.VersionNo,
			UpdatedAt: slot.UpdatedAt, Author: slot.Author,
		}
	}
	return out, nil
}

// promptsVersions 返回一个槽位的历史版本，新到旧。
func (s *Server) promptsVersions(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	key, err := promptKeyFrom(body)
	if err != nil {
		return nil, err
	}
	limit := httpx.Int(body["limit"], defaultPromptVersionLimit)
	if limit <= 0 {
		limit = defaultPromptVersionLimit
	}
	page, err := s.deps.PromptStudio.Versions(ctx, domain.PromptVersionsQuery{
		TenantID:        httpx.String(body["projectId"]),
		Key:             key,
		Lang:            httpx.String(body["lang"]),
		Limit:           min(limit, maxPromptVersionLimit),
		BeforeVersionID: int64(httpx.Int(body["beforeVersionId"], 0)),
	})
	if err != nil {
		return nil, err
	}
	out := promptVersionsJSON{
		Versions: make([]promptVersionJSON, 0, len(page.Versions)),
		HasMore:  page.HasMore,
	}
	for _, item := range page.Versions {
		out.Versions = append(out.Versions, promptVersionJSON{
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

// promptsValidate 静态校验草稿；始终 HTTP 200，错误与警告在 data 里。
func (s *Server) promptsValidate(
	ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	key, err := promptKeyFrom(body)
	if err != nil {
		return nil, err
	}
	lang, err := promptLangFrom(body)
	if err != nil {
		return nil, err
	}
	// 正文长度交给数据面判（始终 200 + too_long），这里只挡整个请求体。
	if err := checkPromptBodySize(body); err != nil {
		return nil, err
	}
	validation, err := s.deps.PromptStudio.Validate(ctx, domain.PromptValidateInput{
		Key: key, Lang: lang, Body: httpx.String(body["body"]),
	})
	if err != nil {
		return nil, err
	}
	return promptValidationToJSON(validation), nil
}

// promptsSave 保存一个槽位的新版本；author 取登录账号，忽略客户端传值。
func (s *Server) promptsSave(
	ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	key, err := promptKeyFrom(body)
	if err != nil {
		return nil, err
	}
	lang, err := promptLangFrom(body)
	if err != nil {
		return nil, err
	}
	result, err := s.deps.PromptStudio.Save(ctx, domain.PromptSaveInput{
		TenantID:             httpx.String(body["projectId"]),
		Key:                  key,
		Lang:                 lang,
		Body:                 httpx.String(body["body"]),
		RestoreFromVersionID: int64(httpx.Int(body["restoreFromVersionId"], 0)),
		Note:                 httpx.String(body["note"]),
		Author:               p.Actor(),
		BaseVersionID:        int64(httpx.Int(body["baseVersionId"], 0)),
	})
	if err != nil {
		return nil, err
	}
	return promptSaveJSON{
		VersionID: result.VersionID,
		VersionNo: result.VersionNo,
		CreatedAt: result.CreatedAt,
		Effective: promptEffectiveJSON{
			Body:      result.Effective.Body,
			Source:    result.Effective.Source,
			VersionID: result.Effective.VersionID,
			VersionNo: result.Effective.VersionNo,
			UpdatedAt: result.Effective.UpdatedAt,
			Author:    result.Effective.Author,
		},
	}, nil
}

// promptsReset 还原单条单语言、单条全部语言或整个项目；返回实际被还原的槽位。
func (s *Server) promptsReset(
	ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	result, err := s.deps.PromptStudio.Reset(ctx, domain.PromptResetInput{
		TenantID: httpx.String(body["projectId"]),
		Key:      httpx.String(body["key"]),
		Lang:     httpx.String(body["lang"]),
		Author:   p.Actor(),
	})
	if err != nil {
		return nil, err
	}
	out := promptResetJSON{Reset: make([]promptResetItemJSON, 0, len(result.Restored))}
	for _, item := range result.Restored {
		out.Reset = append(out.Reset, promptResetItemJSON{
			Key: item.Key, Lang: item.Lang, VersionID: item.VersionID, VersionNo: item.VersionNo,
		})
	}
	return out, nil
}

// promptsTest 用真实抽取路径试跑草稿，不保存、不写记忆。
func (s *Server) promptsTest(
	ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	key, err := promptKeyFrom(body)
	if err != nil {
		return nil, err
	}
	if err := checkPromptBodySize(body); err != nil {
		return nil, err
	}
	result, err := s.deps.PromptStudio.Test(ctx, domain.PromptTestInput{
		TenantID:       httpx.String(body["projectId"]),
		Key:            key,
		Lang:           httpx.String(body["lang"]),
		Body:           httpx.String(body["body"]),
		Input:          body["input"],
		Account:        p.Actor(),
		CompareDefault: httpx.Bool(body["compareDefault"], false),
	})
	if err != nil {
		return nil, err
	}
	out := promptTestJSON{Runs: make([]promptTestRunJSON, 0, len(result.Runs))}
	for i := range result.Runs {
		run := &result.Runs[i]
		item := promptTestRunJSON{
			Label:             run.Label,
			OK:                run.OK,
			ParseOK:           run.ParseOK,
			FallbackTriggered: run.FallbackTriggered,
			Contract: promptContractJSON{
				OK:      run.Contract.OK,
				Missing: normalizedStrings(run.Contract.Missing),
			},
			Output:    run.Output,
			RawOutput: run.RawOutput,
			Truncated: run.Truncated,
			LatencyMS: run.LatencyMS,
			Model:     run.Model,
			Usage:     camelKeys(run.Usage),
			UsedSlots: make([]promptSlotRefJSON, 0, len(run.UsedSlots)),
			Warnings:  promptIssuesToJSON(run.Warnings),
		}
		for _, slot := range run.UsedSlots {
			item.UsedSlots = append(item.UsedSlots, promptSlotRefJSON{
				Key: slot.Key, Lang: slot.Lang, Source: slot.Source, VersionID: slot.VersionID,
			})
		}
		if run.Error != nil {
			item.Error = &promptRunErrorJSON{Code: run.Error.Code, Message: run.Error.Message}
		}
		out.Runs = append(out.Runs, item)
	}
	return out, nil
}

// promptsGenerate 让数据面按一句话指令改写指定提示词；结果不保存。
func (s *Server) promptsGenerate(
	ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any,
) (any, error) {
	if err := s.promptStudioReady(); err != nil {
		return nil, err
	}
	key, err := promptKeyFrom(body)
	if err != nil {
		return nil, err
	}
	lang, err := promptLangFrom(body)
	if err != nil {
		return nil, err
	}
	result, err := s.deps.PromptStudio.Generate(ctx, domain.PromptGenerateInput{
		TenantID:    httpx.String(body["projectId"]),
		Key:         key,
		Lang:        lang,
		Instruction: httpx.String(body["instruction"]),
		BaseBody:    httpx.String(body["baseBody"]),
		Account:     p.Actor(),
	})
	if err != nil {
		return nil, err
	}
	return promptGenerateJSON{
		Body:       result.Body,
		Validation: promptValidationToJSON(result.Validation),
	}, nil
}

// maxPromptRequestBodyBytes 是 prompts/test 与 prompts/validate 的请求体上限
// （spec 第 8 节）。客户端没给 Content-Length 时只能按解码后的内容度量，
// 这里统一用重新编码后的字节数。
const maxPromptRequestBodyBytes = 256 << 10

// prompts/versions 的分页：缺省 20，收敛到 1..100（spec 第 8 节）。
const (
	defaultPromptVersionLimit = 20
	maxPromptVersionLimit     = 100
)

func checkPromptBodySize(body map[string]any) error {
	encoded, err := json.Marshal(body)
	if err != nil {
		return domain.Invalid("prompt", "请求内容无法解析")
	}
	if len(encoded) > maxPromptRequestBodyBytes {
		return domain.TooLarge("prompt", "请求内容太大，请精简后再试")
	}
	return nil
}

func promptKeyFrom(body map[string]any) (string, error) {
	key := httpx.String(body["key"])
	if key == "" {
		return "", domain.Invalid("prompt", "请选择要操作的提示词")
	}
	return key, nil
}

func promptLangFrom(body map[string]any) (string, error) {
	lang := httpx.String(body["lang"])
	if lang == "" {
		return "", domain.Invalid("prompt", "请选择提示词的语言")
	}
	return lang, nil
}

func promptIssuesToJSON(in []domain.PromptIssue) []promptIssueJSON {
	out := make([]promptIssueJSON, 0, len(in))
	for _, item := range in {
		out = append(out, promptIssueJSON{Code: item.Code, Message: item.Message})
	}
	return out
}

func promptValidationToJSON(in domain.PromptValidation) promptValidationJSON {
	out := promptValidationJSON{
		OK:       in.OK,
		Errors:   promptIssuesToJSON(in.Errors),
		Warnings: promptIssuesToJSON(in.Warnings),
	}
	return out
}

// normalizedStrings 保证空集合输出 []，不是 null：前端按数组渲染，null 会让列表崩。
func normalizedStrings(in []string) []string {
	if in == nil {
		return []string{}
	}
	return in
}

// camelKeys 只用于试跑的 usage：上游给的是 snake_case 的扁平表，
// 控制台的其它接口都按 camelCase 展示（示例为 promptTokens/completionTokens）。
// 嵌套值原样不动——它不属于信封字段。
func camelKeys(in map[string]any) map[string]any {
	if in == nil {
		return nil
	}
	out := make(map[string]any, len(in))
	for key, value := range in {
		out[snakeToCamel(key)] = value
	}
	return out
}

func snakeToCamel(key string) string {
	parts := strings.Split(key, "_")
	var b strings.Builder
	b.WriteString(parts[0])
	for _, part := range parts[1:] {
		if part == "" {
			continue
		}
		b.WriteString(strings.ToUpper(part[:1]) + part[1:])
	}
	return b.String()
}

// promptSaveAuditInput 把保存入参换成只剩元数据：正文的 SHA-256 与长度、
// 备注长度、基准版本。正文与备注原文都不进审计。
func promptSaveAuditInput(body map[string]any) map[string]any {
	text := httpx.String(body["body"])
	note := httpx.String(body["note"])
	out := map[string]any{
		"promptKey":    httpx.String(body["key"]),
		"lang":         httpx.String(body["lang"]),
		"noteLength":   utf8.RuneCountInString(note),
		"promptLength": utf8.RuneCountInString(text),
	}
	if text != "" {
		sum := sha256.Sum256([]byte(text))
		out["promptSha256"] = hex.EncodeToString(sum[:])
	}
	if base := httpx.Int(body["baseVersionId"], 0); base != 0 {
		out["baseVersionId"] = base
	}
	if restore := httpx.Int(body["restoreFromVersionId"], 0); restore != 0 {
		out["restoreFromVersionId"] = restore
	}
	return out
}

// promptResetAuditInput 只留槽位标识：还原不涉及正文。
func promptResetAuditInput(body map[string]any) map[string]any {
	return map[string]any{
		"promptKey": httpx.String(body["key"]),
		"lang":      httpx.String(body["lang"]),
	}
}

// promptSaveAuditAnnotate 在成功后补上新版本号；审计要能回答“写成了第几版”。
func promptSaveAuditAnnotate(input map[string]any, payload any) {
	if saved, ok := payload.(promptSaveJSON); ok {
		input["versionId"] = saved.VersionID
	}
}

// promptResetAuditAnnotate 补上实际被还原的槽位版本号。
func promptResetAuditAnnotate(input map[string]any, payload any) {
	reset, ok := payload.(promptResetJSON)
	if !ok {
		return
	}
	ids := make([]int64, 0, len(reset.Reset))
	for _, item := range reset.Reset {
		ids = append(ids, item.VersionID)
	}
	input["versionIds"] = ids
}
