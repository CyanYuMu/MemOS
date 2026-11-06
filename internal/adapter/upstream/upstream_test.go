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

func newTestClient(t *testing.T, handler http.Handler, maxBytes int64) *Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	c, err := New(Options{BaseURL: srv.URL, Timeout: 2 * time.Second, MaxResponseBytes: maxBytes}, nil)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

func TestSearchFlattensBuckets(t *testing.T) {
	t.Parallel()

	var got map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/search" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{
			"text_mem":[{"total_nodes":3,"memories":[
				{"id":"a","memory":"低分","metadata":{"status":"activated","relativity":0.2,"created_at":"2026-10-21T12:00:00Z","tags":["t1"]}},
				{"id":"b","memory":"高分","metadata":{"status":"activated","relativity":0.9,"created_at":"2026-10-21T12:00:00+08:00"}},
				{"id":"b","memory":"重复","metadata":{"status":"activated","relativity":0.9}},
				{"id":"c","memory":"已删","metadata":{"status":"deleted","relativity":0.99}}
			]}],
			"pref_mem":[{"memories":[{"id":"p","memory":"偏好","metadata":{"relativity":0.5}}]}],
			"tool_mem":[{"memories":[{"id":"t","memory":"工具","metadata":{"relativity":0.5}}]}],
			"dream_mem":[{"memories":[{"id":"d","memory":"摘要","metadata":{"relativity":0.5,"title":"标题","created_at":"2026-10-21T00:00:00Z"}}]}]
		}}`)
	})
	c := newTestClient(t, handler, 1<<20)

	result, err := c.Search(context.Background(), domain.SearchQuery{
		TenantID: "cube-a", UserID: "alice", Text: "q", TopK: 10, DreamTopK: 1,
	})
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if got["mem_cube_id"] != "cube-a" || got["user_id"] != "alice" || got["dream_mem_top_k"] != float64(1) {
		t.Fatalf("payload = %v", got)
	}
	if len(result.Facts) != 2 || result.Facts[0].ID != "b" || result.Facts[1].ID != "a" {
		t.Fatalf("facts = %+v", result.Facts)
	}
	// 契约 v2：所有时间都是真 UTC，带偏移的写法按真实时刻换算。
	if result.Facts[1].CreatedAt.String() != "2026-10-21T12:00:00Z" {
		t.Fatalf("utc time changed: %s", result.Facts[1].CreatedAt)
	}
	if result.Facts[0].CreatedAt.String() != "2026-10-21T04:00:00Z" {
		t.Fatalf("+08:00 should convert to UTC: %s", result.Facts[0].CreatedAt)
	}
	if len(result.Preferences) != 1 || len(result.Tools) != 1 || len(result.Dreams) != 1 {
		t.Fatalf("buckets = %+v", result)
	}
	if result.Dreams[0].Key != "标题" {
		t.Fatalf("dream key = %q", result.Dreams[0].Key)
	}
	if result.Total != 3 {
		t.Fatalf("total = %d", result.Total)
	}
}

func TestSearchPushesFilterAndTopK(t *testing.T) {
	t.Parallel()

	var got map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/search" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"text_mem":[{"memories":[
			{"id":"a","memory":"告警","metadata":{"status":"activated","memory_type":"LongTermMemory","created_at":"2026-10-21T12:00:00Z"}}
		]}]}}`)
	})
	c := newTestClient(t, handler, 1<<20)

	_, err := c.Search(context.Background(), domain.SearchQuery{
		TenantID: "cube-a", UserID: "alice", Text: "告警", TopK: domain.ContractTopKMax,
		Filter: map[string]any{"status": "activated", "memory_type": "LongTermMemory", "user_id": "alice"},
	})
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	filter, _ := got["filter"].(map[string]any)
	if filter["status"] != "activated" || filter["memory_type"] != "LongTermMemory" || filter["user_id"] != "alice" {
		t.Fatalf("filter 应下推到上游: %v", got["filter"])
	}
	if got["top_k"] != float64(domain.ContractTopKMax) {
		t.Fatalf("top_k = %v", got["top_k"])
	}
}

