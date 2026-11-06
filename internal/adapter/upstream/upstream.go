package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// Observer 记录上游调用指标；由 internal/platform/metrics 实现。nil 表示不记录。
type Observer interface {
	ObserveUpstream(endpoint, outcome string, d time.Duration)
}

// Client 是 memos-api 的 HTTP 客户端。
type Client struct {
	base     string
	http     *http.Client
	maxBytes int64
	// dashboardMax 是 Dashboard 流式读取的原始字节上限，见 dashboard.go。
	dashboardMax int64
	log          *slog.Logger
	metrics      Observer
}

// Options 是客户端构造参数。
type Options struct {
	BaseURL           string
	Timeout           time.Duration
	MaxResponseBytes  int64 // 通用响应上限
	DashboardMaxBytes int64 // 控制台记忆列表的流式读取上限
	Metrics           Observer
}

// New 构造客户端。BaseURL 为空或不是 http(s) 地址时返回错误。
func New(opts Options, log *slog.Logger) (*Client, error) {
	base := strings.TrimRight(strings.TrimSpace(opts.BaseURL), "/")
	if base == "" {
		return nil, errors.New("upstream: base url must not be empty")
	}
	if !strings.HasPrefix(base, "http://") && !strings.HasPrefix(base, "https://") {
		return nil, errors.New("upstream: base url must start with http:// or https://")
	}
	if opts.Timeout <= 0 {
		return nil, errors.New("upstream: timeout must be positive")
	}
	if opts.MaxResponseBytes <= 0 {
		opts.MaxResponseBytes = 8 << 20
	}
	if opts.DashboardMaxBytes <= 0 {
		opts.DashboardMaxBytes = defaultDashboardMax
	}
	if log == nil {
		log = slog.Default()
	}
	// 默认 Transport 的 MaxIdleConnsPerHost 只有 2，并发检索/写入时连接复用率很低。
	// 控制面只访问一个上游，按并发量显式配置，同时用 MaxConnsPerHost 防止打爆上游。
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          128,
		MaxIdleConnsPerHost:   32,
		MaxConnsPerHost:       64,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: time.Second,
	}
	return &Client{
		base:         base,
		http:         &http.Client{Timeout: opts.Timeout, Transport: transport},
		maxBytes:     opts.MaxResponseBytes,
		dashboardMax: opts.DashboardMaxBytes,
		log:          log,
		metrics:      opts.Metrics,
	}, nil
}

