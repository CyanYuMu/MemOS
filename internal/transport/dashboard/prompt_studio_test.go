package dashboard

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/audit"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/service/prompt"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// fakePromptGateway 给提示词工作台的八条路由提供确定性数据；
// 以 `err.` 开头的 key 用来注入各类上游错误，覆盖错误映射。
type fakePromptGateway struct {
	mu      sync.Mutex
	saved   []domain.PromptSaveInput
	reset   []domain.PromptResetInput
	tested  []domain.PromptTestInput
	gen     []domain.PromptGenerateInput
	queries []domain.PromptVersionsQuery
}

const (
	fakePromptZhBody = "把对话整理成记忆 ${conversation}"
	fakePromptEnBody = "Turn a conversation into memories ${conversation}"
)

func promptGatewayErr(key string) error {
	switch key {
	case "err.invalid":
		return &domain.InvalidPromptError{
			Message: "提示词没有通过校验，请按错误列表修改后再保存。",
			Errors: []domain.PromptIssue{{
				Code: "missing_placeholder", Message: "缺少必填占位符 ${conversation}。请把它放回正文。",
			}},
		}
	case "err.notfound":
		return domain.NotFound("prompt", "这条提示词不存在，请刷新页面后重试")
	case "err.conflict":
		return &domain.VersionConflictError{CurrentVersionID: 9, CurrentVersionNo: 5}
	case "err.conflict-default":
		// 别人把槽位还原成默认后，冲突里的 current 就是 0；0 是合法值，不能丢。
		return &domain.VersionConflictError{CurrentVersionID: 0, CurrentVersionNo: 0}
	case "err.version":
		return domain.NotFound("prompt", "这个历史版本不存在，请刷新版本历史")
	case "err.toolarge":
		return domain.TooLarge("prompt", "内容太长，请精简后再试")
	case "err.busy":
		return &domain.BusyError{Wait: 12 * time.Second, Message: "当前请求太多，请稍后重试"}
	case "err.modelfail":
		return domain.Wrap(domain.ErrModelFailed, "prompt", "模型调用失败，请稍后重试", nil)
	case "err.timeout":
		return domain.Wrap(domain.ErrModelTimeout, "prompt", "模型响应超时，请稍后重试", nil)
	default:
		return nil
	}
}

func (f *fakePromptGateway) List(_ context.Context, tenantID string) (domain.PromptCatalog, error) {
	if tenantID == "cube-fail" {
		return domain.PromptCatalog{}, domain.Wrap(domain.ErrUpstreamUnavailable, "upstream", "暂时连不上记忆服务，请稍后重试。", nil)
	}
	return domain.PromptCatalog{Prompts: []domain.PromptItem{
		{
			Key: "extract.chat", Group: "extract",
			Title:     map[string]string{"zh": "对话抽取", "en": "Conversation extraction"},
			Summary:   map[string]string{"zh": "把对话整理成记忆"},
			Languages: []string{"zh", "en"}, Editable: true,
			State: map[string]domain.PromptSlotState{
				"zh": {
					Source: "project", VersionID: 3, VersionNo: 2,
					UpdatedAt: new("2026-10-28T10:00:00Z"), Author: new("alice"),
				},
				"en": {Source: "default"},
			},
		},
		{
			Key: "dream.motive", Group: "dream",
			Title:     map[string]string{"zh": "动机形成"},
			Languages: []string{"en"}, Editable: true,
			State: map[string]domain.PromptSlotState{"en": {Source: "default"}},
		},
	}}, nil
}

func (f *fakePromptGateway) Detail(_ context.Context, _, key string) (domain.PromptDetail, error) {
	if err := promptGatewayErr(key); err != nil {
		return domain.PromptDetail{}, err
	}
	locked := "\n只输出 JSON。"
	return domain.PromptDetail{
		Key: "extract.chat", Group: "extract",
		Title:     map[string]string{"zh": "对话抽取"},
		Summary:   map[string]string{"zh": "把对话整理成记忆"},
		Languages: []string{"zh", "en"},
		Syntax:    "dollar",
		Placeholders: []domain.PromptPlaceholder{
			{Name: "conversation", Required: true, Description: map[string]string{"zh": "对话正文"}},
		},
		LockedSuffix: &locked,
		InputKind:    "conversation",
		InputSchema:  json.RawMessage(`{"type":"object","properties":{"messages":{"type":"array"}}}`),
		OutputContract: domain.PromptOutputContract{
			Format:       "json",
			RequiredKeys: []string{"memory list"},
			ItemKeys:     []string{"key", "memory_type"},
			Description:  map[string]string{"zh": "记忆列表"},
		},
		SampleInputs: []domain.PromptSample{{
			ID: "s1", Lang: "zh", Title: map[string]string{"zh": "两轮对话"},
			Input: json.RawMessage(`{"messages":[{"role":"user","content":"我喜欢深色模式"}]}`),
		}},
		Editable: true,
		Defaults: map[string]string{"zh": fakePromptZhBody, "en": fakePromptEnBody},
		Effective: map[string]domain.PromptEffective{
			"zh": {
				Body: "自定义 ${conversation}", Source: "project", VersionID: 3, VersionNo: 2,
				UpdatedAt: new("2026-10-28T10:00:00Z"), Author: new("alice"),
			},
			"en": {Body: fakePromptEnBody, Source: "default"},
		},
	}, nil
}

func (f *fakePromptGateway) Versions(
	_ context.Context, in domain.PromptVersionsQuery,
) (domain.PromptVersionPage, error) {
	f.mu.Lock()
	f.queries = append(f.queries, in)
	f.mu.Unlock()
	if err := promptGatewayErr(in.Key); err != nil {
		return domain.PromptVersionPage{}, err
	}
	parent := int64(1)
	return domain.PromptVersionPage{
		Versions: []domain.PromptVersion{
			{
				VersionID: 3, VersionNo: 2, Lang: "zh", Kind: "save", Body: "自定义 ${conversation}",
				Note: "调标签", Author: "alice", CreatedAt: "2026-10-28T10:00:00Z", ParentVersionID: &parent,
			},
			{
				VersionID: 1, Lang: "zh", Kind: "migrated", Body: fakePromptZhBody,
				Author: "migration", CreatedAt: "2026-10-01T00:00:00Z",
			},
		},
		HasMore: true,
	}, nil
}

