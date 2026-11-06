package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestGraphMemoriesMapsNodesEdgesAndClampsLimit(t *testing.T) {
	t.Parallel()

	var got map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_memory_graph" || r.Method != http.MethodPost {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{
			"cube_id":"cube-a",
			"nodes":[{"id":"n1","memory":"短标签","memoryTruncated":false,"memoryType":"Context",
				"status":"activated","createdAt":"2026-10-21T10:00:00+00:00","sessionId":"s1",
				"userId":"alice","sourceMemoryIds":["src-1"]},
				{"id":"n2","memory":"长标签…","memoryTruncated":true,"memoryType":"InsightMemory",
				"createdAt":"2026-10-22T10:00:00+00:00","userId":"cube-a"}],
			"edges":[{"source":"n1","target":"n2","type":"PARENT"}],
			"total_nodes":5,"truncated":true
		}}`))
	})
	c := newTestClient(t, handler, 1<<20)

	page, err := c.GraphMemories(context.Background(), "cube-a", []string{"Context", "InsightMemory"}, 50_000)
	if err != nil {
		t.Fatalf("GraphMemories: %v", err)
	}
	if got["mem_cube_id"] != "cube-a" || got["limit"] != float64(graphNodeMax) {
		t.Fatalf("payload = %v", got)
	}
	if status, _ := got["status"].([]any); len(status) != 1 || status[0] != "activated" {
		t.Fatalf("status = %v", got["status"])
	}
	if !page.Truncated || page.Total != 5 {
		t.Fatalf("page meta = %+v", page)
	}
	if len(page.Nodes) != 2 || page.Nodes[0].ID != "n1" || page.Nodes[0].UserID != "alice" ||
		page.Nodes[0].SessionID != "s1" || page.Nodes[0].SourceIDs[0] != "src-1" {
		t.Fatalf("nodes = %+v", page.Nodes)
	}
	if !page.Nodes[1].Truncated {
		t.Fatal("memoryTruncated 应映射到 Truncated")
	}
	if len(page.Edges) != 1 || page.Edges[0].Kind != "PARENT" || page.Edges[0].Target != "n2" {
		t.Fatalf("edges = %+v", page.Edges)
	}
}

func TestEventMemoriesPassesPaginationAndSearch(t *testing.T) {
	t.Parallel()

	var got map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_event_memories" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{
			"cube_id":"cube-a",
			"memories":[{"id":"e1","memory":"digest","memoryType":"EventMemory",
				"createdAt":"2026-10-21T10:00:00+00:00","userId":"alice"}],
			"total":41,"page":2,"page_size":999
		}}`))
	})
	c := newTestClient(t, handler, 1<<20)

	rows, total, err := c.EventMemories(context.Background(), domain.EventMemoriesQuery{
		TenantID: "cube-a", Types: []string{"EventMemory"}, Query: "  alert  ", Page: 2, Size: 999,
	})
	if err != nil {
		t.Fatalf("EventMemories: %v", err)
	}
	if got["page"] != float64(2) || got["page_size"] != float64(graphEventMax) {
		t.Fatalf("payload = %v", got)
	}
	if got["search_query"] != "alert" {
		t.Fatalf("search_query = %v", got["search_query"])
	}
	if total != 41 || len(rows) != 1 || rows[0].UserID != "alice" || rows[0].Type != "EventMemory" {
		t.Fatalf("rows/total = %+v/%d", rows, total)
	}
}

func TestGraphRequiresTenant(t *testing.T) {
	t.Parallel()

	c := newTestClient(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("不应请求上游")
	}), 1<<20)

	if _, err := c.GraphMemories(context.Background(), " ", nil, 10); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("GraphMemories err = %v", err)
	}
	if _, _, err := c.EventMemories(context.Background(), domain.EventMemoriesQuery{Types: []string{"Context"}, Page: 1, Size: 10}); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("EventMemories err = %v", err)
	}
	if _, err := c.MemoryBody(context.Background(), "cube-a", ""); !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("MemoryBody err = %v", err)
	}
}

func TestMemoryBodyIsTenantScoped(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_memory/m1" || r.URL.Query().Get("mem_cube_id") != "cube-a" {
			t.Errorf("request = %s?%s", r.URL.Path, r.URL.RawQuery)
		}
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{
			"id":"m1","memory":"完整正文","metadata":{
				"mem_cube_id":"cube-a","created_at":"2026-10-18T09:10:38.097378Z",
				"user_id":"alice","memory_type":"Context"
			}
		}}`))
	})
	c := newTestClient(t, handler, 1<<20)

	got, err := c.MemoryBody(context.Background(), "cube-a", "m1")
	if err != nil || got.Content != "完整正文" {
		t.Fatalf("body = %+v, err = %v", got, err)
	}
	if got.CreatedAt != "2026-10-18T09:10:38.097378Z" || got.UserID != "alice" || got.Type != "Context" {
		t.Fatalf("body meta = %+v", got)
	}

	mismatch := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{
			"id":"m1","memory":"别人的正文","metadata":{"mem_cube_id":"cube-b"}
		}}`))
	}), 1<<20)
	if _, err := mismatch.MemoryBody(context.Background(), "cube-a", "m1"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("mismatched tenant err = %v", err)
	}
}

func TestMemoryBodyMissingCreatedAtIsEmpty(t *testing.T) {
	t.Parallel()

	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{
			"id":"m1","memory":"旧上游正文","metadata":{"mem_cube_id":"cube-a"}
		}}`))
	}), 1<<20)

	got, err := c.MemoryBody(context.Background(), "cube-a", "m1")
	if err != nil {
		t.Fatalf("MemoryBody: %v", err)
	}
	if got.Content != "旧上游正文" || got.CreatedAt != "" {
		t.Fatalf("旧上游缺 created_at 时应只返回正文: %+v", got)
	}
}

// 历史行可能带 RFC3339 不接受的脏 created_at。时间只影响展示，解析失败必须退化为
// 空串，不能把详情变成 502——列表走 BeijingClock 能容错，详情也要同样能打开。
func TestMemoryBodyDirtyCreatedAtDoesNotFail(t *testing.T) {
	t.Parallel()

	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{
			"id":"m1","memory":"完整正文","metadata":{
				"mem_cube_id":"cube-a","created_at":"2026-10-18 09:10:38.097+0800",
				"user_id":"alice","memory_type":"Context"
			}
		}}`))
	}), 1<<20)

	got, err := c.MemoryBody(context.Background(), "cube-a", "m1")
	if err != nil {
		t.Fatalf("脏 created_at 不应让详情失败: %v", err)
	}
	if got.Content != "完整正文" || got.CreatedAt != "" {
		t.Fatalf("脏 created_at 应只影响时间字段: %+v", got)
	}
	if got.UserID != "alice" || got.Type != "Context" {
		t.Fatalf("主体字段应照常返回: %+v", got)
	}
}

func TestGraphMissingEndpointIsDenied(t *testing.T) {
	t.Parallel()

	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.NotFound(w, nil)
	}), 1<<20)

	// 404 说明上游版本没有该接口：属控制面侧配置/版本问题，重试无意义。
	if _, err := c.GraphMemories(context.Background(), "cube-a", nil, 10); !errors.Is(err, domain.ErrUpstreamDenied) {
		t.Fatalf("404 should map to upstream denied, got %v", err)
	}
}
