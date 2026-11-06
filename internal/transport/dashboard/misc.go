package dashboard

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
)

// meta 未登录只给公开子集；登录后才多给 sources（数据源配置状态）。
func (s *Server) meta(ctx context.Context, p domain.Principal, _ *http.Request, _ map[string]any) (any, error) {
	out := map[string]any{
		"mode":        "live",
		"service":     s.deps.Service,
		"keyPrefix":   domain.RawTokenPrefix,
		"tools":       domain.GatewayTools(),
		"authEnabled": true,
		"authMode":    s.deps.AuthMode,
	}
	if p.Kind != "" {
		sources, upstreamHealth := s.sourceStatus(ctx, p)
		out["sources"] = sources
		out["health"] = upstreamHealth
		// 前端在另一个仓库，只以产物形式进镜像；这里把它的分支/提交一并报出来，
		// 排查「线上页面是哪版」时不用再比对 asset hash。
		if s.frontend != nil {
			out["frontend"] = s.frontend
		}
	}
	return out, nil
}

// readFrontendMeta 读 `web/dist/frontend.json`（`make web` 生成）。
//
// 这个文件只是给排查用的附加信息：缺失、损坏、字段变了都不能影响控制台启动，
// 所以一律吞掉错误，返回 nil，/api/meta 里就不出现 frontend 字段。
func readFrontendMeta(webDir string) map[string]any {
	webDir = strings.TrimSpace(webDir)
	if webDir == "" {
		return nil
	}
	raw, err := os.ReadFile(filepath.Join(webDir, "frontend.json"))
	if err != nil {
		return nil
	}
	var meta map[string]any
	if err := json.Unmarshal(raw, &meta); err != nil {
		return nil
	}
	return meta
}

// sourceStatus 探测各数据源的真实状态，并返回上游 /health 的原始 JSON 给 /api/meta.health。
// 探测有 2s 上限，避免上游故障时拖死 /api/meta。密钥数与审计条数是全库统计，
// 只给平台管理员看；普通用户拿到的 sources 不含全库计数。
func (s *Server) sourceStatus(ctx context.Context, p domain.Principal) (map[string]any, map[string]any) {
	probeCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	sources := make(map[string]any, 4)

	// 两个探针并行；共用一个 2s 截止时间，串行时上游超时会把图库误判成超时。
	var (
		wg                          sync.WaitGroup
		upstreamStatus, graphStatus string
		upstreamHealth              map[string]any
	)
	wg.Add(2)
	go func() {
		defer wg.Done()
		if s.deps.Memories == nil {
			upstreamStatus = "disabled"
			return
		}
		health, err := s.deps.Memories.Health(probeCtx)
		if err != nil {
			upstreamStatus = "unreachable: " + domain.SafeMessage(err)
			return
		}
		if health == nil {
			health = map[string]any{"status": "ok"}
		}
		upstreamHealth = health
		upstreamStatus = "ok"
	}()
	go func() {
		defer wg.Done()
		if s.deps.Graph == nil {
			graphStatus = "disabled"
		} else if err := s.deps.Graph.Ping(probeCtx); err != nil {
			graphStatus = "unreachable: " + domain.SafeMessage(err)
		} else {
			graphStatus = "ok"
		}
	}()
	wg.Wait()
	sources["upstream"] = upstreamStatus
	sources["graph"] = graphStatus

	if p.PlatformAdmin {
		s.addAdminSourceCounts(ctx, sources)
	}
	return sources, upstreamHealth
}

// addAdminSourceCounts 补全库维度的密钥数与审计条数；只给平台管理员。
func (s *Server) addAdminSourceCounts(ctx context.Context, sources map[string]any) {
	tokens, err := s.deps.Tokens.ListTokens(ctx, "")
	if err != nil {
		sources["keys"] = "unavailable: " + domain.SafeMessage(err)
	} else {
		active := 0
		for i := range tokens {
			if tokens[i].RevokedAt == nil {
				active++
			}
		}
		sources["keys"] = fmt.Sprintf("%d 把（控制面 SQLite）", active)
	}

	if s.deps.Audit == nil {
		sources["callLog"] = "disabled"
	} else if _, total, err := s.deps.Audit.List(ctx, domain.AuditFilter{Limit: 1}); err != nil {
		sources["callLog"] = "unavailable: " + domain.SafeMessage(err)
	} else {
		sources["callLog"] = fmt.Sprintf("%d 条（控制面 SQLite 审计表）", total)
	}
}