func (f *fakePromptGateway) Validate(
	_ context.Context, in domain.PromptValidateInput,
) (domain.PromptValidation, error) {
	if err := promptGatewayErr(in.Key); err != nil {
		return domain.PromptValidation{}, err
	}
	if len([]rune(in.Body)) > 20000 {
		return domain.PromptValidation{
			OK: false,
			Errors: []domain.PromptIssue{{
				Code: "too_long", Message: "正文最多 20000 字，请精简后再保存。",
			}},
			Warnings: []domain.PromptIssue{},
		}, nil
	}
	return domain.PromptValidation{
		OK:       true,
		Errors:   []domain.PromptIssue{},
		Warnings: []domain.PromptIssue{{Code: "missing_output_hint", Message: "正文里没有提到输出结构，模型可能返回非 JSON"}},
	}, nil
}

func (f *fakePromptGateway) Save(
	_ context.Context, in domain.PromptSaveInput,
) (domain.PromptSaveResult, error) {
	f.mu.Lock()
	f.saved = append(f.saved, in)
	f.mu.Unlock()
	if err := promptGatewayErr(in.Key); err != nil {
		return domain.PromptSaveResult{}, err
	}
	if in.BaseVersionID != 0 && in.BaseVersionID != 3 {
		return domain.PromptSaveResult{}, &domain.VersionConflictError{CurrentVersionID: 3, CurrentVersionNo: 2}
	}
	body := in.Body
	if in.RestoreFromVersionID > 0 {
		body = "回滚到 " + fakePromptZhBody
	}
	return domain.PromptSaveResult{
		VersionID: 4,
		VersionNo: 3,
		CreatedAt: "2026-10-29T00:00:00Z",
		Effective: domain.PromptEffective{
			Body: body, Source: "project", VersionID: 4, VersionNo: 3,
			UpdatedAt: new("2026-10-29T00:00:00Z"), Author: new(in.Author),
		},
	}, nil
}

func (f *fakePromptGateway) Reset(
	_ context.Context, in domain.PromptResetInput,
) (domain.PromptResetResult, error) {
	f.mu.Lock()
	f.reset = append(f.reset, in)
	f.mu.Unlock()
	if err := promptGatewayErr(in.Key); err != nil {
		return domain.PromptResetResult{}, err
	}
	if in.Key == "extract.chat" && in.Lang == "en" {
		// 已经是默认的槽位不追加重置行。
		return domain.PromptResetResult{Restored: []domain.PromptResetItem{}}, nil
	}
	return domain.PromptResetResult{Restored: []domain.PromptResetItem{
		{Key: "extract.chat", Lang: "zh", VersionID: 5, VersionNo: 4},
	}}, nil
}

func (f *fakePromptGateway) Test(
	_ context.Context, in domain.PromptTestInput,
) (domain.PromptTestResult, error) {
	f.mu.Lock()
	f.tested = append(f.tested, in)
	f.mu.Unlock()
	if err := promptGatewayErr(in.Key); err != nil {
		return domain.PromptTestResult{}, err
	}
	draft := domain.PromptTestRun{
		Label: "draft", OK: true, ParseOK: true,
		Contract:  domain.PromptContractCheck{OK: true, Missing: []string{}},
		Output:    json.RawMessage(`{"memory list":[{"key":"k1","memory_type":"UserMemory"}],"summary":"s"}`),
		RawOutput: "{\"memory list\":[...]}",
		LatencyMS: 3120, Model: "DeepSeek-V4.1-Flash",
		Usage: map[string]any{"prompt_tokens": float64(812), "completion_tokens": float64(240), "calls": float64(1)},
		UsedSlots: []domain.PromptSlotRef{
			{Key: "extract.chat", Lang: "zh", Source: "draft", VersionID: 3},
		},
		Warnings: []domain.PromptIssue{
			{Code: "draft_not_used", Message: "这段输入按 en 处理，草稿没有参与运行"},
		},
	}
	runs := []domain.PromptTestRun{draft}
	if in.CompareDefault {
		failed := domain.PromptTestRun{
			Label: "default", OK: false,
			Error: &domain.PromptRunError{Code: "llm_timeout", Message: "模型响应超时，请稍后重试"},
		}
		runs = append(runs, failed)
	}
	return domain.PromptTestResult{Runs: runs}, nil
}

func (f *fakePromptGateway) Generate(
	_ context.Context, in domain.PromptGenerateInput,
) (domain.PromptGenerateResult, error) {
	f.mu.Lock()
	f.gen = append(f.gen, in)
	f.mu.Unlock()
	if err := promptGatewayErr(in.Key); err != nil {
		return domain.PromptGenerateResult{}, err
	}
	return domain.PromptGenerateResult{
		Body:       "改写(" + in.Instruction + ") ${conversation}",
		Validation: domain.PromptValidation{OK: true, Errors: []domain.PromptIssue{}, Warnings: []domain.PromptIssue{}},
	}, nil
}

func decodeData(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var env struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode: %v body=%s", err, rec.Body.String())
	}
	return env.Data
}

