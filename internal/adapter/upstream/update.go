package upstream

import (
	"context"
	"encoding/json"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// UpdateMemory 的租户与主体均由控制面确定，不接受调用方覆盖。
func (c *Client) UpdateMemory(ctx context.Context, in domain.MemoryUpdate) error {
	payload := map[string]any{"memory_id": in.ID, "content": in.Content, "mem_cube_id": in.TenantID, "user_id": in.UserID}
	if in.Title != nil {
		payload["title"] = *in.Title
	}
	if in.ResetContentTags {
		payload["metadata"] = map[string]any{"tags": []string{}}
	}
	env, err := c.post(ctx, "/product/update_memory", payload)
	if err != nil {
		return err
	}
	var data struct {
		Success bool `json:"success"`
	}
	if json.Unmarshal(env.Data, &data) != nil || !data.Success {
		return domain.Wrap(domain.ErrUpstreamRejected, "update", "修改未完成，请稍后重试", nil)
	}
	return nil
}
