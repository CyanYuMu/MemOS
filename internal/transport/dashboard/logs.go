package dashboard

import (
	"context"
	"encoding/json"
	"errors"
	"maps"
	"net/http"
	"slices"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

const maxUsageDays = 400

// eventTools 把控制台的事件类型对应到网关工具名。事件类型过滤仍以这 5 个
// 对外工具为主；scope=console 时接收完整的工具名（如 dashboard.project.create）。
var eventTools = map[string]string{
	"addMessage":   domain.ToolAddMessage,
	"searchMemory": domain.ToolSearchMemory,
	"getMemory":    domain.ToolGetUserProfile,
	"deleteMemory": domain.ToolDeleteMemory,
	"updateMemory": domain.ToolUpdateMemory,
	"addFeedback":  domain.ToolAddFeedback,
}

var (
	gatewayTools = slices.Sorted(maps.Values(eventTools))
	toolEvents   = invert(eventTools)
)

// 日志来源。默认 all：控制面自己记录的 Dashboard/Admin/登录操作与网关调用
// 一样是审计数据源，不能因为旧 BFF 只读 JSONL 就把它们藏起来。
const (
	logScopeAll     = "all"
	logScopeGateway = "gateway"
	logScopeConsole = "console"
)

// auditSources 是可按入口过滤的取值。`tool` 回答“调了什么”，`source` 回答
// “从哪进来的”：同一个 add_message 既可能来自 MCP，也可能来自记忆插件的
// /memory/add 门面，还可能是运维写脚本直连 REST，只看工具名分不出这几类流量。
// 空字符串不在这里：它表示“不按来源过滤”，历史行的 source 为空也无法被选中。
var auditSources = map[string]struct{}{
	domain.AuditSourceMCP:       {},
	domain.AuditSourcePlugin:    {},
	domain.AuditSourceREST:      {},
	domain.AuditSourceDashboard: {},
	domain.AuditSourceAdmin:     {},
	domain.AuditSourceAuth:      {},
}

func invert(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[v] = k
	}
	return out
}

// eventOf 把工具名翻成事件类型；控制面操作没有别名，直接用工具名展示。
func eventOf(tool string) string {
	if event := toolEvents[tool]; event != "" {
		return event
	}
	return tool
}

// consoleEvent 接受完整的控制面审计工具名，避免把未登记的事件类型静默当成空结果。
func consoleEvent(tool string) bool {
	return strings.HasPrefix(tool, "dashboard.") ||
		strings.HasPrefix(tool, "admin.") ||
		strings.HasPrefix(tool, "auth.")
}

// logFilter 把请求体翻成审计过滤条件。未知的事件类型/状态、非法时间都是参数
// 错误（400），不能静默返回空列表——否则前端会以为“这段时间没有调用”。
func logFilter(body map[string]any, projectID string, page, size int) (domain.AuditFilter, error) {
	f := domain.AuditFilter{
		TenantID: projectID, Text: strings.TrimSpace(httpx.String(body["query"])),
		// 后台终态行与请求行共用 call_id，列表里只展示请求行；终态行的耗时与
		// token 由存储层的派生状态合并进请求行。
		ExcludeTools: []string{domain.ToolAddMessageBackground},
		Limit:        size, Offset: (page - 1) * size,
	}
	switch scope := strings.TrimSpace(httpx.String(body["scope"])); scope {
	case "", logScopeAll:
	case logScopeGateway:
		f.Tools = slices.Clone(gatewayTools)
		f.ExcludeTools = nil
	case logScopeConsole:
		f.ExcludeTools = append(slices.Clone(gatewayTools), domain.ToolAddMessageBackground)
	default:
		return f, domain.Invalid("dashboard", "不支持的日志来源："+scope)
	}
	if event := strings.TrimSpace(httpx.String(body["source"])); event != "" {
		if _, ok := auditSources[event]; !ok {
			// 与事件类型/状态一致：非法取值是参数错误，静默返回空列表会让人以为
			// “这个入口这段时间没有调用”。
			return f, domain.Invalid("dashboard", "不支持的调用来源："+event)
		}
		f.Sources = []string{event}
	}
	if event := strings.TrimSpace(httpx.String(body["eventType"])); event != "" {
		switch tool, known := eventTools[event]; {
		case known:
			f.Tools = []string{tool}
			f.ExcludeTools = nil
		case consoleEvent(event):
			f.Tools = []string{event}
			f.ExcludeTools = nil
		default:
			return f, domain.Invalid("dashboard", "不支持的事件类型："+event)
		}
	}
	switch status := strings.TrimSpace(httpx.String(body["status"])); status {
	case "":
	case string(domain.AuditStatusSuccess):
		f.Status = domain.AuditStatusSuccess
	case string(domain.AuditStatusFailed):
		f.Status = domain.AuditStatusFailed
	case string(domain.AuditStatusRunning):
		// 异步 add 还没有终态行，且同一 call_id 的 job 仍在保留期内；状态由存储层判定。
		f.Status = domain.AuditStatusRunning
	default:
		return f, domain.Invalid("dashboard", "不支持的状态："+status)
	}
	// 日志的主体就是项目；指定了别的主体 id 就没有结果，subjectType=all 表示不按主体过滤。
	subjectType := strings.TrimSpace(httpx.String(body["subjectType"]))
	subjectID := strings.TrimSpace(httpx.String(body["subjectId"]))
	if subjectType != "all" && subjectID != "" && subjectID != projectID {
		return f, domain.Invalid("dashboard", "subjectId 与当前项目不一致")
	}
	start := strings.TrimSpace(httpx.String(body["startTime"]))
	if start != "" {
		from, ok := domain.ParseBeijingMinute(start)
		if !ok {
			return f, domain.Invalid("dashboard", "startTime 格式应为 YYYY-MM-DD HH:mm")
		}
		f.Since = from
	}
	end := strings.TrimSpace(httpx.String(body["endTime"]))
	if end != "" {
		to, ok := domain.ParseBeijingMinute(end)
		if !ok {
			return f, domain.Invalid("dashboard", "endTime 格式应为 YYYY-MM-DD HH:mm")
		}
		f.Until = to.Add(time.Minute)
	}
	return f, nil
}