func TestPromptsListAndDetail(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)

	list := call(t, srv, http.MethodPost, "/api/dashboard/prompts/list", `{"projectId":"cube-a"}`)
	if list.Code != http.StatusOK {
		t.Fatalf("list: %d %s", list.Code, list.Body.String())
	}
	prompts, _ := decodeData(t, list)["prompts"].([]any)
	if len(prompts) != 2 {
		t.Fatalf("prompts=%d body=%s", len(prompts), list.Body.String())
	}
	first, _ := prompts[0].(map[string]any)
	if first["key"] != "extract.chat" || first["group"] != "extract" {
		t.Fatalf("第一项不符: %v", first)
	}
	state, _ := first["state"].(map[string]any)
	zh, _ := state["zh"].(map[string]any)
	if zh["source"] != "project" || zh["versionId"] != float64(3) || zh["versionNo"] != float64(2) || zh["updatedAt"] == "" {
		t.Fatalf("槽位状态未按 camelCase 输出: %v", zh)
	}
	if en, _ := state["en"].(map[string]any); en["updatedAt"] != nil || en["author"] != nil {
		t.Fatalf("默认槽位的 updatedAt/author 必须是 null: %v", en)
	}
	// 列表形态不携带正文。
	if strings.Contains(list.Body.String(), "conversation}") {
		t.Fatalf("list 不应带提示词正文: %s", list.Body.String())
	}

	detail := call(t, srv, http.MethodPost, "/api/dashboard/prompts/detail", `{"projectId":"cube-a","key":"extract.chat"}`)
	if detail.Code != http.StatusOK {
		t.Fatalf("detail: %d %s", detail.Code, detail.Body.String())
	}
	data := decodeData(t, detail)
	for _, want := range []string{
		`"lockedSuffix"`, `"inputKind"`, `"requiredKeys"`, `"sampleInputs"`, `"defaults"`, `"effective"`,
		`"versionId"`, `"versionNo"`, `"lang":"zh"`,
	} {
		if !strings.Contains(detail.Body.String(), want) {
			t.Fatalf("detail 缺少 %s: %s", want, detail.Body.String())
		}
	}
	// 透传：inputSchema 与 sampleInputs[].input 的键名保持 snake_case 原样。
	schema, _ := data["inputSchema"].(map[string]any)
	if _, ok := schema["properties"]; !ok {
		t.Fatalf("inputSchema 应原样透传: %v", data["inputSchema"])
	}
	samples, _ := data["sampleInputs"].([]any)
	sample, _ := samples[0].(map[string]any)
	input, _ := sample["input"].(map[string]any)
	if _, ok := input["messages"]; !ok {
		t.Fatalf("sampleInputs[].input 应原样透传: %v", sample["input"])
	}
	defaults, _ := data["defaults"].(map[string]any)
	if defaults["zh"] != fakePromptZhBody {
		t.Fatalf("defaults.zh = %v", defaults["zh"])
	}
	effective, _ := data["effective"].(map[string]any)
	zhEff, _ := effective["zh"].(map[string]any)
	if zhEff["body"] == "" || zhEff["versionId"] != float64(3) || zhEff["versionNo"] != float64(2) {
		t.Fatalf("effective.zh = %v", zhEff)
	}
	if enEff, _ := effective["en"].(map[string]any); enEff["updatedAt"] != nil || enEff["author"] != nil {
		t.Fatalf("默认槽位的 updatedAt/author 必须是 null: %v", enEff)
	}
}

func TestPromptsVersions(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/versions",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","limit":2,"beforeVersionId":10}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("versions: %d %s", rec.Code, rec.Body.String())
	}
	for _, want := range []string{`"versionId":3`, `"versionNo":2`, `"hasMore":true`, `"parentVersionId":1`, `"createdAt"`} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Fatalf("versions 缺少 %s: %s", want, rec.Body.String())
		}
	}
	fake, ok := srv.deps.PromptStudio.(*fakePromptGateway)
	if !ok {
		t.Fatal("测试服务器应装配 fakePromptGateway")
	}
	if len(fake.queries) != 1 || fake.queries[0].Limit != 2 || fake.queries[0].BeforeVersionID != 10 {
		t.Fatalf("版本查询入参不符: %+v", fake.queries)
	}
}

func TestPromptsValidate(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/validate",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("validate: %d %s", rec.Code, rec.Body.String())
	}
	var env struct {
		Data struct {
			OK       bool  `json:"ok"`
			Errors   []any `json:"errors"`
			Warnings []struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"warnings"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !env.Data.OK || env.Data.Errors == nil || len(env.Data.Warnings) != 1 || env.Data.Warnings[0].Code == "" {
		t.Fatalf("validate 形状不符: %+v", env.Data)
	}
}

func TestPromptsSaveAndReset(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)

	save := call(t, srv, http.MethodPost, "/api/dashboard/prompts/save",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"新规则 ${conversation}","note":"调教","author":"冒名","baseVersionId":3}`)
	if save.Code != http.StatusOK {
		t.Fatalf("save: %d %s", save.Code, save.Body.String())
	}
	for _, want := range []string{`"versionId":4`, `"versionNo":3`, `"effective"`, `"source":"project"`} {
		if !strings.Contains(save.Body.String(), want) {
			t.Fatalf("save 缺少 %s: %s", want, save.Body.String())
		}
	}

	// author 只取登录账号，客户端传的值被忽略。
	fake, ok := srv.deps.PromptStudio.(*fakePromptGateway)
	if !ok {
		t.Fatal("测试服务器应装配 fakePromptGateway")
	}
	if len(fake.saved) != 1 || fake.saved[0].Author != domain.AdminTokenUsername || fake.saved[0].BaseVersionID != 3 {
		t.Fatalf("保存入参不符: %+v", fake.saved)
	}

	conflict := call(t, srv, http.MethodPost, "/api/dashboard/prompts/save",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x","baseVersionId":1}`)
	if conflict.Code != http.StatusConflict {
		t.Fatalf("版本落后应 409: %d %s", conflict.Code, conflict.Body.String())
	}
	if !strings.Contains(conflict.Body.String(), `"currentVersionId":3`) ||
		!strings.Contains(conflict.Body.String(), `"currentVersionNo":2`) {
		t.Fatalf("冲突应带当前版本号与展示序号: %s", conflict.Body.String())
	}

	reset := call(t, srv, http.MethodPost, "/api/dashboard/prompts/reset",
		`{"projectId":"cube-a","key":"extract.chat"}`)
	// 替身给的 versionNo 是 4：只要传输层漏映射，这里就会是 0。
	if reset.Code != http.StatusOK || !strings.Contains(reset.Body.String(), `"reset":[{"key":"extract.chat"`) ||
		!strings.Contains(reset.Body.String(), `"versionId":5`) ||
		!strings.Contains(reset.Body.String(), `"versionNo":4`) {
		t.Fatalf("reset: %d %s", reset.Code, reset.Body.String())
	}

	// 已经是默认的槽位还原时返回空数组，不是 404。
	empty := call(t, srv, http.MethodPost, "/api/dashboard/prompts/reset",
		`{"projectId":"cube-a","key":"extract.chat","lang":"en"}`)
	if empty.Code != http.StatusOK || !strings.Contains(empty.Body.String(), `"reset":[]`) {
		t.Fatalf("空还原应返回空数组: %d %s", empty.Code, empty.Body.String())
	}
}

