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

func TestUpdateMemoryScopeAndRejection(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, reply string
		rejected    bool
	}{
		{"saved", `{"code":200,"data":{"success":true}}`, false},
		{"false success", `{"code":200,"data":{"success":false}}`, true},
		{"missing result", `{"code":200,"data":{}}`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				if r.URL.Path != "/product/update_memory" || body["mem_cube_id"] != "cube" || body["user_id"] != "user" || body["content"] != "new" || body["memory_id"] != "m" {
					t.Errorf("bad request: %v %v", r.URL, body)
				}
				_, _ = io.WriteString(w, tc.reply)
			}), 1<<20)
			err := c.UpdateMemory(context.Background(), domain.MemoryUpdate{ID: "m", TenantID: "cube", UserID: "user", Content: "new"})
			if tc.rejected != errors.Is(err, domain.ErrUpstreamRejected) {
				t.Fatalf("err=%v", err)
			}
		})
	}
}

func TestUpdateInvalidatesOnlyChangedContentTags(t *testing.T) {
	t.Parallel()
	for _, changed := range []bool{false, true} {
		t.Run(map[bool]string{true: "content", false: "title only"}[changed], func(t *testing.T) {
			t.Parallel()
			c := newTestClient(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				metadata, has := body["metadata"].(map[string]any)
				if has != changed {
					t.Errorf("unexpected metadata: %+v", body)
				}
				if changed {
					tags, ok := metadata["tags"].([]any)
					if !ok || len(tags) != 0 {
						t.Errorf("stale tags retained: %+v", metadata)
					}
				}
				_, _ = io.WriteString(w, `{"code":200,"data":{"success":true}}`)
			}), 1<<20)
			if err := c.UpdateMemory(t.Context(), domain.MemoryUpdate{ID: "m", TenantID: "cube", UserID: "user", Content: "new", ResetContentTags: changed}); err != nil {
				t.Fatal(err)
			}
		})
	}
}
