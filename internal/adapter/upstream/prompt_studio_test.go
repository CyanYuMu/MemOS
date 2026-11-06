package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// 提示词接口的请求体必须是 snake_case 且带 mem_cube_id；响应按第 6 节的形状解码。
func TestPromptGatewayRoundTrip(t *testing.T) {
	t.Parallel()

	seen := map[string]map[string]any{}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		seen[r.URL.Path] = body
		switch r.URL.Path {
		case "/product/prompts/list":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"prompts":[{
				"key":"extract.chat","group":"extract","title":{"zh":"对话抽取"},"summary":{"zh":"整理成记忆"},
				"languages":["zh","en"],"editable":true,
				"state":{"zh":{"source":"project","version_id":3,"version_no":2,"updated_at":"2026-10-28T10:00:00Z","author":"alice"},
				         "en":{"source":"default","version_id":0,"version_no":0,"updated_at":null,"author":null}}}]}}`)
		case "/product/prompts/detail":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{
				"key":"extract.chat","group":"extract","title":{"zh":"对话抽取"},
				"languages":["zh"],"syntax":"dollar","locked_suffix":"后缀",
				"placeholders":[{"name":"conversation","required":true,"description":{"zh":"对话"}}],
				"input_kind":"conversation","input_schema":{"type":"object"},
				"output_contract":{"format":"json","required_keys":["memory list"],"item_keys":["key"],"description":{"zh":"列表"}},
				"sample_inputs":[{"id":"s1","lang":"zh","title":{"zh":"样例"},"input":{"messages":[]}}],
				"editable":true,"defaults":{"zh":"默认 ${conversation}","en":"default"},
				"effective":{"zh":{"body":"自定义 ${conversation}","source":"project","version_id":3,"version_no":2,
					"updated_at":"2026-10-28T10:00:00Z","author":"alice"},
					"en":{"body":"default","source":"default","version_id":0,"version_no":0,
					"updated_at":null,"author":null}}}}`)
		case "/product/prompts/versions":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"versions":[
				{"version_id":3,"version_no":2,"lang":"zh","kind":"save","body":"b","note":"n","author":"alice","created_at":"2026-10-28T10:00:00Z","parent_version_id":1}],
				"has_more":true}}`)
		case "/product/prompts/validate":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"ok":false,
				"errors":[{"code":"missing_placeholder","message":"缺少必填占位符"}],
				"warnings":[{"code":"no_output_hint","message":"没有输出结构说明"}]}}`)
		case "/product/prompts/save":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"version_id":4,"version_no":3,
				"created_at":"2026-10-29T00:00:00Z","effective":{"body":"b","source":"project","version_id":4,"version_no":3,"updated_at":"2026-10-29T00:00:00Z","author":"alice"}}}`)
		case "/product/prompts/reset":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"reset":[
				{"key":"extract.chat","lang":"zh","version_id":5,"version_no":4}]}}`)
		case "/product/prompts/test":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"runs":[
				{"label":"draft","ok":true,"parse_ok":true,"fallback_triggered":false,
				 "contract":{"ok":true,"missing":[]},
				 "output":{"memory list":[{"key":"k1","memory_type":"UserMemory"}]},
				 "raw_output":"{}","truncated":false,"latency_ms":120,"model":"m",
				 "usage":{"prompt_tokens":8,"completion_tokens":2,"calls":1},
				 "used_slots":[{"key":"extract.chat","lang":"zh","source":"draft","version_id":3}],
				 "warnings":[{"code":"draft_not_used","message":"这段输入按 en 处理"}]}]}}`)
		case "/product/prompts/generate":
			_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"body":"改写",
				"validation":{"ok":true,"errors":[],"warnings":[]}}}`)
		default:
			w.WriteHeader(http.StatusNotFound)
			_, _ = io.WriteString(w, `{"code":"unknown_prompt_key","message":"no"}`)
		}
	})
	c := newTestClient(t, handler, 1<<20)
	gw := NewPromptGateway(c)
	ctx := context.Background()

	catalog, err := gw.List(ctx, "cube-a")
	if err != nil || len(catalog.Prompts) != 1 || catalog.Prompts[0].State["zh"].VersionID != 3 {
		t.Fatalf("list = %+v, %v", catalog, err)
	}
	if state := catalog.Prompts[0].State["zh"]; state.VersionNo != 2 || state.UpdatedAt == nil {
		t.Fatalf("state 缺 version_no 或 updated_at: %+v", state)
	}
	if dflt := catalog.Prompts[0].State["en"]; dflt.UpdatedAt != nil || dflt.Author != nil {
		t.Fatalf("默认槽位的 updated_at/author 必须是 null: %+v", dflt)
	}

	detail, err := gw.Detail(ctx, "cube-a", "extract.chat")
	if err != nil {
		t.Fatalf("detail: %v", err)
	}
	if detail.Syntax != "dollar" || detail.LockedSuffix == nil || *detail.LockedSuffix != "后缀" {
		t.Fatalf("detail 注册项不符: %+v", detail)
	}
	if string(detail.InputSchema) != `{"type":"object"}` || len(detail.SampleInputs) != 1 ||
		string(detail.SampleInputs[0].Input) != `{"messages":[]}` {
		t.Fatalf("透传字段被改写: %+v", detail)
	}
	if detail.OutputContract.RequiredKeys[0] != "memory list" || detail.Effective["zh"].VersionID != 3 {
		t.Fatalf("detail 结构不符: %+v", detail)
	}
	if detail.Effective["zh"].VersionNo != 2 || detail.Effective["en"].UpdatedAt != nil ||
		detail.SampleInputs[0].Lang != "zh" {
		t.Fatalf("detail 的新字段没有解出来: %+v", detail)
	}

	page, err := gw.Versions(ctx, domain.PromptVersionsQuery{
		TenantID: "cube-a", Key: "extract.chat", Lang: "zh", Limit: 5, BeforeVersionID: 9,
	})
	if err != nil || len(page.Versions) != 1 || !page.HasMore || page.Versions[0].ParentVersionID == nil {
		t.Fatalf("versions = %+v, %v", page, err)
	}
	if page.Versions[0].VersionNo != 2 {
		t.Fatalf("versions 缺 version_no: %+v", page.Versions[0])
	}

	validation, err := gw.Validate(ctx, domain.PromptValidateInput{Key: "k", Lang: "zh", Body: "b"})
	if err != nil || validation.OK || len(validation.Errors) != 1 || len(validation.Warnings) != 1 {
		t.Fatalf("validate = %+v, %v", validation, err)
	}

	saved, err := gw.Save(ctx, domain.PromptSaveInput{
		TenantID: "cube-a", Key: "extract.chat", Lang: "zh", Body: "b", Note: "n",
		Author: "alice", BaseVersionID: 3,
	})
	if err != nil || saved.VersionID != 4 || saved.Effective.Source != "project" || saved.VersionNo != 3 {
		t.Fatalf("save = %+v, %v", saved, err)
	}

	reset, err := gw.Reset(ctx, domain.PromptResetInput{TenantID: "cube-a", Key: "extract.chat", Author: "alice"})
	if err != nil || len(reset.Restored) != 1 || reset.Restored[0].VersionID != 5 ||
		reset.Restored[0].VersionNo != 4 {
		t.Fatalf("reset = %+v, %v", reset, err)
	}

	tested, err := gw.Test(ctx, domain.PromptTestInput{
		TenantID: "cube-a", Key: "extract.chat", Lang: "zh", Body: "draft",
		Input: map[string]any{"messages": []any{}}, CompareDefault: true,
	})
	if err != nil || len(tested.Runs) != 1 || !tested.Runs[0].ParseOK ||
		tested.Runs[0].Usage["prompt_tokens"] != float64(8) {
		t.Fatalf("test = %+v, %v", tested, err)
	}
	run := tested.Runs[0]
	if run.Usage["calls"] != float64(1) || len(run.UsedSlots) != 1 || run.UsedSlots[0].VersionID != 3 ||
		run.UsedSlots[0].Source != "draft" || len(run.Warnings) != 1 ||
		run.Warnings[0].Code != "draft_not_used" {
		t.Fatalf("test 的新字段没有解出来: %+v", run)
	}

	generated, err := gw.Generate(ctx, domain.PromptGenerateInput{
		TenantID: "cube-a", Key: "extract.chat", Lang: "zh", Instruction: "i", BaseBody: "b",
	})
	if err != nil || generated.Body != "改写" || !generated.Validation.OK {
		t.Fatalf("generate = %+v, %v", generated, err)
	}

	// 每个请求都带 mem_cube_id（validate 是纯静态校验，只有 key/lang/body）。
	for path, body := range seen {
		if path == "/product/prompts/validate" {
			if _, ok := body["mem_cube_id"]; ok {
				t.Fatalf("validate 不应带 mem_cube_id: %v", body)
			}
			continue
		}
		if body["mem_cube_id"] != "cube-a" {
			t.Fatalf("%s 缺少 mem_cube_id: %v", path, body)
		}
	}
	if seen["/product/prompts/save"]["base_version_id"] != float64(3) ||
		seen["/product/prompts/save"]["author"] != "alice" ||
		seen["/product/prompts/save"]["body"] != "b" {
		t.Fatalf("save 入参不符: %v", seen["/product/prompts/save"])
	}
	if seen["/product/prompts/versions"]["before_version_id"] != float64(9) ||
		seen["/product/prompts/versions"]["limit"] != float64(5) {
		t.Fatalf("versions 分页入参不符: %v", seen["/product/prompts/versions"])
	}
	if compare, _ := seen["/product/prompts/test"]["compare_default"].(bool); !compare {
		t.Fatalf("test 入参不符: %v", seen["/product/prompts/test"])
	}
}