func TestPromptsTestRoute(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/test",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"草稿 ${conversation}",
		  "input":{"messages":[{"role":"user","content":"hi"}]},"compareDefault":true}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("test: %d %s", rec.Code, rec.Body.String())
	}
	for _, want := range []string{
		`"label":"draft"`, `"parseOk":true`, `"fallbackTriggered":false`, `"latencyMs":3120`,
		`"promptTokens":812`, `"completionTokens":240`, `"calls":1`, `"memory_type":"UserMemory"`,
		`"memory list"`, `"code":"llm_timeout"`, `"usedSlots":[{"key":"extract.chat","lang":"zh","source":"draft","versionId":3}]`,
		`"warnings":[{"code":"draft_not_used","message":"这段输入按 en 处理，草稿没有参与运行"}]`,
	} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Fatalf("test 响应缺少 %s: %s", want, rec.Body.String())
		}
	}
	// usage 之外的键保持原样；raw_output 转成 rawOutput。
	if strings.Contains(rec.Body.String(), `"prompt_tokens"`) || strings.Contains(rec.Body.String(), `"raw_output"`) {
		t.Fatalf("keys 未转换: %s", rec.Body.String())
	}
}

func TestPromptsGenerate(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/generate",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","instruction":"只抽安全事件","baseBody":"旧"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("generate: %d %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"body":"改写(只抽安全事件) ${conversation}"`) ||
		!strings.Contains(rec.Body.String(), `"validation"`) {
		t.Fatalf("generate 响应不符: %s", rec.Body.String())
	}
}

// 上游七类错误码到控制面状态码、信封 code 与 data 的映射。
func TestPromptsErrorMapping(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name       string
		path       string
		body       string
		wantStatus int
		wantCode   string
		wantData   string
		wantHeader string
	}{
		{
			name: "缺少占位符", path: "detail", body: `{"projectId":"cube-a","key":"err.invalid"}`,
			wantStatus: http.StatusBadRequest, wantCode: `"code":40000`,
			wantData: `"errors":[{"code":"missing_placeholder"`,
		},
		{
			name: "未知提示词", path: "detail", body: `{"projectId":"cube-a","key":"err.notfound"}`,
			wantStatus: http.StatusNotFound, wantCode: `"code":40001`,
		},
		{
			name: "版本冲突", path: "save",
			body:       `{"projectId":"cube-a","key":"err.conflict","lang":"zh","body":"x"}`,
			wantStatus: http.StatusConflict, wantCode: `"code":40900`, wantData: `"currentVersionId":9`,
		},
		{
			name: "内容超长", path: "save",
			body:       `{"projectId":"cube-a","key":"err.toolarge","lang":"zh","body":"x"}`,
			wantStatus: http.StatusRequestEntityTooLarge, wantCode: `"code":41300`,
		},
		{
			// golden 的 generate [413] 条目用的是这条请求：这里把它的信封码钉到具体值，
			// golden 只记类型，值靠这条断言。
			name: "上游 413", path: "generate",
			body:       `{"projectId":"cube-a","key":"err.toolarge","lang":"zh","instruction":"x"}`,
			wantStatus: http.StatusRequestEntityTooLarge, wantCode: `"code":41300`,
		},
		{
			name: "历史版本不存在", path: "save",
			body:       `{"projectId":"cube-a","key":"err.version","lang":"zh","restoreFromVersionId":99}`,
			wantStatus: http.StatusNotFound, wantCode: `"code":40001`,
		},
		{
			name: "上游限流", path: "test",
			body:       `{"projectId":"cube-a","key":"err.busy","lang":"zh","input":{}}`,
			wantStatus: http.StatusTooManyRequests, wantCode: `"code":42900`,
			wantData: `"retryAfterSec":12`, wantHeader: "12",
		},
		{
			name: "模型失败", path: "generate",
			body:       `{"projectId":"cube-a","key":"err.modelfail","lang":"zh","instruction":"x"}`,
			wantStatus: http.StatusBadGateway, wantCode: `"code":50200`,
		},
		{
			name: "模型超时", path: "generate",
			body:       `{"projectId":"cube-a","key":"err.timeout","lang":"zh","instruction":"x"}`,
			wantStatus: http.StatusGatewayTimeout, wantCode: `"code":50400`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			srv := newTestServer(t)
			rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/"+tc.path, tc.body)
			if rec.Code != tc.wantStatus {
				t.Fatalf("status=%d want=%d body=%s", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if !strings.Contains(rec.Body.String(), tc.wantCode) {
				t.Fatalf("code 不符: %s", rec.Body.String())
			}
			if tc.wantData != "" && !strings.Contains(rec.Body.String(), tc.wantData) {
				t.Fatalf("data 不符: %s", rec.Body.String())
			}
			if tc.wantHeader != "" && rec.Header().Get("Retry-After") != tc.wantHeader {
				t.Fatalf("Retry-After=%q want=%q", rec.Header().Get("Retry-After"), tc.wantHeader)
			}
			// 文案只写发生了什么与怎么改，不暴露上游地址与内部字段名。
			if strings.Contains(rec.Body.String(), "upstream status") || strings.Contains(rec.Body.String(), "version_conflict") {
				t.Fatalf("文案泄漏内部细节: %s", rec.Body.String())
			}
		})
	}
}

func TestPromptsUpstreamUnavailable(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/list", `{"projectId":"cube-fail"}`)
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), `"code":50300`) {
		t.Fatalf("上游不可用应 503: %d %s", rec.Code, rec.Body.String())
	}
}

