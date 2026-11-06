package dashboard

import (
	"context"
	"maps"
	"net/http"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

const (
	// memorySearchTopK 取上游契约的 top_k 上限（按桶生效）。上游 /product/search
	// 没有 offset，检索页签每桶最多只能看到这么多条。
	memorySearchTopK = domain.ContractTopKMax
	memoryPageMax    = 100
	memoryPageDef    = 50
)

// searchPageInput 是上游分页检索的入参。
type searchPageInput struct {
	Tenant domain.Tenant
	Tab    memoryTab
	Page   int
	Size   int
	Query  string
	Filter map[string]any
}

// memoryTab 是控制台一个页签的数据来源：上游桶 + 过滤条件，或者图库的节点类型。
// 上游只有 4 个桶且桶里混装多种类型，所以文本桶要再按 memory_type 过滤。
type memoryTab struct {
	bucket     domain.DashboardBucket
	filter     map[string]any
	graphTypes []string
}

var memoryTabs = map[string]memoryTab{
	"text_mem": {
		bucket: domain.DashboardBucketText,
		filter: map[string]any{"memory_type": "LongTermMemory", "status": "activated"},
	},
	"pref_mem": {bucket: domain.DashboardBucketPref, filter: map[string]any{"status": "activated"}},
	"tool_mem": {bucket: domain.DashboardBucketTool, filter: map[string]any{"status": "activated"}},
	// 用户记忆走图库：上游桶对它一页能到 500 MB，图库同一批数据只有几 KB，条数一致。
	"profile_mem": {graphTypes: []string{"UserMemory"}},
	// 事件记忆里的 Context / InsightMemory 只写图库，上游任何入口都读不到。
	"event_mem": {graphTypes: []string{"EventMemory", "Context", "InsightMemory"}},
}

func (s *Server) memoryList(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	page := num.Clamp(httpx.Int(body["pageNum"], 1), 1, maxPageNum)
	size := num.Clamp(httpx.Int(body["pageSize"], memoryPageDef), 1, memoryPageMax)
	tabName := strings.TrimSpace(httpx.String(body["memoryDetailType"]))
	if tabName == "" {
		tabName = "text_mem"
	}
	tab, known := memoryTabs[tabName]
	if !known {
		return nil, domain.Invalid("dashboard", "不支持的记忆类型："+tabName)
	}
	// 知识库不在本期范围：明确告知能力未启用，而不是静默返回空列表。
	if strings.TrimSpace(httpx.String(body["knowledgeBaseId"])) != "" {
		return nil, disabledCapability("知识库")
	}
	tenant, err := s.deps.Tenants.Get(ctx, strings.TrimSpace(httpx.String(body["projectId"])))
	if err != nil {
		return nil, err
	}
	req := memoryListRequest{
		tenant: tenant, tab: tab, name: tabName, page: page, size: size,
		query:  strings.TrimSpace(httpx.String(body["memSearchContent"])),
		userID: strings.TrimSpace(httpx.String(body["externalUserId"])),
	}
	filter := req.upstreamFilter()
	var (
		rows  []domain.DashboardRow
		total int
	)
	switch {
	case len(tab.graphTypes) > 0:
		rows, total, err = s.graphRows(ctx, req)
	case req.query != "":
		rows, total, err = s.searchPage(ctx, searchPageInput{
			Tenant: req.tenant, Tab: tab, Page: page, Size: size, Query: req.query, Filter: filter,
		})
	default:
		rows, total, err = s.bucketRows(ctx, req, filter)
	}
	if err != nil {
		return nil, err
	}
	subject := strings.TrimSpace(httpx.String(body["subjectType"]))
	items := make([]map[string]any, 0, len(rows))
	for i := range rows {
		item := memoryItemJSON(rows[i], tenant.ID, tabName)
		if subject == "" || item["subjectType"] == subject {
			items = append(items, item)
		}
	}
	return memoryListResponse(items, total, page), nil
}

type memoryListRequest struct {
	tenant     domain.Tenant
	tab        memoryTab
	name       string
	page, size int
	query      string
	userID     string
}

func memoryListResponse(items []map[string]any, total, page int) map[string]any {
	if items == nil {
		items = []map[string]any{}
	}
	return map[string]any{"memListItemResponseList": items, "total": total, "current": page}
}

// upstreamFilter 是本页签下推到上游的过滤条件；请求带 externalUserId 时追加 user_id。
// 桶页签与检索页签共用同一份条件。注意它不等同于 pre-filter：上游先在向量库按
// scope→memory_type、status、user_name 预过滤取回候选，再在 Cypher 里对候选应用
// 这份 filter，所以最终条数可能少于 top_k。
func (req memoryListRequest) upstreamFilter() map[string]any {
	filter := maps.Clone(req.tab.filter)
	if filter == nil {
		filter = make(map[string]any, 1)
	}
	if req.userID != "" {
		filter["user_id"] = req.userID
	}
	return filter
}

// bucketRows 走服务端过滤 + 真分页，总数取上游 statistics（随过滤条件变化，不是 total_nodes）。
func (s *Server) bucketRows(
	ctx context.Context,
	req memoryListRequest,
	filter map[string]any,
) ([]domain.DashboardRow, int, error) {
	if s.deps.Memories == nil {
		return nil, 0, disabledCapability("上游")
	}
	page, err := s.deps.Memories.Dashboard(ctx, domain.DashboardQuery{
		TenantID: req.tenant.ID, Bucket: req.tab.bucket, Page: req.page, PageSize: req.size, Filter: filter,
	})
	if err != nil {
		return nil, 0, err
	}
	return page.Rows, page.Total, nil
}

// searchPage 是「上游检索结果 → 本页行 + total」的唯一落点：把 filter 随请求下推，
// 本地再按同一条件兜底过滤与切片。将来上游提供 offset 时，只改这里与适配层的
// 请求参数，不在各页签重复实现分页。
//
// 上游的过滤分两个阶段：向量库按 scope→memory_type、status、user_name 预过滤并取回
// 最多 memorySearchTopK 个候选，随后在 Cypher 里对候选应用本请求的 filter。
// 因此 filter 不是严格意义的 pre-filter，最终命中可能少于 top_k。
//
// 已知限制：上游 /product/search 只有 top_k、没有 offset，每桶最多 memorySearchTopK 条；
// total 是本次检索命中的条数，不是全量匹配数，超过 25 条的匹配无法翻页。
func (s *Server) searchPage(ctx context.Context, in searchPageInput) ([]domain.DashboardRow, int, error) {
	tenant, tab, page, size := in.Tenant, in.Tab, in.Page, in.Size
	query, filter := in.Query, in.Filter
	result, err := s.deps.Memory.Search(ctx,
		domain.Caller{TenantID: tenant.ID, UserID: tenant.UserID},
		domain.SearchQuery{Text: query, TopK: memorySearchTopK, Filter: filter})
	if err != nil {
		return nil, 0, err
	}
	var hits []domain.Memory
	switch tab.bucket {
	case domain.DashboardBucketText:
		hits = result.Facts
	case domain.DashboardBucketPref:
		hits = result.Preferences
	case domain.DashboardBucketTool:
		hits = result.Tools
	}
	rows := make([]domain.DashboardRow, 0, len(hits))
	for i := range hits {
		m := hits[i]
		// 旧镜像可能忽略 filter：本地按同一组条件再过滤一次，避免未激活/异类型记录漏到本页。
		if !matchesFilter(m, filter) {
			continue
		}
		rows = append(rows, domain.DashboardRow{
			ID: m.ID, Content: m.Content, NodeType: m.NodeType, UserID: m.UserID, CreatedAt: m.CreatedAt.String(),
		})
	}
	start := min((page-1)*size, len(rows))
	return rows[start:min(start+size, len(rows))], len(rows), nil
}

// matchesFilter 只比较下推过的三个键；上游 filter 生效（预过滤或候选后置应用）时
// 它们本就不会出现在结果里，这里只兜住忽略 filter 的旧镜像。
func matchesFilter(m domain.Memory, filter map[string]any) bool {
	if status, ok := filter["status"].(string); ok && status != "" && m.Status != status {
		return false
	}
	if memoryType, ok := filter["memory_type"].(string); ok && memoryType != "" && m.NodeType != memoryType {
		return false
	}
	if userID, ok := filter["user_id"].(string); ok && userID != "" && m.UserID != userID {
		return false
	}
	return true
}

// graphRows 读事件记忆：上游 `/product/get_event_memories` 做计数、排序与真分页，
// 搜索只匹配正文子串。user_id 取上游返回的 metadata.user_id（记忆归属主体），
// 所以 externalUserId 与 subjectType 能区分租户自身与终端用户。
func (s *Server) graphRows(ctx context.Context, req memoryListRequest) ([]domain.DashboardRow, int, error) {
	if s.deps.Graph == nil {
		return nil, 0, disabledCapability("事件记忆")
	}
	found, total, err := s.deps.Graph.EventMemories(ctx, domain.EventMemoriesQuery{
		TenantID: req.tenant.ID,
		Types:    req.tab.graphTypes,
		Query:    req.query,
		Page:     req.page,
		Size:     req.size,
	})
	if err != nil {
		return nil, 0, err
	}
	rows := make([]domain.DashboardRow, 0, len(found))
	for i := range found {
		if req.userID != "" && found[i].UserID != req.userID {
			continue
		}
		rows = append(rows, domain.DashboardRow{
			ID: found[i].ID, Content: found[i].Content, NodeType: found[i].Type,
			UserID: found[i].UserID, CreatedAt: found[i].CreatedAt,
		})
	}
	return rows, total, nil
}

// memoryItemJSON 是列表一行的白名单输出。画像字段只对相应类型下发。
func memoryItemJSON(row domain.DashboardRow, tenantID, tab string) map[string]any {
	userID := row.UserID
	if userID == "" {
		userID = tenantID
	}
	item := map[string]any{
		"memoryId":       row.ID,
		"memoryContent":  row.Content,
		"subjectType":    subjectTypeOf(userID, tenantID),
		"externalUserId": userID,
		"createTime":     domain.BeijingClock(row.CreatedAt),
	}
	if row.NodeType != "" {
		item["memoryType"] = row.NodeType
	}
	if row.NodeType == "UserMemory" || tab == "profile_mem" {
		setIfPresent(item, "profileTemplateId", row.TemplateID)
		setIfPresent(item, "profileInstanceId", row.InstanceID)
	}
	return item
}

func setIfPresent(item map[string]any, key, value string) {
	if value != "" {
		item[key] = value
	}
}

// subjectTypeOf：上游没有主体类型字段，只能按 user_id 与租户的关系推。
func subjectTypeOf(userID, tenantID string) string {
	switch userID {
	case "":
		return "public"
	case tenantID:
		return "agent"
	default:
		return "user"
	}
}