func TestSearchIgnoresAuxiliaryBuckets(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{
			"text_mem":[{"total_nodes":1,"memories":[{"id":"a","memory":"事实","metadata":{"status":"activated"}}]}],
			"act_mem":[{"memories":[{"id":"x","memory":"活动"}]}],
			"pref_note":["字符串桶"],
			"other_mem":[{"memories":[{"id":"s","memory":"other"}]}]
		}}`)
	})
	c := newTestClient(t, handler, 1<<20)

	result, err := c.Search(context.Background(), domain.SearchQuery{TenantID: "cube-a", UserID: "alice", Text: "q", TopK: 10})
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(result.Facts) != 1 || result.Facts[0].ID != "a" || result.Total != 1 {
		t.Fatalf("只应收敛 text_mem: %+v", result)
	}
	if len(result.Preferences) != 0 || len(result.Tools) != 0 || len(result.Dreams) != 0 {
		t.Fatalf("辅助桶不应进入结果: %+v", result)
	}
}

func TestSearchTruncatesPerBucket(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"data":{"text_mem":[{"memories":[
			{"id":"1","metadata":{"relativity":0.1}},
			{"id":"2","metadata":{"relativity":0.2}}
		]}],"pref_mem":[{"memories":[{"id":"p1","metadata":{"relativity":0.1}},{"id":"p2","metadata":{"relativity":0.2}}]}]}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	result, err := c.Search(context.Background(), domain.SearchQuery{TenantID: "c", UserID: "u", Text: "q", TopK: 1})
	if err != nil {
		t.Fatalf("Search: %v", err)
	}
	if len(result.Facts) != 1 || len(result.Preferences) != 1 {
		t.Fatalf("per-bucket truncate failed: %+v", result)
	}
	if result.Facts[0].ID != "2" {
		t.Fatalf("facts = %+v", result.Facts)
	}
}

func TestAddPayloadAndSilentFailure(t *testing.T) {
	t.Parallel()

	var body map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"status":"failure"},"usage":{"prompt_tokens":3}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	_, err := c.Add(context.Background(), domain.AddPayload{
		TenantID: "cube-a", UserID: "alice", SessionID: "conv_1",
		Messages: []domain.Message{{Role: "user", Content: "hi"}},
	})
	if !errors.Is(err, domain.ErrUpstreamRejected) {
		t.Fatalf("err = %v, want rejected", err)
	}
	if body["mem_cube_id"] != "cube-a" || body["async_mode"] != "sync" {
		t.Fatalf("payload = %v", body)
	}
	msgs, _ := body["messages"].([]any)
	first, _ := msgs[0].(map[string]any)
	if first["role"] != "user" || first["content"] != "hi" {
		t.Fatalf("messages = %v", body["messages"])
	}
	if _, ok := body["user_id"]; !ok {
		t.Fatalf("missing user_id: %v", body)
	}
}

func TestDeleteScopesTenantWithoutUserID(t *testing.T) {
	t.Parallel()

	var body map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&body)
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{"status":"success"}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	if err := c.Delete(context.Background(), "cube-a", []string{"m1", "m2"}); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	if _, exists := body["user_id"]; exists {
		t.Fatalf("delete must not send user_id: %v", body)
	}
	// 共享图库没有默认 cube，上游要求显式租户作用域才允许按 id 删除。
	cubes, _ := body["writable_cube_ids"].([]any)
	if len(cubes) != 1 || cubes[0] != "cube-a" {
		t.Fatalf("writable_cube_ids = %v", body["writable_cube_ids"])
	}
	ids, _ := body["memory_ids"].([]any)
	if len(ids) != 2 {
		t.Fatalf("ids = %v", body["memory_ids"])
	}
}

func TestOwnedMemoryScopesTenant(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_memory/m1" {
			t.Errorf("path = %s", r.URL.Path)
		}
		if r.URL.Query().Get("mem_cube_id") != "cube-a" {
			_, _ = io.WriteString(w, `{"code":200,"data":null}`)
			return
		}
		_, _ = io.WriteString(w, `{"code":200,"data":{"id":"m1","memory":"x","metadata":{"status":"activated","user_id":"alice"}}}`)
	})
	c := newTestClient(t, handler, 1<<20)

	mem, ok, err := c.OwnedMemory(context.Background(), "cube-a", "alice", "m1")
	if err != nil || !ok || mem.ID != "m1" {
		t.Fatalf("owned = %+v, %v, %v", mem, ok, err)
	}
	if _, ok, err := c.OwnedMemory(context.Background(), "cube-b", "alice", "m1"); err != nil || ok {
		t.Fatalf("cross tenant lookup: ok=%v err=%v", ok, err)
	}
	if _, ok, err := c.OwnedMemory(context.Background(), "cube-a", "bob", "m1"); err != nil || ok {
		t.Fatalf("wrong user lookup: ok=%v err=%v", ok, err)
	}
}

func TestOwnedMemoriesScopesTenant(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_memory_by_ids" {
			t.Errorf("path = %s", r.URL.Path)
		}
		if r.URL.Query().Get("mem_cube_id") != "cube-a" {
			t.Errorf("mem_cube_id = %q", r.URL.Query().Get("mem_cube_id"))
		}
		var body struct {
			MemoryIDs []string `json:"memory_ids"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if len(body.MemoryIDs) != 5 {
			t.Errorf("memory_ids = %v", body.MemoryIDs)
		}
		_, _ = io.WriteString(w, `{"code":200,"data":{"memories":[
			{"id":"m1","memory":"x","metadata":{"status":"activated","user_id":"alice"}},
			{"id":"m2","memory":"x","metadata":{"status":"activated","mem_cube_id":"cube-b","user_id":"alice"}},
			{"id":"m3","memory":"x","metadata":{"status":"activated","user_id":"bob"}},
			{"id":"m4","memory":"x","metadata":{"status":"deleted","user_id":"alice"}}
		]}}`)
	})
	c := newTestClient(t, handler, 1<<20)

	owned, err := c.OwnedMemories(context.Background(), "cube-a", "alice", []string{"m1", "m2", "m3", "m4", "missing"})
	if err != nil {
		t.Fatalf("OwnedMemories: %v", err)
	}
	if len(owned) != 1 || !owned["m1"] {
		t.Fatalf("owned = %v, want only m1", owned)
	}
}