// viewer 能读不能写：三条读路由放行，五条写路由 403。
func TestPromptsViewerPermissions(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	srv.deps.Accounts = viewerAccounts{}

	cases := []struct {
		path string
		body string
		want int
	}{
		{"list", `{"projectId":"cube-a"}`, http.StatusOK},
		{"detail", `{"projectId":"cube-a","key":"extract.chat"}`, http.StatusOK},
		{"versions", `{"projectId":"cube-a","key":"extract.chat","lang":"zh"}`, http.StatusOK},
		{"validate", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x"}`, http.StatusForbidden},
		{"save", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x"}`, http.StatusForbidden},
		{"reset", `{"projectId":"cube-a","key":"extract.chat"}`, http.StatusForbidden},
		{"test", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","input":{}}`, http.StatusForbidden},
		{"generate", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","instruction":"x"}`, http.StatusForbidden},
	}
	for _, tc := range cases {
		t.Run(tc.path, func(t *testing.T) {
			rec := callAs(t, srv, viewerToken, http.MethodPost, "/api/dashboard/prompts/"+tc.path, tc.body)
			if rec.Code != tc.want {
				t.Fatalf("%s: status=%d want=%d body=%s", tc.path, rec.Code, tc.want, rec.Body.String())
			}
		})
	}
}

// 写路由必须显式带 projectId：缺省时不回落到会话当前项目。
func TestPromptsWriteRequiresProjectID(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	for _, path := range []string{"save", "reset", "validate", "test", "generate"} {
		rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/"+path, `{"key":"extract.chat","lang":"zh"}`)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s 缺 projectId 应 400: %d %s", path, rec.Code, rec.Body.String())
		}
	}
}

// 审计入参只留元数据：没有正文、没有备注原文，SHA-256 与长度可从正文复算。
func TestPromptSaveAuditInputKeepsMetadataOnly(t *testing.T) {
	t.Parallel()

	body := "秘密正文 ${conversation}"
	sum := sha256.Sum256([]byte(body))
	got := promptSaveAuditInput(map[string]any{
		"projectId":     "cube-a",
		"key":           "extract.chat",
		"lang":          "zh",
		"body":          body,
		"note":          "这段备注不该进审计",
		"baseVersionId": float64(3),
		"restoreFrom":   "ignored",
		"restoreFromId": float64(0),
	})
	if got["promptSha256"] != hex.EncodeToString(sum[:]) {
		t.Fatalf("sha256 不符: %v", got)
	}
	if got["promptLength"] != len([]rune(body)) || got["noteLength"] != len([]rune("这段备注不该进审计")) {
		t.Fatalf("长度不符: %v", got)
	}
	raw, _ := json.Marshal(got)
	for _, forbidden := range []string{"秘密正文", "这段备注不该进审计", `"body"`, `"note"`, "projectId"} {
		if strings.Contains(string(raw), forbidden) {
			t.Fatalf("审计入参不应包含 %s: %s", forbidden, raw)
		}
	}
	if got["baseVersionId"] != 3 {
		t.Fatalf("baseVersionId 应保留: %v", got)
	}
}

// 无效的 key/lang 在控制面就拒绝，不打到上游。
func TestPromptsRequiredFields(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	cases := []struct{ path, body string }{
		{"detail", `{"projectId":"cube-a"}`},
		{"versions", `{"projectId":"cube-a","lang":"zh"}`},
		{"save", `{"projectId":"cube-a","key":"extract.chat"}`},
		{"generate", `{"projectId":"cube-a","lang":"zh"}`},
		{"test", `{"projectId":"cube-a","lang":"zh"}`},
	}
	for _, tc := range cases {
		rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/"+tc.path, tc.body)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: %d %s", tc.path, rec.Code, rec.Body.String())
		}
	}
}

// 256 KiB 的试跑请求体在控制面就被拒绝，不打到上游。
func TestPromptsTestBodyLimit(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	fake, _ := srv.deps.PromptStudio.(*fakePromptGateway)
	huge := strings.Repeat("a", 300<<10)
	body := `{"projectId":"cube-a","key":"extract.chat","lang":"zh","input":{"text":"` + huge + `"}}`
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/test", body)
	if rec.Code != http.StatusRequestEntityTooLarge || !strings.Contains(rec.Body.String(), `"code":41300`) {
		t.Fatalf("超大请求体应 413: %d %s", rec.Code, rec.Body.String())
	}
	if len(fake.tested) != 0 {
		t.Fatalf("超限请求不应打到上游: %d", len(fake.tested))
	}
}

