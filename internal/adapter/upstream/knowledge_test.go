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

func TestKnowledgeBaseCloudCalls(t *testing.T) {
	t.Parallel()

	var seen []map[string]any
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]any
		_ = json.NewDecoder(r.Body).Decode(&req)
		seen = append(seen, req)
		switch r.URL.Path {
		case "/product/get_knowledgebase":
			if req["knowledgebase_id"] != nil {
				_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"knowledgebase_id":"kb-1","knowledgebase_name":"手册","file_count":2,"total_size":30,"created_at":"2026-10-24T08:00:00+00:00"}}`)
				return
			}
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"knowledgebase_list":[
				{"knowledgebase_id":"kb-1","knowledgebase_name":"手册","file_count":2,"total_size":30}],"total":1}}`)
		case "/product/create_knowledgebase":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"knowledgebase_id":"kb-new","knowledgebase_name":"新库"}}`)
		case "/product/update_knowledgebase":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"knowledgebase_id":"kb-1","knowledgebase_name":"改名"}}`)
		case "/product/delete_knowledgebase":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"success":true}}`)
		case "/product/get_knowledgebase_file":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"file_detail_list":[
				{"id":"file-1","name":"a.txt","size":1048576,"knowledgebase_id":"kb-1","created_at":"2026-10-24T09:30:00+00:00"}],"total":1}}`)
		case "/product/add_knowledgebase_file":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":[{"id":"file-new","name":"b.md","knowledgebase_id":"kb-1"}]}`)
		case "/product/delete_knowledgebase_file":
			_, _ = io.WriteString(w, `{"code":0,"message":"ok","data":{"success":true,"deleted":2}}`)
		default:
			_, _ = io.WriteString(w, `{"code":404,"message":"not found","data":null}`)
		}
	})
	c := newTestClient(t, handler, 1<<20)
	ctx := context.Background()

	bases, err := c.KnowledgeBases(ctx, "cube-a")
	if err != nil || len(bases) != 1 || bases[0].ID != "kb-1" || bases[0].FileCount != 2 {
		t.Fatalf("list = %+v, %v", bases, err)
	}
	one, err := c.KnowledgeBase(ctx, "cube-a", "kb-1")
	if err != nil || one.Name != "手册" || one.CreatedAt.IsZero() {
		t.Fatalf("get = %+v, %v", one, err)
	}
	created, err := c.CreateKnowledgeBase(ctx, "cube-a", "新库", "描述")
	if err != nil || created.ID != "kb-new" {
		t.Fatalf("create = %+v, %v", created, err)
	}
	name := "改名"
	updated, err := c.UpdateKnowledgeBase(ctx, domain.KnowledgeBaseUpdate{TenantID: "cube-a", ID: "kb-1", Name: &name})
	if err != nil || updated.Name != "改名" {
		t.Fatalf("update = %+v, %v", updated, err)
	}
	if _, ok := seen[3]["knowledgebase_description"]; ok {
		t.Fatalf("未提供的描述字段不应发出: %v", seen[3])
	}
	if err := c.DeleteKnowledgeBase(ctx, "cube-a", "kb-1"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	files, err := c.KnowledgeFiles(ctx, domain.KnowledgeFilesQuery{TenantID: "cube-a", KnowledgeBaseID: "kb-1", Page: 1, Size: 10})
	if err != nil || files.Total != 1 || files.Items[0].SizeMB != 1 {
		t.Fatalf("files = %+v, %v", files, err)
	}
	added, err := c.AddKnowledgeFiles(ctx, domain.KnowledgeFilesAdd{TenantID: "cube-a", KnowledgeBaseID: "kb-1", Files: []domain.KnowledgeFileInput{
		{Name: "b.md", Content: "data:text/markdown;base64,aGk="},
	}})
	if err != nil || len(added) != 1 || added[0].ID != "file-new" {
		t.Fatalf("add = %+v, %v", added, err)
	}
	if seen[len(seen)-1]["mem_cube_id"] != "cube-a" {
		t.Fatalf("add 必须带租户: %v", seen[len(seen)-1])
	}
	deleted, err := c.DeleteKnowledgeFiles(ctx, "cube-a", []string{"file-1", "file-2"})
	if err != nil || deleted != 2 {
		t.Fatalf("delete files = %d, %v", deleted, err)
	}
}

func TestKnowledgeBaseNotFoundAndDownload(t *testing.T) {
	t.Parallel()

	handler := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Disposition", `attachment; filename="a.txt"; filename*=UTF-8''%E6%89%8B%E5%86%8C.txt`)
		_, _ = io.WriteString(w, "hello")
	})
	c := newTestClient(t, handler, 1<<20)
	content, name, err := c.DownloadKnowledgeFile(context.Background(), "cube-a", "file-1")
	if err != nil || string(content) != "hello" || name != "手册.txt" {
		t.Fatalf("download = %q %q %v", content, name, err)
	}

	missing := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"code":404,"message":"knowledgebase not found","data":null}`)
	}), 1<<20)
	if _, err := missing.KnowledgeBase(context.Background(), "cube-a", "kb-x"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("不存在应映射 404: %v", err)
	}
	if err := missing.DeleteKnowledgeBase(context.Background(), "cube-a", "kb-x"); !errors.Is(err, domain.ErrNotFound) {
		t.Fatalf("删除不存在应映射 404: %v", err)
	}
}