func TestPromptGatewaySaveRestoreFromVersion(t *testing.T) {
	t.Parallel()

	var got map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = io.WriteString(w, `{"code":0,"data":{"version_id":6,"created_at":"x","effective":{}}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	if _, err := NewPromptGateway(c).Save(context.Background(), domain.PromptSaveInput{
		TenantID: "cube-a", Key: "k", Lang: "zh", RestoreFromVersionID: 2, Author: "alice",
	}); err != nil {
		t.Fatalf("save: %v", err)
	}
	if got["restore_from_version_id"] != float64(2) {
		t.Fatalf("回滚入参不符: %v", got)
	}
	if _, ok := got["body"]; ok {
		t.Fatalf("回滚不应同时带 body: %v", got)
	}
}

// 数据面的稳定 code 字符串映射成控制面的领域错误：只看状态码会丢版本号与等待时间。
func TestPromptGatewayErrorMapping(t *testing.T) {
	t.Parallel()

	cases := []struct {
		name   string
		status int
		body   string
		check  func(t *testing.T, err error)
	}{
		{
			name: "400 invalid_prompt", status: http.StatusBadRequest,
			body: `{"code":"invalid_prompt","message":"提示词没有通过校验，请按错误列表修改后再保存。",
				"data":{"errors":[{"code":"missing_placeholder","message":"缺少必填占位符 ${conversation}"}]}}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrInvalidInput) {
					t.Fatalf("err = %v", err)
				}
				if domain.SafeMessage(err) != "提示词没有通过校验，请按错误列表修改后再保存。" {
					t.Fatalf("message = %q", domain.SafeMessage(err))
				}
				invalid, ok := errors.AsType[*domain.InvalidPromptError](err)
				if !ok || len(invalid.Errors) != 1 || invalid.Errors[0].Code != "missing_placeholder" {
					t.Fatalf("逐条错误没有带上: %+v", err)
				}
			},
		},
		{
			name: "400 空 message 用兜底文案", status: http.StatusBadRequest,
			body: `{"code":"invalid_input","message":""}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrInvalidInput) || domain.SafeMessage(err) == "" {
					t.Fatalf("err = %v msg = %q", err, domain.SafeMessage(err))
				}
			},
		},
		{
			name: "404 unknown_prompt_key", status: http.StatusNotFound,
			body: `{"code":"unknown_prompt_key","message":"no such key"}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrNotFound) {
					t.Fatalf("err = %v", err)
				}
			},
		},
		{
			name: "404 unknown_version", status: http.StatusNotFound,
			body: `{"code":"unknown_version","message":"version not found"}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrNotFound) {
					t.Fatalf("历史版本不存在应映射成 404: %v", err)
				}
				if !strings.Contains(domain.SafeMessage(err), "版本") {
					t.Fatalf("文案应说明是版本问题: %q", domain.SafeMessage(err))
				}
			},
		},
		{
			name: "404 未部署该路由", status: http.StatusNotFound,
			body: `{"detail":"Not Found"}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrUpstreamDenied) {
					t.Fatalf("路由缺失应报控制面侧问题，而不是「提示词不存在」: %v", err)
				}
			},
		},
		{
			name: "409 version_conflict", status: http.StatusConflict,
			body: `{"code":"version_conflict","message":"stale","data":{"current":{"version_id":7,"source":"project"}}}`,
			check: func(t *testing.T, err error) {
				var conflict *domain.VersionConflictError
				if !errors.As(err, &conflict) || conflict.CurrentVersionID != 7 {
					t.Fatalf("err = %v", err)
				}
				if !errors.Is(err, domain.ErrConflict) {
					t.Fatalf("errors.Is(ErrConflict) 不成立: %v", err)
				}
			},
		},
		{
			name: "413 input_too_large", status: http.StatusRequestEntityTooLarge,
			body: `{"code":"input_too_large","message":"too big"}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrTooLarge) {
					t.Fatalf("err = %v", err)
				}
			},
		},
		{
			name: "429 busy", status: http.StatusTooManyRequests,
			body: `{"code":"busy","message":"busy","data":{"retry_after_sec":9}}`,
			check: func(t *testing.T, err error) {
				var busy *domain.BusyError
				if !errors.As(err, &busy) || busy.Wait != 9*time.Second {
					t.Fatalf("err = %v", err)
				}
				if !errors.Is(err, domain.ErrRateLimited) {
					t.Fatalf("errors.Is(ErrRateLimited) 不成立: %v", err)
				}
			},
		},
		{
			name: "502 llm_error", status: http.StatusBadGateway,
			body: `{"code":"llm_error","message":"boom"}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrModelFailed) {
					t.Fatalf("err = %v", err)
				}
				if domain.SafeMessage(err) != "模型调用失败，请稍后重试" {
					t.Fatalf("message 泄漏内部原因: %q", domain.SafeMessage(err))
				}
			},
		},
		{
			name: "504 llm_timeout", status: http.StatusGatewayTimeout,
			body: `{"code":"llm_timeout","message":"timeout"}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrModelTimeout) {
					t.Fatalf("err = %v", err)
				}
			},
		},
		{
			name: "500 未知失败", status: http.StatusInternalServerError,
			body: `{"code":"internal","message":"boom"}`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrUpstreamUnavailable) {
					t.Fatalf("err = %v", err)
				}
			},
		},
		{
			name: "错误体不是 JSON", status: http.StatusBadGateway,
			body: `not json`,
			check: func(t *testing.T, err error) {
				if !errors.Is(err, domain.ErrModelFailed) {
					t.Fatalf("err = %v", err)
				}
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()

			handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			})
			c := newTestClient(t, handler, 1<<20)
			_, err := NewPromptGateway(c).Detail(context.Background(), "cube-a", "k")
			if err == nil {
				t.Fatal("应返回错误")
			}
			tc.check(t, err)
		})
	}
}

func TestPromptGatewayMalformedData(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":0,"data":{"prompts":"not an array"}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	if _, err := NewPromptGateway(c).List(context.Background(), "cube-a"); !errors.Is(err, domain.ErrUpstreamRejected) {
		t.Fatalf("err = %v", err)
	}
}

func TestPromptGatewayUnreachable(t *testing.T) {
	t.Parallel()

	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	base := srv.URL
	srv.Close()
	c, err := New(Options{BaseURL: base, Timeout: time.Second}, nil)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if _, err := NewPromptGateway(c).List(context.Background(), "cube-a"); !errors.Is(err, domain.ErrUpstreamUnavailable) {
		t.Fatalf("err = %v", err)
	}
}