// 控制面自己的令牌桶与上游 busy 走同一条 429 形状：retryAfterSec + Retry-After。
func TestPromptsLocalThrottleShape(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	now := time.Date(2026, 10, 29, 0, 0, 0, 0, time.UTC)
	srv.deps.PromptStudio = prompt.New(&fakePromptGateway{}, prompt.Options{
		TestBurst: 1, TestRefillPerMin: 6,
		Now: func() time.Time { return now },
	})

	body := `{"projectId":"cube-a","key":"extract.chat","lang":"zh","input":{}}`
	if first := call(t, srv, http.MethodPost, "/api/dashboard/prompts/test", body); first.Code != http.StatusOK {
		t.Fatalf("首个试跑应放行: %d %s", first.Code, first.Body.String())
	}
	second := call(t, srv, http.MethodPost, "/api/dashboard/prompts/test", body)
	if second.Code != http.StatusTooManyRequests || !strings.Contains(second.Body.String(), `"code":42900`) {
		t.Fatalf("超出突发应 429: %d %s", second.Code, second.Body.String())
	}
	if !strings.Contains(second.Body.String(), `"retryAfterSec":10`) {
		t.Fatalf("应给出补满一个令牌的等待秒数: %s", second.Body.String())
	}
	if got := second.Header().Get("Retry-After"); got != "10" {
		t.Fatalf("Retry-After=%q", got)
	}
	// 文案面向用户，不暴露令牌桶参数。
	if strings.Contains(second.Body.String(), "burst") || strings.Contains(second.Body.String(), "6") {
		t.Fatalf("文案泄漏限流内部参数: %s", second.Body.String())
	}
}

// captureAuditStore 只收集审计记录，用于断言保存/还原的审计里没有正文。
type captureAuditStore struct {
	mu      sync.Mutex
	records []domain.AuditRecord
}

func (s *captureAuditStore) InsertAudit(_ context.Context, r domain.AuditRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records = append(s.records, r)
	return nil
}

func (s *captureAuditStore) InsertAudits(_ context.Context, records []domain.AuditRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records = append(s.records, records...)
	return nil
}

func (s *captureAuditStore) ListAudit(context.Context, service.AuditFilter) ([]domain.AuditRecord, int, error) {
	return nil, 0, nil
}

func (s *captureAuditStore) GetAudit(context.Context, string) (domain.AuditRecord, error) {
	return domain.AuditRecord{}, domain.NotFound("audit", "审计不存在")
}

func (s *captureAuditStore) AuditHourCounts(context.Context, service.AuditFilter) ([]domain.AuditHourCount, error) {
	return nil, nil
}

func (s *captureAuditStore) PurgeAuditBefore(context.Context, time.Time) (int64, error) {
	return 0, nil
}

func TestPromptSaveAuditHasNoBody(t *testing.T) {
	store := &captureAuditStore{}
	var logs lockedBuffer
	recorder, err := audit.New(store, slog.New(slog.NewTextHandler(&logs, nil)), audit.Options{
		QueueSize: 8, FlushInterval: 5 * time.Millisecond, PurgeInterval: time.Hour,
	})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = recorder.Run(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})

	srv := newTestServer(t)
	srv.deps.Audit = recorder
	srv.deps.Logger = slog.New(slog.NewTextHandler(&logs, nil))

	secret := "秘密正文 ${conversation}"
	body := `{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"` + secret + `","note":"别记我"}`
	if rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/save", body); rec.Code != http.StatusOK {
		t.Fatalf("save: %d %s", rec.Code, rec.Body.String())
	}

	record := waitAudit(t, store, "dashboard.prompts.save")
	if record.Input["promptKey"] != "extract.chat" || record.Input["versionId"] != int64(4) {
		t.Fatalf("审计应记元数据与版本号: %v", record.Input)
	}
	sum := sha256.Sum256([]byte(secret))
	if record.Input["promptSha256"] != hex.EncodeToString(sum[:]) {
		t.Fatalf("审计的 sha256 不符: %v", record.Input)
	}
	raw, _ := json.Marshal(record.Input)
	if strings.Contains(string(raw), "秘密正文") || strings.Contains(string(raw), "别记我") {
		t.Fatalf("审计入参泄漏正文/备注: %s", raw)
	}
	if strings.Contains(logs.String(), "秘密正文") || strings.Contains(logs.String(), "别记我") {
		t.Fatalf("日志泄漏正文/备注: %s", logs.String())
	}

	// 还原同样只记槽位与版本号。
	if rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/reset", `{"projectId":"cube-a","key":"extract.chat"}`); rec.Code != http.StatusOK {
		t.Fatalf("reset: %d %s", rec.Code, rec.Body.String())
	}
	reset := waitAudit(t, store, "dashboard.prompts.reset")
	if reset.Input["promptKey"] != "extract.chat" || reset.Input["versionIds"] == nil {
		t.Fatalf("还原审计应记槽位与版本号: %v", reset.Input)
	}
}

func waitAudit(t *testing.T, store *captureAuditStore, tool string) domain.AuditRecord {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		store.mu.Lock()
		for i := range store.records {
			record := &store.records[i]
			if record.Tool == tool && record.OK {
				store.mu.Unlock()
				return *record
			}
		}
		store.mu.Unlock()
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("没有等到 %s 的审计记录", tool)
	return domain.AuditRecord{}
}