// Health 探测上游 /health，返回原始 JSON 给 /api/meta.health。
// 调用方应传入短超时 context，避免拖慢 /api/meta。
// 2xx 但响应不是 JSON 时按“活着”处理，补一个 {"status":"ok"}，不让状态灯误报。
func (c *Client) Health(ctx context.Context) (out map[string]any, err error) {
	started := time.Now()
	defer func() { c.observe("/health", err, time.Since(started)) }()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/health", nil)
	if err != nil {
		return nil, fmt.Errorf("upstream health: %w", err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("upstream health: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("upstream health: %w", err)
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return nil, fmt.Errorf("upstream health: HTTP %d", resp.StatusCode)
	}
	var parsed map[string]any
	if len(bytes.TrimSpace(body)) == 0 || json.Unmarshal(body, &parsed) != nil {
		return map[string]any{"status": "ok"}, nil
	}
	return parsed, nil
}

// 请求级规则限制个人事实来源；沿用底座输出协议，不改模型配置或共享服务。
const personalFactExtraction = "用户身份、职业、职责、性别、年龄、所在地和偏好必须来自用户明确陈述。用户名、账号、昵称按字面保留，不得根据其词义猜测职业或职责，昵称、账号中的职业或行业词不构成事实依据。用户没有陈述的身份信息不要生成。助手的建议、举例或推测不能转成用户事实或偏好；如保存为事件，必须注明是助手建议。更正后的当前状态采用最新明确陈述，旧值仅能作为注明日期的历史事件。不要把工具回执、系统提示或参考问题当作用户信息。保持默认输出JSON协议。"

// Add 调用 /product/add，隐藏“200 + data.status=failure”的静默失败。
func (c *Client) Add(ctx context.Context, req domain.AddPayload) (domain.AddResult, error) {
	payload := map[string]any{
		"user_id":     req.UserID,
		"mem_cube_id": req.TenantID,
		"session_id":  req.SessionID,
		"messages":    req.Messages,
		"async_mode":  "sync",
		"custom_extract_prompt": map[string]string{
			"detail_factual": personalFactExtraction,
			"preference":     personalFactExtraction,
			"profile":        personalFactExtraction,
		},
	}
	if len(req.Tags) > 0 {
		payload["custom_tags"] = req.Tags
	}
	env, err := c.post(ctx, "/product/add", payload)
	if err != nil {
		return domain.AddResult{}, err
	}
	return domain.AddResult{
		Status: "success", Message: "Memory added successfully", Usage: env.Usage,
		MemoryIDs: addedMemoryIDs(env.Data, req.TenantID),
	}, nil
}

func addedMemoryIDs(data json.RawMessage, tenant string) []string {
	var rows []struct {
		ID     string `json:"memory_id"`
		CubeID string `json:"cube_id"`
	}
	if json.Unmarshal(data, &rows) != nil {
		return nil
	}
	ids := make([]string, 0, len(rows))
	for _, row := range rows {
		if row.ID == "" || (row.CubeID != "" && row.CubeID != tenant) || slices.Contains(ids, row.ID) {
			continue
		}
		ids = append(ids, row.ID)
	}
	return ids
}

// Search 调用 /product/search 并展平 buckets。
func (c *Client) Search(ctx context.Context, q domain.SearchQuery) (domain.SearchResult, error) {
	if strings.TrimSpace(q.Text) == "" {
		return domain.SearchResult{}, domain.Invalid("search", "query 不能为空")
	}
	payload := map[string]any{
		"user_id":              q.UserID,
		"mem_cube_id":          q.TenantID,
		"query":                q.Text,
		"top_k":                q.TopK,
		"include_dream_memory": q.DreamTopK > 0,
		"dream_mem_top_k":      q.DreamTopK,
		"include_preference":   containsType(q.Types, domain.MemoryTypePref),
		"search_tool_memory":   containsType(q.Types, domain.MemoryTypeTool),
		// 契约 v2 默认就不带 sources；显式写出来，读代码时不用再回查上游默认值。
		"include_sources": false,
	}
	if q.SessionID != "" {
		payload["session_id"] = q.SessionID
	}
	if q.Mode != "" {
		payload["mode"] = q.Mode
	}
	if len(q.Filter) > 0 {
		payload["filter"] = q.Filter
	}
	env, err := c.post(ctx, "/product/search", payload)
	if err != nil {
		return domain.SearchResult{}, err
	}
	return parseBuckets(env.Data, q.TopK, q.DreamTopK)
}

// Profile 调用 /product/get_memory 并展平 buckets。
func (c *Client) Profile(ctx context.Context, q domain.ProfileQuery) (domain.ProfileResult, error) {
	payload := map[string]any{
		"user_id":             q.UserID,
		"mem_cube_id":         q.TenantID,
		"page":                q.Page,
		"page_size":           q.PageSize,
		"include_preference":  q.IncludePreference,
		"include_tool_memory": q.IncludeToolMemory,
		"include_sources":     false,
	}
	env, err := c.post(ctx, "/product/get_memory", payload)
	if err != nil {
		return domain.ProfileResult{}, err
	}
	result, err := parseBuckets(env.Data, 0, 0)
	if err != nil {
		return domain.ProfileResult{}, err
	}
	return domain.ProfileResult{
		Facts:       result.Facts,
		Preferences: result.Preferences,
		Tools:       result.Tools,
		Total:       result.Total,
		Current:     q.Page,
		Size:        q.PageSize,
	}, nil
}

// OwnedMemory 单条查询并带租户条件。第二个返回值为 false 表示不存在、已删除或不属于该租户。
// 上游在 multi-DB 下可能忽略 mem_cube_id，因此元数据缺失租户时用 user_id 兜底；
// 两者都无法证明归属时 fail-closed。
func (c *Client) OwnedMemory(ctx context.Context, tenantID, userID, memoryID string) (domain.Memory, bool, error) {
	return c.ownedMemory(ctx, tenantID, userID, memoryID, false)
}

// MemoryDetail 只在用户打开详情时读取来源，避免检索与活动轮询带大正文。
func (c *Client) MemoryDetail(ctx context.Context, tenantID, userID, memoryID string) (domain.Memory, bool, error) {
	return c.ownedMemory(ctx, tenantID, userID, memoryID, true)
}

func (c *Client) ownedMemory(ctx context.Context, tenantID, userID, memoryID string, sources bool) (domain.Memory, bool, error) {
	if tenantID == "" || userID == "" {
		return domain.Memory{}, false, domain.Invalid("owned_memory", "缺少租户条件")
	}
	path := "/product/get_memory/" + url.PathEscape(memoryID) +
		"?mem_cube_id=" + url.QueryEscape(tenantID) + "&include_sources=" + strconv.FormatBool(sources)
	env, err := c.do(ctx, http.MethodGet, path, nil)
	if err != nil {
		return domain.Memory{}, false, err
	}
	raws, err := decodeMemoryList(env.Data)
	if err != nil {
		return domain.Memory{}, false, err
	}
	for _, raw := range raws {
		mem, err := raw.toDomain(domain.MemoryTypeText)
		if err != nil {
			return domain.Memory{}, false, err
		}
		if mem.ID != memoryID || mem.Deleted() {
			continue
		}
		// 有租户字段时必须精确相等；缺失（multi-DB 忽略 mem_cube_id）时才用 user_id 兜底。
		if mem.TenantID != "" {
			if mem.TenantID != tenantID {
				continue
			}
		} else if mem.UserID == "" || mem.UserID != userID {
			continue
		}
		return mem, true, nil
	}
	return domain.Memory{}, false, nil
}

// OwnedMemories 批量查询记忆并校验归属，返回自有 id 集合。
// 与 OwnedMemory 同一套 fail-closed 规则：接口失败、记录已删除、租户/用户不匹配都不算自有。
func (c *Client) OwnedMemories(ctx context.Context, tenantID, userID string, ids []string) (map[string]bool, error) {
	if tenantID == "" || userID == "" {
		return nil, domain.Invalid("owned_memories", "缺少租户条件")
	}
	owned := make(map[string]bool, len(ids))
	if len(ids) == 0 {
		return owned, nil
	}
	path := "/product/get_memory_by_ids?mem_cube_id=" + url.QueryEscape(tenantID)
	// 契约 v2 的请求体是对象（product_models.GetMemoryByIdsRequest）；裸数组只是
	// 上游为过渡保留的旧形状，不再使用。
	env, err := c.post(ctx, path, map[string]any{"memory_ids": ids})
	if err != nil {
		return nil, err
	}
	var payload struct {
		Memories []rawMemory `json:"memories"`
	}
	trimmed := bytes.TrimSpace(env.Data)
	if len(trimmed) > 0 && !bytes.Equal(trimmed, []byte("null")) {
		if err := json.Unmarshal(trimmed, &payload); err != nil {
			return nil, domain.Wrap(domain.ErrUpstreamRejected, "owned_memories", "记忆服务返回了无法解析的记录", err)
		}
	}
	for _, raw := range payload.Memories {
		mem, err := raw.toDomain(domain.MemoryTypeText)
		if err != nil {
			return nil, err
		}
		if mem.Deleted() {
			continue
		}
		if mem.TenantID != "" {
			if mem.TenantID != tenantID {
				continue
			}
		} else if mem.UserID == "" || mem.UserID != userID {
			continue
		}
		owned[mem.ID] = true
	}
	return owned, nil
}

// Owners 返回 memory_id → user_name（租户 cube_id）映射，用于删除前区分“不存在”与“别人的记忆”。
// 上游抖动或返回非对象时返回错误，调用方必须按 fail-closed 拒绝删除。
func (c *Client) Owners(ctx context.Context, ids []string) (map[string]string, error) {
	if len(ids) == 0 {
		return map[string]string{}, nil
	}
	env, err := c.post(ctx, "/product/get_user_names_by_memory_ids", map[string]any{"memory_ids": ids})
	if err != nil {
		return nil, err
	}
	var raw map[string]any
	if err := json.Unmarshal(env.Data, &raw); err != nil || raw == nil {
		return nil, domain.Wrap(domain.ErrUpstreamRejected, "owners", "归属查询返回了非对象", err)
	}
	out := make(map[string]string, len(raw))
	for id, value := range raw {
		if owner, ok := value.(string); ok && owner != "" {
			out[id] = owner
		}
	}
	return out, nil
}

// Delete 删除指定租户下的记忆。user_id 不是删除作用域，带上会让上游 200 + failure；
// 共享图库必须显式给 writable_cube_ids，否则上游拒绝按 id 删除。
func (c *Client) Delete(ctx context.Context, tenantID string, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	payload := map[string]any{"memory_ids": ids}
	if tenantID != "" {
		payload["writable_cube_ids"] = []string{tenantID}
	}
	_, err := c.post(ctx, "/product/delete_memory", payload)
	return err
}

// Feedback 调用 /product/feedback 并解析改动记录。
func (c *Client) Feedback(ctx context.Context, req domain.FeedbackRequest) (domain.FeedbackResult, error) {
	payload := map[string]any{
		"user_id":          req.UserID,
		"mem_cube_id":      req.TenantID,
		"history":          []map[string]string{{"role": "user", "content": req.Content}},
		"feedback_content": req.Content,
		"async_mode":       "sync",
	}
	if req.SessionID != "" {
		payload["session_id"] = req.SessionID
	}
	if len(req.RetrievedMemoryIDs) > 0 {
		payload["retrieved_memory_ids"] = req.RetrievedMemoryIDs
	}
	env, err := c.post(ctx, "/product/feedback", payload)
	if err != nil {
		return domain.FeedbackResult{}, err
	}
	return parseFeedback(env.Data)
}

type envelope struct {
	Code    *int            `json:"code"`
	Message string          `json:"message"`
	Data    json.RawMessage `json:"data"`
	Usage   map[string]int  `json:"usage"`
}

func (c *Client) post(ctx context.Context, path string, payload any) (envelope, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return envelope{}, fmt.Errorf("upstream: marshal request: %w", err)
	}
	return c.do(ctx, http.MethodPost, path, body)
}

func (c *Client) do(ctx context.Context, method, path string, body []byte) (envelope, error) {
	return c.doChecked(ctx, checkedRequest{Method: method, Path: path, Body: body, Check: checkEnvelope})
}

// checkEnvelope 是记忆接口的成功判据：code 缺省或为 200，且 data.status 不是 failure。
func checkEnvelope(env envelope) error {
	if env.Code != nil && *env.Code != http.StatusOK {
		return domain.Wrap(domain.ErrUpstreamRejected, "upstream", safeMessage(env.Message), nil)
	}
	if statusFailure(env.Data) {
		return domain.Wrap(domain.ErrUpstreamRejected, "upstream", safeMessage(env.Message), nil)
	}
	return nil
}

func (c *Client) doChecked(ctx context.Context, in checkedRequest) (env envelope, err error) {
	started := time.Now()
	defer func() {
		c.observe(endpointLabel(in.Path), err, time.Since(started))
	}()
	var reader io.Reader
	if in.Body != nil {
		reader = bytes.NewReader(in.Body)
	}
	req, err := http.NewRequestWithContext(ctx, in.Method, c.base+in.Path, reader)
	if err != nil {
		return envelope{}, fmt.Errorf("upstream: build request: %w", err)
	}
	if in.Body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.http.Do(req)
	if err != nil {
		// 不把上游地址写进返回给调用方的消息。
		c.log.WarnContext(ctx, "upstream unreachable", slog.String("error", err.Error()))
		return envelope{}, domain.Wrap(domain.ErrUpstreamUnavailable, "upstream", "暂时连不上记忆服务，请稍后重试。", err)
	}
	defer func() { _ = resp.Body.Close() }()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, c.maxBytes+1))
	if err != nil {
		return envelope{}, domain.Wrap(domain.ErrUpstreamUnavailable, "upstream", "读取记忆服务响应失败", err)
	}
	if int64(len(raw)) > c.maxBytes {
		return envelope{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream",
			"记忆服务响应过大，已拒绝", fmt.Errorf("response exceeds %d bytes", c.maxBytes))
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		c.log.WarnContext(ctx, "upstream 返回非 2xx",
			slog.String("method", in.Method), slog.String("path", in.Path),
			slog.Int("status", resp.StatusCode), slog.Int("bytes", len(raw)))
		if in.CheckError != nil {
			return envelope{}, in.CheckError(resp.StatusCode, raw)
		}
		return envelope{}, mapHTTPError(resp.StatusCode, raw)
	}

	if len(bytes.TrimSpace(raw)) == 0 {
		return env, nil
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return envelope{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务返回了无法解析的响应", err)
	}
	if err := in.Check(env); err != nil {
		return envelope{}, err
	}
	return env, nil
}