func TestOwnedMemoriesRejectsMissingTenant(t *testing.T) {
	t.Parallel()

	c := newTestClient(t, http.HandlerFunc(func(_ http.ResponseWriter, _ *http.Request) {
		t.Error("上游不应被调用")
	}), 1<<20)
	if _, err := c.OwnedMemories(context.Background(), "", "alice", []string{"m1"}); err == nil {
		t.Fatal("缺少租户时应当报错")
	}
}

func TestOwnedMemoryRejectsExplicitForeignTenant(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"data":{"id":"m1","memory":"x","metadata":{"status":"activated","mem_cube_id":"cube-b","user_id":"alice"}}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	if _, ok, err := c.OwnedMemory(context.Background(), "cube-a", "alice", "m1"); err != nil || ok {
		t.Fatalf("显式外租户必须拒绝: ok=%v err=%v", ok, err)
	}
}

func TestOwnedMemoryFailsClosedWithoutOwnershipMetadata(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"data":{"id":"m1","memory":"x","metadata":{"status":"activated"}}}`)
	})
	c := newTestClient(t, handler, 1<<20)
	if _, ok, err := c.OwnedMemory(context.Background(), "cube-a", "alice", "m1"); err != nil || ok {
		t.Fatalf("missing tenant+user must not be owned: ok=%v err=%v", ok, err)
	}
}

func TestOwners(t *testing.T) {
	t.Parallel()

	var got map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_user_names_by_memory_ids" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = io.WriteString(w, `{"code":200,"data":{"m1":"cube-a","m2":"cube-b"},"message":"ok"}`)
	})
	c := newTestClient(t, handler, 1<<20)
	owners, err := c.Owners(context.Background(), []string{"m1", "m2"})
	if err != nil || owners["m1"] != "cube-a" || owners["m2"] != "cube-b" {
		t.Fatalf("owners = %v, %v", owners, err)
	}
	ids, _ := got["memory_ids"].([]any)
	if len(ids) != 2 {
		t.Fatalf("payload = %v", got)
	}
	if owners, err := c.Owners(context.Background(), nil); err != nil || len(owners) != 0 {
		t.Fatalf("empty owners = %v, %v", owners, err)
	}
}

func TestOwnersRejectsNonObject(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"data":["m1"],"message":"ok"}`)
	})
	c := newTestClient(t, handler, 1<<20)
	if _, err := c.Owners(context.Background(), []string{"m1"}); !errors.Is(err, domain.ErrUpstreamRejected) {
		t.Fatalf("non-object err = %v", err)
	}
}