type lockedBuffer struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// validate 是“始终 200”的接口：超长正文由数据面给 too_long，控制面不能本地 413。
func TestPromptsValidateLongBodyStays200(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	srv.deps.PromptStudio = prompt.New(&fakePromptGateway{}, prompt.Options{})
	long := strings.Repeat("好", 21000)
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/validate",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"`+long+`"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("validate 必须 200: %d %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"ok":false`) || !strings.Contains(rec.Body.String(), `"too_long"`) {
		t.Fatalf("应带 too_long: %s", rec.Body.String())
	}
}

// 生成请求超过上限是 413，信封码用新增的 41300。
func TestPromptsGenerateTooLarge(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	srv.deps.PromptStudio = prompt.New(&fakePromptGateway{}, prompt.Options{})
	body := `{"projectId":"cube-a","key":"extract.chat","lang":"zh","instruction":"` +
		strings.Repeat("改", 2001) + `"}`
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/generate", body)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("应 413: %d %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"code":41300`) {
		t.Fatalf("413 的信封码应为 41300: %s", rec.Body.String())
	}
}

// versions 的 limit 收敛到 1..100，缺省 20。
func TestPromptsVersionsLimitClamped(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	cases := []struct {
		body string
		want int
	}{
		{`{"projectId":"cube-a","key":"extract.chat","lang":"zh"}`, 20},
		{`{"projectId":"cube-a","key":"extract.chat","lang":"zh","limit":0}`, 20},
		{`{"projectId":"cube-a","key":"extract.chat","lang":"zh","limit":-5}`, 20},
		{`{"projectId":"cube-a","key":"extract.chat","lang":"zh","limit":1}`, 1},
		{`{"projectId":"cube-a","key":"extract.chat","lang":"zh","limit":100}`, 100},
		{`{"projectId":"cube-a","key":"extract.chat","lang":"zh","limit":101}`, 100},
	}
	for _, tc := range cases {
		if rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/versions", tc.body); rec.Code != http.StatusOK {
			t.Fatalf("versions: %d %s", rec.Code, rec.Body.String())
		}
		fake, _ := srv.deps.PromptStudio.(*fakePromptGateway)
		got := fake.queries[len(fake.queries)-1].Limit
		if got != tc.want {
			t.Fatalf("limit=%d want=%d (%s)", got, tc.want, tc.body)
		}
	}
}

// 试跑与 AI 改写的输入/输出不进审计也不进日志：这两条路由根本不写审计。
func TestPromptTestAndGenerateLeaveNoTrace(t *testing.T) {
	store := &captureAuditStore{}
	var logs lockedBuffer
	recorder, err := audit.New(store, slog.New(slog.NewTextHandler(&logs, nil)), audit.Options{
		QueueSize: 8, FlushInterval: 5 * time.Millisecond, PurgeInterval: time.Hour,
	})
	if err != nil {
		t.Fatalf("audit.New: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = recorder.Run(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})

	srv := newTestServer(t)
	srv.deps.Audit = recorder
	srv.deps.Logger = slog.New(slog.NewTextHandler(&logs, nil))

	const marker = "MARKER-BE-SECRET-9f3a"
	if rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/test",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"草稿 `+marker+` ${conversation}",`+
			`"input":{"messages":[{"role":"user","content":"`+marker+`"}]}}`); rec.Code != http.StatusOK {
		t.Fatalf("test: %d %s", rec.Code, rec.Body.String())
	}
	if rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/generate",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","instruction":"`+marker+`",`+
			`"baseBody":"原文 `+marker+` ${conversation}"}`); rec.Code != http.StatusOK {
		t.Fatalf("generate: %d %s", rec.Code, rec.Body.String())
	}

	// 审计是异步的，给它足够长的窗口再断言“确实没有”。
	time.Sleep(100 * time.Millisecond)
	store.mu.Lock()
	records := append([]domain.AuditRecord(nil), store.records...)
	store.mu.Unlock()
	for i := range records {
		if records[i].Tool == "dashboard.prompts.test" || records[i].Tool == "dashboard.prompts.generate" {
			t.Fatalf("试跑与改写不应写审计: %+v", records[i].Tool)
		}
	}
	raw, _ := json.Marshal(records)
	if strings.Contains(string(raw), marker) || strings.Contains(logs.String(), marker) {
		t.Fatalf("审计或日志泄漏了试跑/改写内容")
	}
}

// 审计只记元数据：保存与还原的入参键集合固定，正文、备注原文都不在里面。
func TestPromptAuditInputKeyWhitelist(t *testing.T) {
	t.Parallel()

	saved := promptSaveAuditInput(map[string]any{
		"key": "extract.chat", "lang": "zh", "body": "x ${conversation}",
		"note": "备注", "baseVersionId": float64(2), "restoreFromVersionId": float64(0),
	})
	allowed := map[string]bool{
		"promptKey": true, "lang": true, "baseVersionId": true, "restoreFromVersionId": true,
		"noteLength": true, "promptLength": true, "promptSha256": true, "versionId": true,
	}
	for key := range saved {
		if !allowed[key] {
			t.Fatalf("审计入参出现了白名单之外的键: %s", key)
		}
	}
	reset := promptResetAuditInput(map[string]any{"key": "extract.chat", "lang": "zh", "marker": "x"})
	if len(reset) != 2 || reset["promptKey"] != "extract.chat" || reset["lang"] != "zh" {
		t.Fatalf("还原审计只应记槽位: %v", reset)
	}
}

// 本轮新增字段逐个用非零/非空替身值核对：传输层漏映射一处，这里就会看到 0 或空。
// 逐字段清单与 status 里的表格一一对应。
func TestPromptNewFieldsPassThroughExactValues(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)

	// state[lang].versionNo；默认槽位仍是 0 + null。
	prompts, _ := decodeData(t, call(t, srv, http.MethodPost, "/api/dashboard/prompts/list",
		`{"projectId":"cube-a"}`))["prompts"].([]any)
	state, _ := prompts[0].(map[string]any)["state"].(map[string]any)
	zhState, _ := state["zh"].(map[string]any)
	if zhState["versionNo"] != float64(2) {
		t.Fatalf("state.zh.versionNo = %v，期望 2", zhState["versionNo"])
	}
	enState, _ := state["en"].(map[string]any)
	if enState["versionNo"] != float64(0) || enState["updatedAt"] != nil || enState["author"] != nil {
		t.Fatalf("默认槽位应保持 0 + null: %v", enState)
	}

	// effective[lang].versionNo 与 sampleInputs[].lang。
	detail := decodeData(t, call(t, srv, http.MethodPost, "/api/dashboard/prompts/detail",
		`{"projectId":"cube-a","key":"extract.chat"}`))
	effective, _ := detail["effective"].(map[string]any)
	zhEffective, _ := effective["zh"].(map[string]any)
	if zhEffective["versionNo"] != float64(2) {
		t.Fatalf("effective.zh.versionNo = %v，期望 2", zhEffective["versionNo"])
	}
	samples, _ := detail["sampleInputs"].([]any)
	if sample, _ := samples[0].(map[string]any); sample["lang"] != "zh" {
		t.Fatalf("sampleInputs[0].lang = %v，期望 zh", sample["lang"])
	}

	// versions[].versionNo。
	versions, _ := decodeData(t, call(t, srv, http.MethodPost, "/api/dashboard/prompts/versions",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh"}`))["versions"].([]any)
	if v, _ := versions[0].(map[string]any); v["versionNo"] != float64(2) {
		t.Fatalf("versions[0].versionNo = %v，期望 2", v["versionNo"])
	}

	// save 顶层与 effective 的 versionNo。
	saved := decodeData(t, call(t, srv, http.MethodPost, "/api/dashboard/prompts/save",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"新 ${conversation}","baseVersionId":3}`))
	if saved["versionNo"] != float64(3) {
		t.Fatalf("save.versionNo = %v，期望 3", saved["versionNo"])
	}
	savedEffective, _ := saved["effective"].(map[string]any)
	if savedEffective["versionNo"] != float64(3) {
		t.Fatalf("save.effective.versionNo = %v，期望 3", savedEffective["versionNo"])
	}

	// reset 清单项的 versionNo（本轮返工点）。
	reset, _ := decodeData(t, call(t, srv, http.MethodPost, "/api/dashboard/prompts/reset",
		`{"projectId":"cube-a","key":"extract.chat"}`))["reset"].([]any)
	if r, _ := reset[0].(map[string]any); r["versionNo"] != float64(4) {
		t.Fatalf("reset[0].versionNo = %v，期望 4", r["versionNo"])
	}

	// 409 的 currentVersionNo 与 currentVersionId。
	var env struct {
		Data map[string]any `json:"data"`
	}
	conflict := call(t, srv, http.MethodPost, "/api/dashboard/prompts/save",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x ${conversation}","baseVersionId":1}`)
	if err := json.Unmarshal(conflict.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode conflict: %v", err)
	}
	if env.Data["currentVersionId"] != float64(3) || env.Data["currentVersionNo"] != float64(2) {
		t.Fatalf("409 的版本字段 = %v", env.Data)
	}

	// runs[].usedSlots、runs[].warnings、usage.calls。
	runs, _ := decodeData(t, call(t, srv, http.MethodPost, "/api/dashboard/prompts/test",
		`{"projectId":"cube-a","key":"extract.chat","lang":"zh","input":{"messages":[]}}`))["runs"].([]any)
	run, _ := runs[0].(map[string]any)
	slots, _ := run["usedSlots"].([]any)
	slot, _ := slots[0].(map[string]any)
	if slot["key"] != "extract.chat" || slot["lang"] != "zh" || slot["source"] != "draft" ||
		slot["versionId"] != float64(3) {
		t.Fatalf("usedSlots[0] = %v", slot)
	}
	warnings, _ := run["warnings"].([]any)
	if w, _ := warnings[0].(map[string]any); w["code"] != "draft_not_used" || w["message"] == "" {
		t.Fatalf("warnings[0] = %v", w)
	}
	usage, _ := run["usage"].(map[string]any)
	if usage["calls"] != float64(1) || usage["promptTokens"] != float64(812) {
		t.Fatalf("usage = %v", usage)
	}
}

