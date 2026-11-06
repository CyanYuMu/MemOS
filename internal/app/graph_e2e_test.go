package app

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/config"
)

// 上游历史行可能带 RFC3339 不接受的 created_at。时间字段只影响展示：详情与
// graph/memory 都必须 200，createTime 为空串，正文照常返回，不能出现
// “列表能看、详情 502”。
func TestE2EEventMemoryDirtyCreatedAtIsDisplayOnly(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_memory/m1" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{
			"id":"m1","memory":"完整正文","metadata":{
				"mem_cube_id":"cube-a","created_at":"2026-10-18 09:10:38.097+0800",
				"user_id":"alice","memory_type":"Context"
			}
		}}`))
	}))
	t.Cleanup(upstream.Close)

	e := newE2EConfig(t, func(cfg *config.Config) { cfg.Upstream.BaseURL = upstream.URL })
	root := e.client()
	expect(t, "root login", root.login(e2eSeedUser, e2eSeedPass), http.StatusOK)

	detail := root.post("/api/dashboard/project/memory/detail",
		`{"projectId":"cube-a","memoryId":"m1","memType":"event_mem"}`)
	expect(t, "event detail", detail, http.StatusOK)
	if detail.str("memoryContent") != "完整正文" || detail.str("createTime") != "" {
		t.Fatalf("脏 created_at 只应让 createTime 为空: %s", detail.raw)
	}

	graph := root.get("/api/dashboard/graph/memory?id=m1&tenant=cube-a")
	expect(t, "graph memory", graph, http.StatusOK)
	if graph.str("memory") != "完整正文" {
		t.Fatalf("graph/memory 不应受脏时间影响: %s", graph.raw)
	}
}
