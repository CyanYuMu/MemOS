package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"testing"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// cloud 风格接口：成功 code 为 0，不存在是 code 404（HTTP 仍是 200）；租户边界靠 mem_cube_id。
func TestProfileTemplatesCloudEnvelope(t *testing.T) {
	t.Parallel()

	var seen []map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]any
		_ = json.NewDecoder(r.Body).Decode(&req)
		seen = append(seen, req)
		switch r.URL.Path {
		case "/product/list_profile_template":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"profile_template_list":[
				{"profile_template_id":"t1","name":"画像","metadata":{"基础":{"年龄":"int"}},"bound_count":2,"updated_at":"2026-10-21T08:00:00Z"}],"total":1}}`)
		case "/product/update_profile_template":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"profile_template_id":"t1","success":true}}`)
		default:
			_, _ = io.WriteString(w, `{"code":404,"message":"profile template not found","data":null}`)
		}
	})
	c := newTestClient(t, handler, 1<<20)
	ctx := context.Background()

	page, err := c.ListProfileTemplates(ctx, "cube-a", 1, 10)
	if err != nil || page.Total != 1 || page.Items[0].ID != "t1" || page.Items[0].BoundCount != 2 || page.Items[0].Metadata["基础"] == nil {
		t.Fatalf("list = %+v, %v", page, err)
	}
	if seen[0]["mem_cube_id"] != "cube-a" {
		t.Fatalf("租户必须经 mem_cube_id 下发: %v", seen[0])
	}

	name := "新名字"
	if _, err := c.UpdateProfileTemplate(ctx, "cube-a", "t1", domain.ProfileTemplateUpdate{Name: &name}); err != nil {
		t.Fatalf("update: %v", err)
	}
	if _, has := seen[1]["metadata"]; has {
		t.Fatalf("仅改名不能带 metadata，否则上游会清空属性树: %v", seen[1])
	}

	if _, err := c.GetProfileTemplate(ctx, "cube-a", "other"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("code 404 应映射为 ErrNotFound: %v", err)
	}
	if err := c.DeleteProfileTemplate(ctx, "cube-a", "other"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("删除不存在的模板应为 ErrNotFound: %v", err)
	}
}
