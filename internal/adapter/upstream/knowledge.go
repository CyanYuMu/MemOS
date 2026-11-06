package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/num"
)

const (
	kbPageMax   = 200
	kbScanPages = 20
)

type rawKB struct {
	ID          string `json:"id"`
	KnowledgeID string `json:"knowledgebase_id"`
	Name        string `json:"knowledgebase_name"`
	Description string `json:"knowledgebase_description"`
	FileCount   int    `json:"file_count"`
	TotalSize   int64  `json:"total_size"`
	CreatedAt   string `json:"created_at"`
	UpdatedAt   string `json:"updated_at"`
}

func (r rawKB) toDomain() domain.KnowledgeBase {
	return domain.KnowledgeBase{
		ID:          firstNonEmpty(r.KnowledgeID, r.ID),
		Name:        r.Name,
		Description: r.Description,
		FileCount:   r.FileCount,
		TotalSize:   r.TotalSize,
		CreatedAt:   parseUpstreamTime(r.CreatedAt),
		UpdatedAt:   parseUpstreamTime(r.UpdatedAt),
	}
}

type rawKBFile struct {
	ID          string  `json:"id"`
	KnowledgeID string  `json:"knowledgebase_id"`
	Name        string  `json:"name"`
	Type        string  `json:"type"`
	Status      string  `json:"status"`
	Size        int64   `json:"size"`
	SizeMB      float64 `json:"sizeMB"`
	CreatedAt   string  `json:"created_at"`
}

func (r rawKBFile) toDomain() domain.KnowledgeFile {
	created := parseUpstreamTime(r.CreatedAt)
	sizeMB := r.SizeMB
	if sizeMB == 0 && r.Size > 0 {
		sizeMB = float64(r.Size) / (1024 * 1024)
	}
	return domain.KnowledgeFile{
		ID:              r.ID,
		KnowledgeBaseID: r.KnowledgeID,
		Name:            r.Name,
		Type:            r.Type,
		Status:          r.Status,
		Size:            r.Size,
		SizeMB:          sizeMB,
		CreatedAt:       created,
	}
}

func parseUpstreamTime(raw string) time.Time {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return time.Time{}
	}
	if parsed, err := time.Parse(time.RFC3339Nano, raw); err == nil {
		return parsed
	}
	return time.Time{}
}

// KnowledgeBases 逐页读取租户下的知识库。上游没有名称过滤，控制台按名称搜索时
// 在这个结果上做，所以这里把扫描上限固定在 kbScanPages 页，避免无界翻页。
func (c *Client) KnowledgeBases(ctx context.Context, tenant string) ([]domain.KnowledgeBase, error) {
	if strings.TrimSpace(tenant) == "" {
		return nil, domain.Invalid("knowledgebase", "缺少项目")
	}
	var out []domain.KnowledgeBase
	for page := 1; page <= kbScanPages; page++ {
		env, err := c.postCloud(ctx, "/product/get_knowledgebase", map[string]any{
			"mem_cube_id": tenant,
			"page":        page,
			"page_size":   kbPageMax,
		})
		if err != nil {
			return nil, err
		}
		var data struct {
			List  []rawKB `json:"knowledgebase_list"`
			Total int     `json:"total"`
		}
		if err := decodeData(env, &data); err != nil {
			return nil, err
		}
		for i := range data.List {
			out = append(out, data.List[i].toDomain())
		}
		if len(data.List) < kbPageMax || len(out) >= data.Total {
			break
		}
	}
	return out, nil
}

// KnowledgeBase 按 ID 读取一个知识库；不存在或不属于租户都返回 ErrNotFound。
func (c *Client) KnowledgeBase(ctx context.Context, tenant, id string) (domain.KnowledgeBase, error) {
	if strings.TrimSpace(id) == "" {
		return domain.KnowledgeBase{}, domain.Invalid("knowledgebase", "缺少知识库 ID")
	}
	env, err := c.postCloud(ctx, "/product/get_knowledgebase", map[string]any{
		"mem_cube_id":      tenant,
		"knowledgebase_id": id,
	})
	if err != nil {
		return domain.KnowledgeBase{}, err
	}
	var raw rawKB
	if err := decodeData(env, &raw); err != nil {
		return domain.KnowledgeBase{}, err
	}
	return raw.toDomain(), nil
}

