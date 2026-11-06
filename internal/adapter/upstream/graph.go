package upstream

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
)

// 上游 `/product/get_memory_graph` 的 limit 上限是 10000，事件记忆的 page_size 上限是 200。
const (
	graphNodeMax    = 10_000
	graphEventMax   = 200
	graphLabelMax   = 200
	graphActiveStat = "activated"
)

// graphNode 是上游 `get_memory_graph` / `get_event_memories` 返回的节点投影。
// 字段名由 dumemos 的 `_graph_node_json` 固定，控制面只依赖这一层。
type graphNode struct {
	ID              string   `json:"id"`
	Memory          string   `json:"memory"`
	MemoryTruncated bool     `json:"memoryTruncated"`
	MemoryType      string   `json:"memoryType"`
	Status          string   `json:"status"`
	CreatedAt       string   `json:"createdAt"`
	SessionID       string   `json:"sessionId"`
	UserID          string   `json:"userId"`
	SourceMemoryIDs []string `json:"sourceMemoryIds"`
}

type graphData struct {
	Nodes      []graphNode `json:"nodes"`
	Edges      []graphEdge `json:"edges"`
	TotalNodes int         `json:"total_nodes"`
	Truncated  bool        `json:"truncated"`
}

type graphEdge struct {
	Source string `json:"source"`
	Target string `json:"target"`
	Type   string `json:"type"`
}

type eventData struct {
	Memories []graphNode `json:"memories"`
	Total    int         `json:"total"`
}

// Ping 探测上游 /health。图谱由 memos-api 提供，可达性与上游一致。
func (c *Client) Ping(ctx context.Context) error {
	_, err := c.Health(ctx)
	return err
}

// GraphMemories 调 `POST /product/get_memory_graph`，一次取回节点与 PARENT/MERGED_TO 边。
// 只返回 status=activated 的节点，与迁移前的图库直读口径一致。
func (c *Client) GraphMemories(
	ctx context.Context, tenant string, types []string, limit int,
) (domain.GraphPage, error) {
	if strings.TrimSpace(tenant) == "" {
		return domain.GraphPage{}, domain.Invalid("graph", "tenant 不能为空")
	}
	limit = num.Clamp(limit, 1, graphNodeMax)
	payload := map[string]any{
		"mem_cube_id":   tenant,
		"memory_types":  types,
		"status":        []string{graphActiveStat},
		"limit":         limit,
		"label_max":     graphLabelMax,
		"include_edges": true,
	}
	env, err := c.post(ctx, "/product/get_memory_graph", payload)
	if err != nil {
		return domain.GraphPage{}, err
	}
	var data graphData
	if err := json.Unmarshal(env.Data, &data); err != nil {
		return domain.GraphPage{}, domain.Wrap(domain.ErrUpstreamRejected, "graph", "记忆图谱返回了无法解析的响应", err)
	}
	page := domain.GraphPage{
		Nodes:     graphMemories(data.Nodes),
		Total:     data.TotalNodes,
		Truncated: data.Truncated,
	}
	for _, edge := range data.Edges {
		page.Edges = append(page.Edges, domain.GraphEdge{
			Source: edge.Source,
			Target: edge.Target,
			Kind:   edge.Type,
		})
	}
	return page, nil
}

// EventMemories 调 `POST /product/get_event_memories`，按 created_at 倒序真分页。
// 上游的 search_query 是大小写敏感的子串匹配。
func (c *Client) EventMemories(ctx context.Context, in domain.EventMemoriesQuery) ([]domain.GraphMemory, int, error) {
	if strings.TrimSpace(in.TenantID) == "" || len(in.Types) == 0 {
		return nil, 0, domain.Invalid("graph", "tenant 与类型不能为空")
	}
	in.Page = max(in.Page, 1)
	in.Size = num.Clamp(in.Size, 1, graphEventMax)
	payload := map[string]any{
		"mem_cube_id":  in.TenantID,
		"memory_types": in.Types,
		"status":       []string{graphActiveStat},
		"page":         in.Page,
		"page_size":    in.Size,
	}
	if in.Query = strings.TrimSpace(in.Query); in.Query != "" {
		payload["search_query"] = in.Query
	}
	env, err := c.post(ctx, "/product/get_event_memories", payload)
	if err != nil {
		return nil, 0, err
	}
	var data eventData
	if err := json.Unmarshal(env.Data, &data); err != nil {
		return nil, 0, domain.Wrap(domain.ErrUpstreamRejected, "graph", "事件记忆返回了无法解析的响应", err)
	}
	return graphMemories(data.Memories), data.Total, nil
}

// MemoryBody 通过 `GET /product/get_memory/{id}?mem_cube_id=` 读正文，并在同一次响应里
// 带回创建时间与归属主体（`metadata.created_at`/`user_id`/`memory_type`），
// 事件记忆详情因此不需要再打列表接口。
// created_at 只影响展示：缺失或无法解析（例如历史行写成 `... 09:10:38.097+0800`）
// 时 CreatedAt 为空串，正文与主体照常返回，不能因为一行脏时间让详情变 502。
// 上游按 mem_cube_id 过滤；响应里带租户字段且与请求不一致时按不存在处理。
func (c *Client) MemoryBody(ctx context.Context, tenant, id string) (domain.GraphMemory, error) {
	if strings.TrimSpace(tenant) == "" || strings.TrimSpace(id) == "" {
		return domain.GraphMemory{}, domain.Invalid("graph", "tenant 与 id 不能为空")
	}
	path := "/product/get_memory/" + url.PathEscape(id) + "?mem_cube_id=" + url.QueryEscape(tenant) + "&include_sources=true"
	env, err := c.do(ctx, http.MethodGet, path, nil)
	if err != nil {
		return domain.GraphMemory{}, err
	}
	rows, err := decodeMemoryList(env.Data)
	if err != nil {
		return domain.GraphMemory{}, err
	}
	for _, row := range rows {
		if row.ID != id || row.Memory == "" {
			continue
		}
		cube := firstNonEmpty(stringField(row.Metadata, "mem_cube_id"), stringField(row.Metadata, "cube_id"))
		if cube != "" && cube != tenant {
			continue
		}
		// 契约 v2 的 created_at 是真 UTC；解析失败时留空串，由展示层输出空。
		created, parseErr := domain.ParseUTC(stringField(row.Metadata, "created_at"))
		if parseErr != nil {
			created = domain.UTCTime{}
		}
		version, history, sources := memoryDetails(row.Metadata)
		return domain.GraphMemory{
			Version: version, History: history, Sources: sources, UpdatedAt: metadataTime(stringField(row.Metadata, "updated_at")),
			ID:        row.ID,
			Content:   row.Memory,
			Type:      stringField(row.Metadata, "memory_type"),
			UserID:    stringField(row.Metadata, "user_id"),
			CreatedAt: created.String(),
		}, nil
	}
	return domain.GraphMemory{}, domain.NotFound("graph", "记忆不存在")
}

func graphMemories(nodes []graphNode) []domain.GraphMemory {
	out := make([]domain.GraphMemory, 0, len(nodes))
	for i := range nodes {
		node := &nodes[i]
		out = append(out, domain.GraphMemory{
			ID:        node.ID,
			Content:   node.Memory,
			Type:      node.MemoryType,
			UserID:    node.UserID,
			SessionID: node.SessionID,
			CreatedAt: node.CreatedAt,
			SourceIDs: node.SourceMemoryIDs,
			Truncated: node.MemoryTruncated,
		})
	}
	return out
}
