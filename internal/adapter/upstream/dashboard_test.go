package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

func TestDashboardProjectsWhitelist(t *testing.T) {
	t.Parallel()
	var got map[string]any
	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/product/get_memory_dashboard" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = io.WriteString(w, `{"code":200,"message":"ok","data":{
			"text_mem":[],
			"pref_mem":[{"cube_id":"cube-a","total_nodes":4,"memories":[
				{"id":"a","memory":"偏好一","metadata":{"memory_type":"PreferenceMemory","status":"activated","created_at":"2026-10-18T23:00:31+00:00","user_id":"alice","sources":[{"role":"user","content":"很长"}],"embedding":[0.1,0.2],"nested":{"x":[1,2,3]}}},
				{"id":"a","memory":"重复","metadata":{}},
				{"id":"d","memory":"已删","metadata":{"status":"deleted"}},
				{"id":"f","memory":"别人的","metadata":{"mem_cube_id":"cube-b"}},
				{"id":"n","memory":"数字字段","metadata":{"user_id":42,"created_at":null,"key":{"x":1}}}
			]},{"cube_id":"cube-b","memories":[{"id":"z","memory":"别的分组","metadata":{}}]}],
			"tool_mem":"not-a-list",
			"statistics":{"total_preference_nodes":594,"total_text_nodes":3}
		}}`)
	}), 1<<20)

	page, err := c.Dashboard(context.Background(), domain.DashboardQuery{
		TenantID: "cube-a", Bucket: domain.DashboardBucketPref, Page: 2, PageSize: 500,
		Filter: map[string]any{"status": "activated"},
	})
	if err != nil {
		t.Fatalf("Dashboard: %v", err)
	}
	if got["mem_cube_id"] != "cube-a" || got["page"] != float64(2) || got["page_size"] != float64(100) ||
		got["include_preference"] != any(true) || got["include_tool_memory"] != any(false) {
		t.Fatalf("payload = %v", got)
	}
	if filter, _ := got["filter"].(map[string]any); filter["status"] != "activated" {
		t.Fatalf("filter 应原样透传: %v", got["filter"])
	}
	if page.Total != 594 {
		t.Fatalf("total 应取 statistics: %d", page.Total)
	}
	ids := make([]string, 0, len(page.Rows))
	for _, row := range page.Rows {
		ids = append(ids, row.ID)
	}
	if strings.Join(ids, ",") != "a,n" {
		t.Fatalf("应只保留本租户、未删除、去重后的 a 和 n: %v", ids)
	}
	if first := page.Rows[0]; first.Content != "偏好一" || first.NodeType != "PreferenceMemory" ||
		first.UserID != "alice" || first.CreatedAt != "2026-10-18T23:00:31+00:00" {
		t.Fatalf("白名单投影不对: %+v", first)
	}
	if odd := page.Rows[1]; odd.UserID != "42" || odd.CreatedAt != "" {
		t.Fatalf("形状异常的字段应宽松处理: %+v", odd)
	}
}

// 真实规模的回归：偏好桶一页 148 MB，metadata.sources 占绝大部分。
// 响应由流式生成，测试进程自己也不会持有整个响应。
func TestDashboardStreamsHugeResponse(t *testing.T) {
	const (
		memories     = 12
		sourceChunks = 200
		chunkBytes   = 64 << 10
		wantBytes    = 148 << 20
	)
	chunk := strings.Repeat("s", chunkBytes)
	var sent atomic.Int64
	c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		write := func(s string) {
			n, _ := io.WriteString(w, s)
			sent.Add(int64(n))
		}
		write(`{"code":200,"message":"ok","data":{"pref_mem":[{"cube_id":"cube-a","memories":[`)
		for i := range memories {
			if i > 0 {
				write(",")
			}
			write(fmt.Sprintf(`{"id":"m%d","memory":"正文%d","metadata":{"memory_type":"PreferenceMemory","status":"activated","created_at":"2026-10-18T23:00:31","user_id":"alice","sources":[`, i, i))
			for j := range sourceChunks {
				if j > 0 {
					write(",")
				}
				write(`"` + chunk + `"`)
			}
			write(`],"embedding":[0.1,0.2,0.3]}}`)
		}
		write(`]}],"statistics":{"total_preference_nodes":594}}}`)
	}), 8<<20)
	c.http.Timeout = 2 * time.Minute

	runtime.GC()
	var base runtime.MemStats
	runtime.ReadMemStats(&base)
	var peak atomic.Uint64
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		var ms runtime.MemStats
		for {
			select {
			case <-stop:
				return
			case <-time.After(5 * time.Millisecond):
				runtime.ReadMemStats(&ms)
				if ms.HeapAlloc > peak.Load() {
					peak.Store(ms.HeapAlloc)
				}
			}
		}
	}()

	page, err := c.Dashboard(context.Background(), domain.DashboardQuery{
		TenantID: "cube-a", Bucket: domain.DashboardBucketPref, Page: 1, PageSize: memories,
	})
	close(stop)
	<-done
	if err != nil {
		t.Fatalf("Dashboard: %v", err)
	}
	if sent.Load() < wantBytes {
		t.Fatalf("测试响应只有 %d 字节，达不到 148 MB 级", sent.Load())
	}
	if len(page.Rows) != memories || page.Total != 594 {
		t.Fatalf("rows=%d total=%d", len(page.Rows), page.Total)
	}
	c.dashboardMax = 1 << 20
	if _, err := c.Dashboard(context.Background(), domain.DashboardQuery{TenantID: "cube-a", Bucket: domain.DashboardBucketPref}); !errors.Is(err, domain.ErrUpstreamRejected) {
		t.Fatalf("超过原始字节上限应拒绝而不是截断: %v", err)
	}
	growth := int64(peak.Load()) - int64(base.HeapAlloc)
	t.Logf("响应 %d MiB，堆峰值增长 %d MiB", sent.Load()>>20, growth>>20)
	if growth > 48<<20 {
		t.Fatalf("流式解码的堆峰值增长 %d MiB，不应与响应体积成正比", growth>>20)
	}
}