func mapHTTPError(status int, raw []byte) error {
	var env envelope
	message := ""
	if err := json.Unmarshal(raw, &env); err == nil {
		message = env.Message
	}
	switch status {
	case http.StatusBadRequest, http.StatusUnprocessableEntity:
		if message == "" {
			message = "记忆服务拒绝了请求"
		}
		return domain.Wrap(domain.ErrInvalidInput, "upstream", safeMessage(message), fmt.Errorf("upstream status %d", status))
	case http.StatusUnauthorized, http.StatusForbidden, http.StatusNotFound, http.StatusConflict:
		// 控制面侧的凭据、路由或资源问题：重试不会自愈，但也不能报成调用方的鉴权错误。
		return domain.Wrap(domain.ErrUpstreamDenied, "upstream",
			"记忆服务拒绝了控制面的请求", fmt.Errorf("upstream status %d", status))
	case http.StatusTooManyRequests:
		return domain.Wrap(domain.ErrRateLimited, "upstream",
			"记忆服务限流，请稍后重试。", fmt.Errorf("upstream status %d", status))
	default:
		return domain.Wrap(domain.ErrUpstreamUnavailable, "upstream",
			"记忆服务暂时不可用，请稍后重试。", fmt.Errorf("upstream status %d: %s", status, safeMessage(message)))
	}
}

