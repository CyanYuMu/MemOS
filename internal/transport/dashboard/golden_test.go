package dashboard

import (
	"bytes"
	"encoding/json"
	"flag"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

const goldenPath = "../../../test/golden/dashboard/routes.json"

var updateGolden = flag.Bool("update-golden", false, "重写 Dashboard 响应形状快照")

// TestEnvelopeGolden 钉住已实现路由的信封形状与列表键。
// 只比较类型与键，不比较具体值；新增/删除路由必须同步更新 golden。
func TestEnvelopeGolden(t *testing.T) {
	srv := newTestServer(t)
	cases := []struct {
		method string
		path   string
		body   string
	}{
		{http.MethodGet, "/api/dashboard/project/dict", ""},
		{http.MethodPost, "/api/dashboard/project/list", `{"pageNum":1,"pageSize":10}`},
		{http.MethodPost, "/api/dashboard/project/create", `{"name":"cube-golden","ownerUsername":"alice"}`},
		{http.MethodPost, "/api/dashboard/project/update", `{"id":"cube-golden","status":"ENABLED"}`},
		{http.MethodPost, "/api/dashboard/project/revoke/cube-golden", ""},
		{http.MethodPost, "/api/dashboard/keys/list", `{"projectId":"cube-a"}`},
		{http.MethodPost, "/api/dashboard/keys/create", `{"projectId":"cube-a","keyName":"golden"}`},
		{http.MethodPost, "/api/dashboard/keys/revoke/nope", ""},
		{http.MethodPost, "/api/dashboard/keys/copy", `{"apiKeyId":"x"}`},
		{http.MethodPost, "/api/dashboard/project/memory/detail", `{"projectId":"cube-a","memoryId":"mem-1"}`},
		{http.MethodPost, "/api/dashboard/project/memory/delete", `{"projectId":"cube-a","memory_ids":["mem-1"]}`},
		{http.MethodPost, "/api/dashboard/project/memory/list", `{"projectId":"cube-a","memoryDetailType":"text_mem"}`},
		{http.MethodPost, "/api/dashboard/prompt-config/detail", `{"projectId":"cube-a"}`},
		{http.MethodPost, "/api/dashboard/prompt-config/save", `{"projectId":"cube-a","prompt":"p2"}`},
		{http.MethodPost, "/api/dashboard/profileTemplate/list", `{"projectId":"cube-a","pageNum":1,"pageSize":10}`},
		{http.MethodGet, "/api/dashboard/profileTemplate/detail?templateId=t1&projectId=cube-a", ""},
		{http.MethodPost, "/api/dashboard/profileTemplate/create", `{"projectId":"cube-a","templateName":"画像","基础":{"年龄":{"value":"int","algorithm_updatable":true}}}`},
		{http.MethodPost, "/api/dashboard/profileTemplate/update", `{"projectId":"cube-a","templateId":"t1","templateName":"新名"}`},
		{http.MethodPost, "/api/dashboard/profileTemplate/delete", `{"projectId":"cube-a","templateId":"t1"}`},
		{http.MethodPost, "/api/dashboard/prompt-config/reset-default", `{"projectId":"cube-a"}`},
		{http.MethodPost, "/api/dashboard/prompt-config/generate", `{"projectId":"cube-a","description":"x"}`},
		{http.MethodPost, "/api/dashboard/prompt-config/debug/extract", `{"projectId":"cube-a","messages":[]}`},
		{http.MethodPost, "/api/dashboard/prompts/list", `{"projectId":"cube-a"}`},
		{http.MethodPost, "/api/dashboard/prompts/detail", `{"projectId":"cube-a","key":"extract.chat"}`},
		{http.MethodPost, "/api/dashboard/prompts/versions", `{"projectId":"cube-a","key":"extract.chat","lang":"zh"}`},
		{http.MethodPost, "/api/dashboard/prompts/validate", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x"}`},
		{http.MethodPost, "/api/dashboard/prompts/save", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x"}`},
		{http.MethodPost, "/api/dashboard/prompts/reset", `{"projectId":"cube-a","key":"extract.chat"}`},
		{http.MethodPost, "/api/dashboard/prompts/test", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","input":{}}`},
		{http.MethodPost, "/api/dashboard/prompts/generate", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","instruction":"x"}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/list", `{"projectId":"cube-a","pageNum":1,"pageSize":10}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/detail", `{"projectId":"cube-a","knowledgeBaseId":"kb"}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/file/list", `{"projectId":"cube-a","knowledgeBaseId":"kb"}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/memory/list", `{"projectId":"cube-a","knowledgeBaseId":"kb"}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/create", `{"projectId":"cube-a","knowledgebaseName":"kb"}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/update", `{"projectId":"cube-a","id":"kb","knowledgebaseName":"kb2"}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/delete", `{"projectId":"cube-a","knowledgeBaseId":"kb"}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/file/add", `{"projectId":"cube-a","knowledgeBaseId":"kb","file":{"name":"a.txt","content":"aGk="}}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/file/delete", `{"projectId":"cube-a","fileIds":["file-1"]}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/project/associate", `{"projectId":"cube-a","knowledgeBaseIds":["kb"]}`},
		{http.MethodPost, "/api/dashboard/knowledgebase/project/remove", `{"projectId":"cube-a","knowledgeBaseId":"kb"}`},
		{http.MethodPost, "/api/dashboard/project/restore/cube-a", ""},
		{http.MethodGet, "/api/dashboard/models", ""},
		{http.MethodGet, "/api/dashboard/graph/memories?tenant=cube-a", ""},
		{http.MethodGet, "/api/dashboard/graph/memory?id=m1&tenant=cube-a", ""},
		{http.MethodGet, "/api/dashboard/local-sync/devices?projectId=cube-a", ""},
		{http.MethodDelete, "/api/dashboard/local-sync/devices/ldev-1?projectId=cube-a", ""},
		{http.MethodGet, "/api/dashboard/local-sync/memories?projectId=cube-a&page=1&pageSize=10", ""},
		{http.MethodGet, "/api/dashboard/local-sync/memories/lmem-1?projectId=cube-a", ""},
		{http.MethodDelete, "/api/dashboard/local-sync/memories/lmem-1?projectId=cube-a", ""},
		{http.MethodPost, "/api/dashboard/local-sync/memories/promote?projectId=cube-a", `{"ids":["lmem-1"]}`},
		{http.MethodGet, "/api/dashboard/local-sync/summary?projectId=cube-a", ""},
		{http.MethodGet, "/api/meta", ""},
		{http.MethodGet, "/api/health", ""},
		{http.MethodGet, "/api/user/quota/summary", ""},
		{http.MethodPost, "/api/user/subscription/details", `{}`},
	}

	golden := map[string]any{}
	for _, tc := range cases {
		rec := call(t, srv, tc.method, tc.path, tc.body)
		var env any
		if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
			t.Fatalf("%s %s: 响应不是 JSON: %v", tc.method, tc.path, err)
		}
		golden[routeKey(tc.method, tc.path)] = map[string]any{
			"status": rec.Code,
			"body":   shapeOf(env),
		}
	}
	// 同一条路由的失败形状也要钉住：版本冲突带当前版本号、限流带等待秒数、
	// 不存在的 key 是 404。routeKey 会抹掉查询串，所以这里用显式 key。
	for _, tc := range []struct {
		key, body string
	}{
		{"POST /api/dashboard/prompts/save [409]", `{"projectId":"cube-a","key":"extract.chat","lang":"zh","body":"x","baseVersionId":1}`},
		{"POST /api/dashboard/prompts/test [429]", `{"projectId":"cube-a","key":"err.busy","lang":"zh","input":{}}`},
		{"POST /api/dashboard/prompts/detail [404]", `{"projectId":"cube-a","key":"err.notfound"}`},
		{"POST /api/dashboard/prompts/save [400]", `{"projectId":"cube-a","key":"err.invalid","lang":"zh","body":"x"}`},
		{"POST /api/dashboard/prompts/generate [413]", `{"projectId":"cube-a","key":"err.toolarge","lang":"zh","instruction":"x"}`},
	} {
		rec := call(t, srv, http.MethodPost, strings.SplitN(tc.key, " ", 3)[1], tc.body)
		var env any
		if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
			t.Fatalf("%s: 响应不是 JSON: %v", tc.key, err)
		}
		golden[tc.key] = map[string]any{"status": rec.Code, "body": shapeOf(env)}
	}
	got, err := json.MarshalIndent(golden, "", " ")
	if err != nil {
		t.Fatalf("marshal golden: %v", err)
	}
	got = append(got, '\n')
	if *updateGolden {
		if err := os.MkdirAll(filepath.Dir(goldenPath), 0o750); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		if err := os.WriteFile(goldenPath, got, 0o600); err != nil {
			t.Fatalf("write golden: %v", err)
		}
	}
	want, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("Dashboard 响应形状与 golden 不一致；确认契约变更后运行 go test ./internal/transport/dashboard -update-golden")
	}
}

// 错误形状的 golden 条目名里带状态码。名字写 [413]、记录的却是 200 时，
// 这条 golden 什么都没锁住，还会把一个成功响应登记成 413（第二轮验收发现的空锁）。
func TestGoldenErrorNamesMatchStatus(t *testing.T) {
	t.Parallel()

	raw, err := os.ReadFile(goldenPath)
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var golden map[string]struct {
		Status int `json:"status"`
	}
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatalf("decode golden: %v", err)
	}

	checked := 0
	for key, entry := range golden {
		open := strings.LastIndex(key, "[")
		if open < 0 || !strings.HasSuffix(key, "]") {
			continue
		}
		want, err := strconv.Atoi(key[open+1 : len(key)-1])
		if err != nil {
			t.Fatalf("%q 里的状态码不是数字", key)
		}
		if entry.Status != want {
			t.Errorf("%q 记录的是 status=%d；名字与状态码不一致说明这条 golden 是空锁", key, entry.Status)
		}
		checked++
	}
	if checked < 5 {
		t.Fatalf("带状态码的 golden 条目只有 %d 条，预期的错误形状没有钉住", checked)
	}
}

func routeKey(method, path string) string {
	if idx := bytes.IndexByte([]byte(path), '?'); idx >= 0 {
		path = path[:idx]
	}
	return method + " " + path
}

func shapeOf(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		out := make(map[string]any, len(typed))
		for _, key := range keys {
			out[key] = shapeOf(typed[key])
		}
		return out
	case []any:
		if len(typed) == 0 {
			return "empty_array"
		}
		return map[string]any{"array_of": shapeOf(typed[0])}
	case string:
		return "string"
	case float64:
		return "number"
	case bool:
		return "bool"
	case nil:
		return "null"
	default:
		return "other"
	}
}