func TestResponseLimitAndErrors(t *testing.T) {
	t.Parallel()

	big := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"data":{"text_mem":[]},"padding":"`+strings.Repeat("x", 4096)+`"}`)
	})
	c := newTestClient(t, big, 512)
	if _, err := c.Search(context.Background(), domain.SearchQuery{TenantID: "c", UserID: "u", Text: "q"}); !errors.Is(err, domain.ErrUpstreamRejected) {
		t.Fatalf("oversize err = %v", err)
	}

	unavailable := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = io.WriteString(w, `{"message":"bad gateway"}`)
	})
	c = newTestClient(t, unavailable, 1<<20)
	if _, err := c.Search(context.Background(), domain.SearchQuery{TenantID: "c", UserID: "u", Text: "q"}); !errors.Is(err, domain.ErrUpstreamUnavailable) {
		t.Fatalf("5xx err = %v", err)
	}

	invalid := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = io.WriteString(w, `{"message":"field required"}`)
	})
	c = newTestClient(t, invalid, 1<<20)
	if _, err := c.Search(context.Background(), domain.SearchQuery{TenantID: "c", UserID: "u", Text: "q"}); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("422 err = %v", err)
	}
}

// 401/403/404/409 是控制面侧问题（不可重试），429 是限流（可重试）。
func TestHTTPErrorClassification(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		status int
		want   error
	}{
		{http.StatusUnauthorized, domain.ErrUpstreamDenied},
		{http.StatusForbidden, domain.ErrUpstreamDenied},
		{http.StatusNotFound, domain.ErrUpstreamDenied},
		{http.StatusConflict, domain.ErrUpstreamDenied},
		{http.StatusTooManyRequests, domain.ErrRateLimited},
		{http.StatusInternalServerError, domain.ErrUpstreamUnavailable},
	} {
		handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(tc.status)
			_, _ = io.WriteString(w, `{"message":"upstream says no"}`)
		})
		c := newTestClient(t, handler, 1<<20)
		_, err := c.Search(context.Background(), domain.SearchQuery{TenantID: "c", UserID: "u", Text: "q"})
		if !errors.Is(err, tc.want) {
			t.Fatalf("status %d err = %v, want %v", tc.status, err, tc.want)
		}
	}
}

func TestTimeoutIsUnavailable(t *testing.T) {
	t.Parallel()

	slow := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(200 * time.Millisecond)
		_, _ = io.WriteString(w, `{"code":200}`)
	})
	srv := httptest.NewServer(slow)
	t.Cleanup(srv.Close)
	c, err := New(Options{BaseURL: srv.URL, Timeout: 20 * time.Millisecond, MaxResponseBytes: 1 << 20}, nil)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if _, err := c.Search(context.Background(), domain.SearchQuery{TenantID: "c", UserID: "u", Text: "q"}); !errors.Is(err, domain.ErrUpstreamUnavailable) {
		t.Fatalf("timeout err = %v", err)
	}
}

func TestFeedbackParsesRecord(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":[{"answer":"a","record":{
			"add":[{"id":"a1","text":"n"}],"update":[{"id":"u1","text":"t","archived_id":"old"}]}}]}`)
	})
	c := newTestClient(t, handler, 1<<20)
	result, err := c.Feedback(context.Background(), domain.FeedbackRequest{TenantID: "c", UserID: "u", Content: "fix"})
	if err != nil {
		t.Fatalf("Feedback: %v", err)
	}
	if len(result.Added) != 1 || len(result.Updated) != 1 || result.Updated[0].ArchivedID != "old" {
		t.Fatalf("result = %+v", result)
	}
	if !strings.Contains(result.Message, "1 added, 1 updated") {
		t.Fatalf("message = %q", result.Message)
	}
}

func TestDeleteEmptyIsNoop(t *testing.T) {
	t.Parallel()

	called := false
	handler := http.HandlerFunc(func(http.ResponseWriter, *http.Request) { called = true })
	c := newTestClient(t, handler, 1<<20)
	if err := c.Delete(context.Background(), "cube-a", nil); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	if called {
		t.Fatal("empty delete should not call upstream")
	}
}