// endpointLabel 把带 id 的路径收敛成低基数模板。
func endpointLabel(path string) string {
	if i := strings.IndexByte(path, '?'); i >= 0 {
		path = path[:i]
	}
	if strings.HasPrefix(path, "/product/get_memory/") {
		return "/product/get_memory/{id}"
	}
	return path
}

func (c *Client) observe(endpoint string, err error, d time.Duration) {
	if c.metrics == nil {
		return
	}
	outcome := "ok"
	switch {
	case err == nil:
	case errors.Is(err, domain.ErrInvalidInput):
		outcome = "invalid"
	case errors.Is(err, domain.ErrUpstreamRejected):
		outcome = "rejected"
	case errors.Is(err, domain.ErrUpstreamDenied):
		outcome = "denied"
	case errors.Is(err, domain.ErrRateLimited):
		outcome = "rate_limited"
	case errors.Is(err, domain.ErrUpstreamUnavailable):
		outcome = "unavailable"
	default:
		outcome = "error"
	}
	c.metrics.ObserveUpstream(endpoint, outcome, d)
}

func safeMessage(message string) string {
	message = strings.TrimSpace(message)
	if message == "" {
		return "记忆服务拒绝了该请求"
	}
	runes := []rune(message)
	if len(runes) > 300 {
		return string(runes[:300])
	}
	return message
}

