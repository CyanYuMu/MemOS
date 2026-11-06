package upstream

import (
	"context"
	"errors"
	"io"
	"net/http"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestExtractPromptEndpoints(t *testing.T) {
	t.Parallel()

	var seen []map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, map[string]any{"path": r.URL.Path, "method": r.Method})
		switch r.URL.Path {
		case "/product/get_extract_prompt":
			_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{
				"mem_cube_id":"cube-a","prompt":"规则 ${conversation}",
				"extraction_types":["text_mem","pref_mem"],"updated_at":"2026-10-22T00:00:00Z",
				"source":"saved","is_default":false}}`)
		case "/product/save_extract_prompt", "/product/reset_extract_prompt":
			_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"prompt":"内置 ${conversation}"}}`)
		case "/product/generate_extract_prompt":
			_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"prompt":"重写 ${conversation}"}}`)
		case "/product/debug_extract":
			_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{
				"extraction_types":["text_mem"],
				"results":[{"type":"LongTermMemory","content":"用户喜欢深色模式"}]}}`)
		case "/product/models":
			_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"records":[
				{"role":"记忆读取","model":"reader","spec":"host","source":"MEMRADER_MODEL","group":"memory"}],
				"total":1}}`)
		default:
			_, _ = io.WriteString(w, `{"code":404,"message":"not found","data":null}`)
		}
	})
	c := newTestClient(t, handler, 1<<20)
	ctx := context.Background()

	got, err := c.GetExtractPrompt(ctx, "cube-a")
	if err != nil || got.Prompt != "规则 ${conversation}" || got.IsDefault || got.UpdatedAt == "" {
		t.Fatalf("get = %+v, %v", got, err)
	}

	if err := c.SaveExtractPrompt(ctx, "cube-a", "规则 ${conversation}"); err != nil {
		t.Fatalf("save: %v", err)
	}
	reset, err := c.ResetExtractPrompt(ctx, "cube-a")
	if err != nil || reset.Prompt != "内置 ${conversation}" {
		t.Fatalf("reset = %+v, %v", reset, err)
	}
	generated, err := c.GenerateExtractPrompt(ctx, "cube-a", "只抽安全事件")
	if err != nil || generated != "重写 ${conversation}" {
		t.Fatalf("generate = %q, %v", generated, err)
	}

	debug, err := c.DebugExtract(ctx, "cube-a",
		[]domain.PromptMessage{{Role: "user", Content: "我喜欢深色模式"}}, []string{"text_mem"})
	if err != nil || len(debug.Results) != 1 || debug.Results[0].Content != "用户喜欢深色模式" {
		t.Fatalf("debug = %+v, %v", debug, err)
	}

	models, err := c.Models(ctx)
	if err != nil || len(models) != 1 || models[0].Source != "MEMRADER_MODEL" || models[0].Group != "memory" {
		t.Fatalf("models = %+v, %v", models, err)
	}

	paths := make([]string, 0, len(seen))
	for _, item := range seen {
		path, _ := item["path"].(string)
		paths = append(paths, path)
	}
	want := []string{
		"/product/get_extract_prompt", "/product/save_extract_prompt", "/product/reset_extract_prompt",
		"/product/generate_extract_prompt", "/product/debug_extract", "/product/models",
	}
	for i := range want {
		if paths[i] != want[i] {
			t.Fatalf("调用顺序 = %v", paths)
		}
	}
}

func TestExtractPromptRejectsProjectWithoutPlaceholder(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":400,"message":"prompt must contain ${conversation}","data":null}`)
	})
	c := newTestClient(t, handler, 1<<20)
	err := c.SaveExtractPrompt(context.Background(), "cube-a", "没有占位符")
	if !errors.Is(err, domain.ErrUpstreamRejected) {
		t.Fatalf("err = %v", err)
	}
}

func TestModelsPayloadShape(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Fatalf("模型清单应走 GET，实际 %s", r.Method)
		}
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"records":[],"total":0}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	models, err := c.Models(context.Background())
	if err != nil || len(models) != 0 {
		t.Fatalf("models = %v, %v", models, err)
	}
}

// 保证 json 解码失败也能映射成 upstream 错误，而不是 panic。
func TestExtractPromptMalformedData(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"prompt":42}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	if _, err := c.GetExtractPrompt(context.Background(), "cube-a"); err == nil {
		t.Fatal("类型不匹配应报错")
	}
}