// CreateKnowledgeBase 创建知识库，名称与描述由调用方校验后传入。
func (c *Client) CreateKnowledgeBase(
	ctx context.Context, tenant, name, description string,
) (domain.KnowledgeBase, error) {
	if strings.TrimSpace(name) == "" {
		return domain.KnowledgeBase{}, domain.Invalid("knowledgebase", "知识库名称不能为空")
	}
	env, err := c.postCloud(ctx, "/product/create_knowledgebase", map[string]any{
		"mem_cube_id":               tenant,
		"knowledgebase_name":        name,
		"knowledgebase_description": description,
	})
	if err != nil {
		return domain.KnowledgeBase{}, err
	}
	var raw rawKB
	if err := decodeData(env, &raw); err != nil {
		return domain.KnowledgeBase{}, err
	}
	kb := raw.toDomain()
	if kb.ID == "" {
		kb.ID = firstNonEmpty(raw.KnowledgeID, raw.ID)
	}
	return kb, nil
}

// UpdateKnowledgeBase 调上游 update_knowledgebase；nil 字段表示不修改。
func (c *Client) UpdateKnowledgeBase(ctx context.Context, in domain.KnowledgeBaseUpdate) (domain.KnowledgeBase, error) {
	payload := map[string]any{"mem_cube_id": in.TenantID, "knowledgebase_id": in.ID}
	if in.Name != nil {
		payload["knowledgebase_name"] = *in.Name
	}
	if in.Description != nil {
		payload["knowledgebase_description"] = *in.Description
	}
	env, err := c.postCloud(ctx, "/product/update_knowledgebase", payload)
	if err != nil {
		return domain.KnowledgeBase{}, err
	}
	var raw rawKB
	if err := decodeData(env, &raw); err != nil {
		return domain.KnowledgeBase{}, err
	}
	return raw.toDomain(), nil
}

// DeleteKnowledgeBase 删除知识库及其文件。
func (c *Client) DeleteKnowledgeBase(ctx context.Context, tenant, id string) error {
	env, err := c.postCloud(ctx, "/product/delete_knowledgebase", map[string]any{
		"mem_cube_id":      tenant,
		"knowledgebase_id": id,
		"delete_all":       true,
	})
	if err != nil {
		return err
	}
	var data struct {
		Success bool `json:"success"`
	}
	if err := decodeData(env, &data); err != nil {
		return err
	}
	if !data.Success {
		return domain.NotFound("knowledgebase", "知识库不存在")
	}
	return nil
}

// KnowledgeFiles 列出知识库的一页文件。
func (c *Client) KnowledgeFiles(ctx context.Context, in domain.KnowledgeFilesQuery) (domain.KnowledgeFilePage, error) {
	if strings.TrimSpace(in.KnowledgeBaseID) == "" {
		return domain.KnowledgeFilePage{}, domain.Invalid("knowledgebase", "缺少知识库 ID")
	}
	env, err := c.postCloud(ctx, "/product/get_knowledgebase_file", map[string]any{
		"mem_cube_id":      in.TenantID,
		"knowledgebase_id": in.KnowledgeBaseID,
		"page":             max(in.Page, 1),
		"page_size":        num.Clamp(in.Size, 1, kbPageMax),
	})
	if err != nil {
		return domain.KnowledgeFilePage{}, err
	}
	var data struct {
		List  []rawKBFile `json:"file_detail_list"`
		Total int         `json:"total"`
	}
	if err := decodeData(env, &data); err != nil {
		return domain.KnowledgeFilePage{}, err
	}
	out := domain.KnowledgeFilePage{Items: make([]domain.KnowledgeFile, 0, len(data.List)), Total: data.Total}
	for i := range data.List {
		out.Items = append(out.Items, data.List[i].toDomain())
	}
	return out, nil
}