// statusFailure 识别 HTTP 200 但 data.status=="failure" 的静默失败。
func statusFailure(data json.RawMessage) bool {
	var obj struct {
		Status string `json:"status"`
	}
	if err := json.Unmarshal(data, &obj); err != nil {
		return false
	}
	return strings.EqualFold(obj.Status, "failure")
}

type rawMemory struct {
	ID       string         `json:"id"`
	Memory   string         `json:"memory"`
	Metadata map[string]any `json:"metadata"`
}

func (r rawMemory) toDomain(bucket domain.MemoryType) (domain.Memory, error) {
	meta := r.Metadata
	mem := domain.Memory{
		ID:       r.ID,
		Type:     bucket,
		Content:  r.Memory,
		Key:      stringField(meta, "key"),
		Subtype:  stringField(meta, "type"),
		NodeType: stringField(meta, "memory_type"),
		Status:   stringField(meta, "status"),
		Tags:     stringSlice(meta["tags"]),
		TenantID: stringField(meta, "mem_cube_id"),
		UserID:   stringField(meta, "user_id"),
	}
	mem.Version, mem.History, mem.Sources = memoryDetails(meta)
	if mem.TenantID == "" {
		mem.TenantID = stringField(meta, "cube_id")
	}
	if bucket == domain.MemoryTypeDream {
		mem.Key = firstNonEmpty(stringField(meta, "title"), mem.Key)
	}
	if v, ok := meta["relativity"].(float64); ok {
		mem.Relativity = v
	}
	// 契约 v2 起上游所有时间都是真 UTC，不再按桶区分口径。
	created := stringField(meta, "created_at")
	utc, err := domain.ParseUTC(created)
	mem.CreatedAt = utc
	mem.UpdatedAt, _ = domain.ParseUTC(stringField(meta, "updated_at"))
	if err != nil {
		return domain.Memory{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆时间格式无法解析", err)
	}
	return mem, nil
}

type bucketGroup struct {
	Memories   []rawMemory `json:"memories"`
	TotalNodes int         `json:"total_nodes"`
}

// parseBuckets 展平 {bucket:[{memories:[...]}]}，过滤软删除、按 id 去重、按 relativity 降序、按桶截断。
func parseBuckets(data json.RawMessage, topK, dreamTopK int) (domain.SearchResult, error) {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return domain.SearchResult{}, nil
	}
	var buckets map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &buckets); err != nil {
		return domain.SearchResult{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream",
			"记忆服务返回了无法解析的结果"+typeHint(err), err)
	}

	result := domain.SearchResult{}
	for _, bucket := range searchBuckets {
		rawGroups, ok := buckets[bucket.name]
		if !ok {
			continue
		}
		var groups []bucketGroup
		if err := json.Unmarshal(rawGroups, &groups); err != nil {
			return domain.SearchResult{}, domain.Wrap(domain.ErrUpstreamRejected, "upstream",
				"记忆服务的 "+bucket.name+" 桶无法解析"+typeHint(err), err)
		}
		rows, total, err := collectRows(groups, bucket.kind)
		if err != nil {
			return domain.SearchResult{}, err
		}
		result.Total += total
		sort.SliceStable(rows, func(i, j int) bool { return rows[i].Relativity > rows[j].Relativity })
		limit := topK
		if bucket.kind == domain.MemoryTypeDream && dreamTopK > 0 {
			limit = dreamTopK
		}
		if limit > 0 && len(rows) > limit {
			rows = rows[:limit]
		}
		assignBucket(&result, bucket.kind, rows)
	}
	return result, nil
}