// H1：别人把槽位还原成默认后，冲突里的当前版本是 0（合法值）。
// 409 必须始终带 currentVersionId/currentVersionNo，不能因为等于 0 就省掉。
func TestPromptsConflictAlwaysCarriesCurrentVersion(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	rec := call(t, srv, http.MethodPost, "/api/dashboard/prompts/save",
		`{"projectId":"cube-a","key":"err.conflict-default","lang":"zh","body":"x ${conversation}"}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("应 409: %d %s", rec.Code, rec.Body.String())
	}
	var env struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got, ok := env.Data["currentVersionId"]; !ok || got != float64(0) {
		t.Fatalf("currentVersionId 必须存在且为 0: %v", env.Data)
	}
	if got, ok := env.Data["currentVersionNo"]; !ok || got != float64(0) {
		t.Fatalf("currentVersionNo 必须存在且为 0: %v", env.Data)
	}
}

// H2：413 的信封码是全局的，非 prompts 路由超过请求体上限也必须是 413 + 41300。
func TestDashboardBodyLimitUsesPayloadTooLargeCode(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	handler := httpx.MaxBody(64)(srv.Handler())
	body := `{"projectId":"cube-a","pad":"` + strings.Repeat("x", 128) + `"}`
	req := httptest.NewRequestWithContext(context.Background(),
		http.MethodPost, "/api/dashboard/project/list", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+adminToken)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusRequestEntityTooLarge || !strings.Contains(rec.Body.String(), `"code":41300`) {
		t.Fatalf("超过请求体上限应 413 + 41300: %d %s", rec.Code, rec.Body.String())
	}
}

// H3：三条读路由也必须显式带 projectId。用“有当前项目”的会话主体来测：
// InBody 会静默回落到 cube-a 并返回 200，只有 InBodyStrict 才会 400。
func TestPromptsReadRoutesRequireProjectID(t *testing.T) {
	t.Parallel()

	srv := newTestServer(t)
	srv.deps.Accounts = sessionAccounts{}
	for _, path := range []string{"list", "detail", "versions"} {
		rec := callAs(t, srv, sessionToken, http.MethodPost, "/api/dashboard/prompts/"+path,
			`{"key":"extract.chat","lang":"zh"}`)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s 缺 projectId 应 400（不能回落到当前项目）: %d %s", path, rec.Code, rec.Body.String())
		}
		// 带上 projectId 仍正常。
		rec = callAs(t, srv, sessionToken, http.MethodPost, "/api/dashboard/prompts/"+path,
			`{"projectId":"cube-a","key":"extract.chat","lang":"zh"}`)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s 带 projectId 应 200: %d %s", path, rec.Code, rec.Body.String())
		}
	}
}