func TestTypeHintUsesStdlibError(t *testing.T) {
	t.Parallel()

	var target map[string][]bucketGroup
	err := json.Unmarshal([]byte(`{"text_mem":"oops"}`), &target)
	if got := typeHint(err); !strings.Contains(got, "text_mem") || !strings.Contains(got, "string") {
		t.Fatalf("typeHint = %q", got)
	}
	if got := typeHint(errors.New("other")); got != "" {
		t.Fatalf("非类型错误不应有提示: %q", got)
	}
}

func TestAddPreservesWrittenIDsWithinTenant(t *testing.T) {
	t.Parallel()
	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"data":[{"memory_id":"m1","cube_id":"cube-a"},{"memory_id":"m1","cube_id":"cube-a"},{"memory_id":"foreign","cube_id":"cube-b"},{"memory_id":"m2"}]}`)
	}), 1<<20)
	got, err := c.Add(context.Background(), domain.AddPayload{TenantID: "cube-a"})
	if err != nil || len(got.MemoryIDs) != 2 || got.MemoryIDs[0] != "m1" || got.MemoryIDs[1] != "m2" {
		t.Fatalf("result=%+v err=%v", got, err)
	}
}

func TestOwnedMemoryPreservesFullMetadata(t *testing.T) {
	t.Parallel()
	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":200,"data":{"id":"m1","memory":"午餐","metadata":{"mem_cube_id":"cube-a","key":"午餐计划","memory_type":"UserMemory","status":"activated","tags":["午餐"],"created_at":"2026-10-30T01:00:00Z","updated_at":"2026-10-31T01:02:03Z"}}}`)
	}), 1<<20)
	got, owned, err := c.OwnedMemory(context.Background(), "cube-a", "alice", "m1")
	if err != nil || !owned || got.Key != "午餐计划" || got.NodeType != "UserMemory" || len(got.Tags) != 1 || got.UpdatedAt.UTC().Format(time.RFC3339) != "2026-10-31T01:02:03Z" {
		t.Fatalf("memory=%+v owned=%v err=%v", got, owned, err)
	}
}

func TestMemoryDetailReadsSourcesOnlyWhenRequested(t *testing.T) {
	t.Parallel()
	var includes []string
	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		includes = append(includes, r.URL.Query().Get("include_sources"))
		_, _ = io.WriteString(w, `{"code":200,"data":[{"id":"detail","memory":"current","metadata":{"mem_cube_id":"p1","version":2,"history":[{"version":1,"memory":"old","created_at":"2026-10-30T00:00:00Z"}],"sources":[{"type":"chat","role":"user","content":"source"}]}}]}`)
	}), 1<<20)
	mem, owned, err := c.MemoryDetail(context.Background(), "p1", "u1", "detail")
	if err != nil || !owned || mem.Version != 2 || len(mem.History) != 1 || len(mem.Sources) != 1 {
		t.Fatalf("detail=%+v owned=%v err=%v", mem, owned, err)
	}
	_, _, err = c.OwnedMemory(context.Background(), "p1", "u1", "detail")
	if err != nil {
		t.Fatal(err)
	}
	_, foreign, err := c.MemoryDetail(context.Background(), "p2", "u2", "detail")
	if err != nil || foreign {
		t.Fatalf("foreign=%v err=%v", foreign, err)
	}
	if len(includes) != 3 || includes[0] != "true" || includes[1] != "false" {
		t.Fatalf("source flags=%v", includes)
	}
}

func TestAddPassesRequestScopedPersonalFactBoundary(t *testing.T) {
	t.Parallel()
	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/add" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
			return
		}
		custom, ok := payload["custom_extract_prompt"].(map[string]any)
		if !ok || len(custom) != 3 {
			t.Errorf("missing request-scoped fact boundary: %v", payload)
		}
		for _, key := range []string{"detail_factual", "preference", "profile"} {
			if custom[key] != personalFactExtraction {
				t.Errorf("fact boundary missing for %s", key)
			}
		}
		if payload["mem_cube_id"] != "cube-a" || payload["user_id"] != "alice" || payload["async_mode"] != "sync" {
			t.Error("original request scope changed")
		}
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":[]}`)
	}), 1<<20)
	if _, err := c.Add(context.Background(), domain.AddPayload{TenantID: "cube-a", UserID: "alice"}); err != nil {
		t.Fatal(err)
	}
}