func (s *Server) logList(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	if s.deps.Audit == nil {
		return nil, disabledCapability("审计")
	}
	page := num.Clamp(httpx.Int(body["pageNum"], 1), 1, maxPageNum)
	size := num.Clamp(httpx.Int(body["pageSize"], memoryPageDef), 1, memoryPageMax)
	filter, err := logFilter(body, httpx.String(body["projectId"]), page, size)
	if err != nil {
		return nil, err
	}
	records, total, err := s.deps.Audit.List(ctx, filter)
	if err != nil {
		return nil, err
	}
	logs := make([]map[string]any, 0, len(records))
	for i := range records {
		rec := records[i]
		logs = append(logs, map[string]any{
			"id":         rec.ID,
			"callId":     rec.CallID,
			"createTime": domain.FormatBeijing(rec.TS),
			"eventType":  eventOf(rec.Tool),
			"source":     rec.Source,
			"userIds":    []string{actorID(rec)},
			"agentIds":   []string{rec.TenantID},
			"status":     logStatus(rec),
			"duration":   rec.Ms,
		})
	}
	return map[string]any{"logs": logs, "total": total, "truncated": false}, nil
}

// actorID 是调用主体：Token 调用没有账号时回退到租户，历史行也能显示。
// 不能直接用 TenantID，否则同一项目下所有调用看起来都来自同一个人。
func actorID(rec domain.AuditRecord) string {
	if rec.UserID != "" {
		return rec.UserID
	}
	return rec.TenantID
}

// recordCode 返回这次调用真实的 HTTP 状态。0019 之前的行没有 http_status，
// 按派生状态兜底：FAILED 记 500，其余（含异步已受理）记 200。
func recordCode(rec domain.AuditRecord, status string) int {
	if rec.HTTPStatus > 0 {
		return rec.HTTPStatus
	}
	if status == string(domain.AuditStatusFailed) {
		return http.StatusInternalServerError
	}
	return http.StatusOK
}

func logStatus(rec domain.AuditRecord) string {
	if rec.Status != "" {
		return string(rec.Status)
	}
	// 存储层总会填派生状态；这里只给不走存储的实现兜底。
	if rec.OK {
		return string(domain.AuditStatusSuccess)
	}
	return string(domain.AuditStatusFailed)
}

// logDetail 按日志所属项目判权，而不是请求里自报的项目：只带 logId 的请求没有项目维度。
// 不存在的记录对非平台管理员是 403，不泄露日志是否存在。没有租户的审计行（登录、
// 平台级操作）只有平台管理员能看。
func (s *Server) logDetail(ctx context.Context, p domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	if s.deps.Audit == nil {
		return nil, disabledCapability("审计")
	}
	id := strings.TrimSpace(httpx.String(body["logId"]))
	if id == "" {
		return nil, domain.Invalid("dashboard", "缺少 logId")
	}
	rec, err := s.deps.Audit.Get(ctx, id)
	if err != nil {
		if errors.Is(err, domain.ErrNotFound) && !p.PlatformAdmin {
			return nil, domain.Wrap(domain.ErrForbidden, "acl", "没有该项目的操作权限", nil)
		}
		return nil, err
	}
	if rec.TenantID == "" {
		if !p.PlatformAdmin {
			return nil, domain.Wrap(domain.ErrForbidden, "acl", "没有该项目的操作权限", nil)
		}
	} else if err := p.RequireCap(rec.TenantID, domain.CapProjectView); err != nil {
		return nil, err
	}
	return logDetailJSON(rec), nil
}