func (s *Server) quota(context.Context, domain.Principal, *http.Request, map[string]any) (any, error) {
	// 本部署没有配额概念。enabled=false 让调用方能区分“未启用”和“真实用量为 0”；
	// 原有零值字段保留，避免旧前端读不到键。
	return map[string]any{
		"enabled":                   false,
		"memoryCount":               0,
		"knowledgebaseCount":        0,
		"knowledgebaseCapacity":     0,
		"allowedKnowledgebaseCount": 0,
	}, nil
}

func (s *Server) subscription(context.Context, domain.Principal, *http.Request, map[string]any) (any, error) {
	return map[string]any{"enabled": false, "cycles": []any{}, "currentCycle": nil}, nil
}

// models 的实现见 prompt.go：模型清单由上游进程自己的配置生成。

func (s *Server) graphMemories(
	ctx context.Context,
	_ domain.Principal,
	r *http.Request,
	_ map[string]any,
) (any, error) {
	if s.deps.Graph == nil {
		return nil, disabledCapability("图库")
	}
	query := r.URL.Query()
	tenant := strings.TrimSpace(query.Get("tenant"))
	if tenant == "" {
		return nil, domain.Invalid("dashboard", "缺少 tenant")
	}
	limit := s.deps.GraphMaxNodes
	if raw := query.Get("limit"); raw != "" {
		if parsed, err := strconv.Atoi(raw); err == nil {
			limit = num.Clamp(parsed, 1, s.deps.GraphMaxNodes)
		}
	}
	types := splitCSV(query.Get("types"))
	page, err := s.deps.Graph.GraphMemories(ctx, tenant, types, limit)
	if err != nil {
		return nil, err
	}
	return buildGraph(page, tenant), nil
}

func (s *Server) graphMemory(ctx context.Context, _ domain.Principal, r *http.Request, _ map[string]any) (any, error) {
	if s.deps.Graph == nil {
		return nil, disabledCapability("图库")
	}
	query := r.URL.Query()
	id := strings.TrimSpace(query.Get("id"))
	tenant := strings.TrimSpace(query.Get("tenant"))
	if id == "" || tenant == "" {
		return nil, domain.Invalid("dashboard", "id 与 tenant 不能为空")
	}
	mem, err := s.deps.Graph.MemoryBody(ctx, tenant, id)
	if err != nil {
		return nil, err
	}
	return map[string]any{"id": id, "memory": mem.Content}, nil
}

func buildGraph(page domain.GraphPage, tenant string) map[string]any {
	nodes := page.Nodes
	edges := page.Edges
	nodeOut := make([]map[string]any, 0, len(nodes))
	ids := make(map[string]struct{}, len(nodes))
	sessions := map[string]struct{}{}
	typeCount := map[string]int{}
	for i := range nodes {
		node := &nodes[i]
		ids[node.ID] = struct{}{}
		if node.SessionID != "" {
			sessions[node.SessionID] = struct{}{}
		}
		typeCount[node.Type]++
		nodeOut = append(nodeOut, map[string]any{
			"id":             node.ID,
			"label":          node.Content,
			"labelTruncated": node.Truncated,
			"memoryType":     node.Type,
			"createdAt":      node.CreatedAt,
			"sessionId":      node.SessionID,
			"clusterId":      "session:" + node.SessionID,
			"userId":         node.UserID,
			"sourceIds":      node.SourceIDs,
		})
	}
	linkOut := make([]map[string]any, 0, len(edges))
	linked := make(map[string]struct{}, len(nodes))
	for _, edge := range edges {
		_, okSource := ids[edge.Source]
		_, okTarget := ids[edge.Target]
		if !okSource || !okTarget {
			continue
		}
		linked[edge.Source] = struct{}{}
		linked[edge.Target] = struct{}{}
		linkOut = append(linkOut, map[string]any{"source": edge.Source, "target": edge.Target, "kind": strings.ToLower(edge.Kind)})
	}
	orphans := 0
	for id := range ids {
		if _, ok := linked[id]; !ok {
			orphans++
		}
	}
	type typeRow struct {
		name  string
		count int
	}
	rows := make([]typeRow, 0, len(typeCount))
	for name, count := range typeCount {
		rows = append(rows, typeRow{name: name, count: count})
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].count > rows[j].count })
	types := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		types = append(types, map[string]any{"name": row.name, "count": row.count})
	}
	return map[string]any{
		"tenant": tenant,
		"nodes":  nodeOut,
		"links":  linkOut,
		"stats": map[string]any{
			"nodes":     len(nodeOut),
			"links":     len(linkOut),
			"sessions":  len(sessions),
			"clusters":  len(sessions),
			"orphans":   orphans,
			"truncated": page.Truncated,
			"types":     types,
		},
	}
}

func splitCSV(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}