// AddKnowledgeFiles 以 cloud JSON 形式添加文件（URL 或 base64/data URL）。
func (c *Client) AddKnowledgeFiles(ctx context.Context, in domain.KnowledgeFilesAdd) ([]domain.KnowledgeFile, error) {
	items := make([]map[string]any, 0, len(in.Files))
	for _, f := range in.Files {
		item := map[string]any{"name": f.Name, "content": f.Content}
		if f.Type != "" {
			item["type"] = f.Type
		}
		items = append(items, item)
	}
	payload := map[string]any{
		"mem_cube_id":      in.TenantID,
		"knowledgebase_id": in.KnowledgeBaseID,
		"file":             items,
	}
	if in.FileType != "" {
		payload["type"] = in.FileType
	}
	env, err := c.postCloud(ctx, "/product/add_knowledgebase_file", payload)
	if err != nil {
		return nil, err
	}
	var raws []rawKBFile
	if err := decodeData(env, &raws); err != nil {
		return nil, err
	}
	out := make([]domain.KnowledgeFile, 0, len(raws))
	for i := range raws {
		out = append(out, raws[i].toDomain())
	}
	return out, nil
}

// DeleteKnowledgeFiles 删除文件，返回实际删除数量。
func (c *Client) DeleteKnowledgeFiles(ctx context.Context, tenant string, fileIDs []string) (int, error) {
	if len(fileIDs) == 0 {
		return 0, domain.Invalid("knowledgebase", "fileIds 不能为空")
	}
	env, err := c.postCloud(ctx, "/product/delete_knowledgebase_file", map[string]any{
		"mem_cube_id": tenant,
		"file_ids":    fileIDs,
	})
	if err != nil {
		return 0, err
	}
	var data struct {
		Deleted int `json:"deleted"`
	}
	if err := decodeData(env, &data); err != nil {
		return 0, err
	}
	return data.Deleted, nil
}

// DownloadKnowledgeFile 读取文件字节与原始文件名。上游出错时返回 JSON，这里统一
// 映射为错误；成功响应没有信封，直接是文件流。
func (c *Client) DownloadKnowledgeFile(ctx context.Context, tenant, fileID string) ([]byte, string, error) {
	if strings.TrimSpace(fileID) == "" {
		return nil, "", domain.Invalid("knowledgebase", "缺少文件 ID")
	}
	body, err := json.Marshal(map[string]any{"mem_cube_id": tenant, "file_id": fileID})
	if err != nil {
		return nil, "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/product/download_knowledgebase_file", bytes.NewReader(body))
	if err != nil {
		return nil, "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, "", domain.Wrap(domain.ErrUpstreamUnavailable, "upstream", "暂时连不上记忆服务，请稍后重试。", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBytes))
		return nil, "", mapHTTPError(resp.StatusCode, raw)
	}
	content, err := io.ReadAll(io.LimitReader(resp.Body, c.maxBytes+1))
	if err != nil {
		return nil, "", domain.Wrap(domain.ErrUpstreamUnavailable, "upstream", "读取记忆服务响应失败", err)
	}
	if int64(len(content)) > c.maxBytes {
		return nil, "", domain.Wrap(domain.ErrUpstreamRejected, "upstream", "文件过大，已拒绝", nil)
	}
	name := filenameFromDisposition(resp.Header.Get("Content-Disposition"))
	return content, name, nil
}

func filenameFromDisposition(header string) string {
	const marker = `filename*=UTF-8''`
	if _, after, ok := strings.Cut(header, marker); ok {
		raw := strings.TrimSpace(strings.SplitN(after, ";", 2)[0])
		if decoded, err := url.PathUnescape(raw); err == nil && decoded != "" {
			return decoded
		}
	}
	const plain = `filename="`
	if _, after, ok := strings.Cut(header, plain); ok {
		rest := after
		if before, _, ok := strings.Cut(rest, `"`); ok {
			return before
		}
	}
	return ""
}