func logDetailJSON(rec domain.AuditRecord) map[string]any {
	status := logStatus(rec)
	code := recordCode(rec, status)
	out := map[string]any{
		"id":           rec.ID,
		"callId":       rec.CallID,
		"createTime":   domain.FormatBeijing(rec.TS),
		"eventType":    eventOf(rec.Tool),
		"source":       rec.Source,
		"status":       status,
		"duration":     rec.Ms,
		"userId":       actorID(rec),
		"agentId":      rec.TenantID,
		"requestCode":  code,
		"responseCode": code,
	}
	if rec.RequestID != "" {
		out["requestId"] = rec.RequestID
	}
	if rec.SessionID != "" {
		out["traceId"] = rec.SessionID
	}
	if rec.ClientVersion != "" {
		out["clientVersion"] = rec.ClientVersion
	}
	if rec.HostVersion != "" {
		out["hostVersion"] = rec.HostVersion
	}
	if rec.AgentIdentity != "" {
		out["agentIdentity"] = rec.AgentIdentity
	}
	if rec.AgentContext != "" {
		out["agentContext"] = rec.AgentContext
	}
	if rec.TurnNumber > 0 {
		out["turnNumber"] = rec.TurnNumber
	}
	// 0 表示上游没有回报用量；不下发，前端保持空而不是显示 0。
	if rec.RequestTokens > 0 {
		out["requestTokens"] = rec.RequestTokens
	}
	if rec.ResponseTokens > 0 {
		out["responseTokens"] = rec.ResponseTokens
	}
	if rec.LLMCalls > 0 {
		out["llmCalls"] = rec.LLMCalls
	}
	if len(rec.Input) > 0 {
		if raw, err := json.MarshalIndent(rec.Input, "", "  "); err == nil {
			out["requestBody"] = string(raw)
		}
	}
	switch {
	case rec.Err != "":
		detail := map[string]any{"ok": false, "err": rec.Err}
		// 没有安全消息的历史行只给出错误类型，不把内部原因拼进来。
		if rec.ErrMessage != "" {
			detail["message"] = rec.ErrMessage
		}
		if raw, err := json.MarshalIndent(detail, "", "  "); err == nil {
			out["responseBody"] = string(raw)
		}
	case rec.Output != "":
		out["responseBody"] = rec.Output
	}
	return out
}

// usageQuery 按北京日期与事件类型聚合调用次数。token 只来自异步 add 的终态行
// （旧网关 add-bg 日志里的同口径 usage），其余链路没有可观测数据，保持 0。
func (s *Server) usageQuery(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	if s.deps.Audit == nil {
		return nil, disabledCapability("审计")
	}
	start := strings.TrimSpace(httpx.String(body["startDate"]))
	end := strings.TrimSpace(httpx.String(body["endDate"]))
	filter := domain.AuditFilter{
		TenantID: httpx.String(body["projectId"]),
		Tools:    append(slices.Clone(gatewayTools), domain.ToolAddMessageBackground),
	}
	if start != "" {
		from, ok := domain.BeijingDayStart(start)
		if !ok {
			return nil, domain.Invalid("dashboard", "startDate 格式应为 YYYY-MM-DD")
		}
		filter.Since = from
	}
	if end != "" {
		to, ok := domain.BeijingDayStart(end)
		if !ok {
			return nil, domain.Invalid("dashboard", "endDate 格式应为 YYYY-MM-DD")
		}
		filter.Until = to.AddDate(0, 0, 1)
	}
	counts, err := s.deps.Audit.HourCounts(ctx, filter)
	if err != nil {
		return nil, err
	}
	byDay := map[string]map[string]int{}
	tokensByDay := map[string]int{}
	for _, c := range counts {
		day := domain.BeijingDay(c.Hour)
		if c.Tool == domain.ToolAddMessageBackground {
			tokensByDay[day] += c.RequestTokens + c.ResponseTokens
			continue
		}
		if byDay[day] == nil {
			byDay[day] = map[string]int{}
		}
		byDay[day][toolEvents[c.Tool]] += c.Calls
	}
	days := usageDays(slices.Collect(maps.Keys(byDay)), start, end)
	stats := make([]map[string]any, 0, len(days))
	for _, day := range days {
		stats = append(stats, map[string]any{
			"date":         day,
			"addMessage":   map[string]any{"calls": byDay[day]["addMessage"], "totalTokens": tokensByDay[day]},
			"searchMemory": map[string]any{"calls": byDay[day]["searchMemory"], "totalTokens": 0},
			"chat":         map[string]any{"requestTokens": 0, "responseTokens": 0, "totalTokens": 0},
			"extractMemory": map[string]any{
				"requestTokens": 0, "responseTokens": 0, "totalTokens": 0,
			},
			"rerank": map[string]any{"requestTokens": 0, "totalTokens": 0},
		})
	}
	return map[string]any{"dailyStats": stats, "truncated": false}, nil
}

// usageDays 按区间逐天补齐，缺数据的天给空桶，图上才不会丢天；区间缺省或颠倒时只给有数据的天。
func usageDays(have []string, start, end string) []string {
	from, errFrom := time.Parse(time.DateOnly, start)
	to, errTo := time.Parse(time.DateOnly, end)
	if errFrom != nil || errTo != nil || from.After(to) {
		slices.Sort(have)
		return have
	}
	days := make([]string, 0, maxUsageDays)
	for day := from; !day.After(to) && len(days) < maxUsageDays; day = day.AddDate(0, 0, 1) {
		days = append(days, day.Format(time.DateOnly))
	}
	return days
}