// typeHint 只读取 encoding/json 反序列化错误里的类型信息，不自行解析响应。
func typeHint(err error) string {
	var typeErr *json.UnmarshalTypeError
	if !errors.As(err, &typeErr) {
		return ""
	}
	if typeErr.Field != "" {
		return fmt.Sprintf("（字段 %s 期望 %s，实际 %s）", typeErr.Field, typeErr.Type, typeErr.Value)
	}
	return fmt.Sprintf("（期望 %s，实际 %s）", typeErr.Type, typeErr.Value)
}

// searchBuckets 是检索结果里认识的桶。上游还会返回 act_mem、para_mem、
// pref_note、pref_string 等辅助键，形状不一定是记忆组，直接忽略，
// 不能把它们当 text 桶混进 facts。
var searchBuckets = []struct {
	name string
	kind domain.MemoryType
}{
	{"text_mem", domain.MemoryTypeText},
	{"pref_mem", domain.MemoryTypePref},
	{"tool_mem", domain.MemoryTypeTool},
	{"dream_mem", domain.MemoryTypeDream},
}

// collectRows 展平一个桶，过滤软删除并按 id 去重；total 来自上游 total_nodes。
func collectRows(groups []bucketGroup, memType domain.MemoryType) ([]domain.Memory, int, error) {
	rows := make([]domain.Memory, 0, 8)
	seen := make(map[string]struct{})
	total := 0
	for _, group := range groups {
		total += group.TotalNodes
		for _, raw := range group.Memories {
			mem, err := raw.toDomain(memType)
			if err != nil {
				return nil, 0, err
			}
			if mem.Deleted() {
				continue
			}
			if _, dup := seen[mem.ID]; dup {
				continue
			}
			seen[mem.ID] = struct{}{}
			rows = append(rows, mem)
		}
	}
	return rows, total, nil
}

func assignBucket(result *domain.SearchResult, memType domain.MemoryType, rows []domain.Memory) {
	switch memType {
	case domain.MemoryTypeText:
		result.Facts = rows
	case domain.MemoryTypePref:
		result.Preferences = rows
	case domain.MemoryTypeTool:
		result.Tools = rows
	case domain.MemoryTypeDream:
		result.Dreams = rows
	}
}

func decodeMemoryList(data json.RawMessage) ([]rawMemory, error) {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return nil, nil
	}
	var list []rawMemory
	if err := json.Unmarshal(trimmed, &list); err == nil {
		return list, nil
	}
	var one rawMemory
	if err := json.Unmarshal(trimmed, &one); err != nil {
		return nil, domain.Wrap(domain.ErrUpstreamRejected, "upstream", "记忆服务返回了无法解析的记录", err)
	}
	return []rawMemory{one}, nil
}

type feedbackPayload struct {
	Record struct {
		Add []struct {
			ID   string `json:"id"`
			Text string `json:"text"`
		} `json:"add"`
		Update []struct {
			ID         string `json:"id"`
			Text       string `json:"text"`
			ArchivedID string `json:"archived_id"`
		} `json:"update"`
	} `json:"record"`
}

func parseFeedback(data json.RawMessage) (domain.FeedbackResult, error) {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return emptyFeedback(), nil
	}
	payloads, err := decodeFeedback(trimmed)
	if err != nil {
		return domain.FeedbackResult{}, err
	}
	return summarizeFeedback(payloads, 50), nil
}

// decodeFeedback 接受数组或单个对象两种响应形态。
func decodeFeedback(trimmed []byte) ([]feedbackPayload, error) {
	var payloads []feedbackPayload
	if err := json.Unmarshal(trimmed, &payloads); err == nil {
		return payloads, nil
	}
	var one feedbackPayload
	if err := json.Unmarshal(trimmed, &one); err != nil {
		return nil, domain.Wrap(domain.ErrUpstreamRejected, "upstream", "反馈结果无法解析", err)
	}
	return []feedbackPayload{one}, nil
}

func summarizeFeedback(payloads []feedbackPayload, limit int) domain.FeedbackResult {
	result := domain.FeedbackResult{Status: "success"}
	for _, payload := range payloads {
		for _, item := range payload.Record.Add {
			if item.ID != "" {
				result.Added = append(result.Added, domain.FeedbackChange{ID: item.ID, Text: item.Text})
			}
		}
		for _, item := range payload.Record.Update {
			if item.ID != "" {
				result.Updated = append(result.Updated, domain.FeedbackChange{
					ID:         item.ID,
					Text:       item.Text,
					ArchivedID: item.ArchivedID,
				})
			}
		}
	}
	if len(result.Added) > limit || len(result.Updated) > limit {
		result.Truncated = true
	}
	result.Added = limitChanges(result.Added, limit)
	result.Updated = limitChanges(result.Updated, limit)
	if len(result.Added)+len(result.Updated) > 0 {
		result.Message = fmt.Sprintf("Feedback recorded: %d added, %d updated", len(result.Added), len(result.Updated))
		return result
	}
	result.Message = "Feedback recorded, but no memory was changed"
	return result
}

func limitChanges(changes []domain.FeedbackChange, limit int) []domain.FeedbackChange {
	if len(changes) > limit {
		return changes[:limit]
	}
	return changes
}

func emptyFeedback() domain.FeedbackResult {
	return domain.FeedbackResult{Status: "success", Message: "Feedback recorded, but no memory was changed"}
}

func containsType(types []domain.MemoryType, want domain.MemoryType) bool {
	if len(types) == 0 {
		return true
	}
	return slices.Contains(types, want)
}

func stringField(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	s, _ := m[key].(string)
	return s
}

func stringSlice(v any) []string {
	list, ok := v.([]any)
	if !ok {
		if s, ok := v.(string); ok && s != "" {
			return []string{s}
		}
		return nil
	}
	out := make([]string, 0, len(list))
	for _, item := range list {
		if s, ok := item.(string); ok && s != "" {
			out = append(out, s)
		}
	}
	return out
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// checkedRequest 是一次经过成功判据校验的上游调用。
type checkedRequest struct {
	Method string
	Path   string
	Body   []byte
	Check  func(envelope) error
	// CheckError 覆盖默认的按 HTTP 状态码映射。提示词接口用稳定的 code
	// 字符串区分 400/404/409/413/429/502/504，只看状态码会丢掉版本冲突与等待时间。
	CheckError func(status int, raw []byte) error
}
