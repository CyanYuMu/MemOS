package dashboard

import (
	"context"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	pageutil "github.com/hugegraph/MemOS/apps/memory-console-be/internal/platform/page"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

const maxKnowledgeNameRunes = 128

func knowledgeBaseID(body map[string]any) string {
	for _, key := range []string{"knowledgeBaseId", "id"} {
		if id := strings.TrimSpace(httpx.String(body[key])); id != "" {
			return id
		}
	}
	return ""
}

func (s *Server) kbList(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	items, err := s.deps.Knowledge.KnowledgeBases(ctx, projectID)
	if err != nil {
		return nil, err
	}
	name := strings.ToLower(strings.TrimSpace(httpx.String(body["name"])))
	filtered := make([]domain.KnowledgeBase, 0, len(items))
	for _, kb := range items {
		if name == "" || strings.Contains(strings.ToLower(kb.Name), name) {
			filtered = append(filtered, kb)
		}
	}
	page, size := pageOf(body)
	records := make([]map[string]any, 0, len(filtered))
	for _, kb := range filtered {
		records = append(records, kbJSON(kb, projectID))
	}
	return map[string]any{
		"records": pageutil.Slice(records, page, size),
		"total":   len(records),
		"current": page,
		"size":    size,
	}, nil
}

func (s *Server) kbDetail(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	id := knowledgeBaseID(body)
	if id == "" {
		return nil, domain.Invalid("knowledgebase", "缺少 knowledgeBaseId")
	}
	kb, err := s.deps.Knowledge.KnowledgeBase(ctx, projectID, id)
	if err != nil {
		return nil, err
	}
	return kbJSON(kb, projectID), nil
}

func (s *Server) kbCreate(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	name, err := knowledgeName(body)
	if err != nil {
		return nil, err
	}
	kb, err := s.deps.Knowledge.CreateKnowledgeBase(ctx, projectID, name, strings.TrimSpace(httpx.String(body["knowledgebaseDescription"])))
	if err != nil {
		return nil, err
	}
	return kbJSON(kb, projectID), nil
}

func (s *Server) kbUpdate(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	id := knowledgeBaseID(body)
	if id == "" {
		return nil, domain.Invalid("knowledgebase", "缺少 knowledgeBaseId")
	}
	var name, description *string
	if raw, present := body["knowledgebaseName"]; present {
		value := strings.TrimSpace(httpx.String(raw))
		if value == "" {
			return nil, domain.Invalid("knowledgebase", "知识库名称不能为空")
		}
		if len([]rune(value)) > maxKnowledgeNameRunes {
			return nil, domain.Invalid("knowledgebase", "知识库名称过长")
		}
		name = &value
	}
	if raw, present := body["knowledgebaseDescription"]; present {
		value := strings.TrimSpace(httpx.String(raw))
		description = &value
	}
	if name == nil && description == nil {
		return nil, domain.Invalid("knowledgebase", "没有需要修改的字段")
	}
	kb, err := s.deps.Knowledge.UpdateKnowledgeBase(ctx, domain.KnowledgeBaseUpdate{
		TenantID:    projectID,
		ID:          id,
		Name:        name,
		Description: description,
	})
	if err != nil {
		return nil, err
	}
	return kbJSON(kb, projectID), nil
}

func (s *Server) kbDelete(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	id := knowledgeBaseID(body)
	if id == "" {
		return nil, domain.Invalid("knowledgebase", "缺少 knowledgeBaseId")
	}
	if err := s.deps.Knowledge.DeleteKnowledgeBase(ctx, projectID, id); err != nil {
		return nil, err
	}
	return map[string]any{"ok": true, "id": id}, nil
}

func (s *Server) kbFileList(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	id := knowledgeBaseID(body)
	if id == "" {
		return nil, domain.Invalid("knowledgebase", "缺少 knowledgeBaseId")
	}
	page, size := pageOf(body)
	files, err := s.deps.Knowledge.KnowledgeFiles(ctx, domain.KnowledgeFilesQuery{
		TenantID:        projectID,
		KnowledgeBaseID: id,
		Page:            page,
		Size:            size,
	})
	if err != nil {
		return nil, err
	}
	records := make([]map[string]any, 0, len(files.Items))
	for _, f := range files.Items {
		records = append(records, fileJSON(f))
	}
	return map[string]any{"records": records, "total": files.Total, "current": page, "size": size}, nil
}

func (s *Server) kbFileAdd(ctx context.Context, _ domain.Principal, _ *http.Request, body map[string]any) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	id := knowledgeBaseID(body)
	if id == "" {
		return nil, domain.Invalid("knowledgebase", "缺少 knowledgeBaseId")
	}
	files, err := knowledgeFiles(body)
	if err != nil {
		return nil, err
	}
	added, err := s.deps.Knowledge.AddKnowledgeFiles(ctx, domain.KnowledgeFilesAdd{
		TenantID:        projectID,
		KnowledgeBaseID: id,
		Files:           files,
		FileType:        strings.TrimSpace(httpx.String(body["type"])),
	})
	if err != nil {
		return nil, err
	}
	out := make([]map[string]any, 0, len(added))
	for _, f := range added {
		out = append(out, fileJSON(f))
	}
	return map[string]any{"records": out, "total": len(out)}, nil
}

func (s *Server) kbFileDelete(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	projectID := strings.TrimSpace(httpx.String(body["projectId"]))
	ids := knowledgeFileIDs(body)
	if len(ids) == 0 {
		return nil, domain.Invalid("knowledgebase", "fileIds 不能为空")
	}
	deleted, err := s.deps.Knowledge.DeleteKnowledgeFiles(ctx, projectID, ids)
	if err != nil {
		return nil, err
	}
	return map[string]any{"ok": deleted > 0, "deleted": deleted}, nil
}

// kbFileDownload 是唯一不套 JSON 信封的接口：直接把上游文件流给浏览器。
// 授权与读取顺序与其它路由一致（先解析主体，再按项目判权，最后才打上游）。
func (s *Server) kbFileDownload(w http.ResponseWriter, r *http.Request) {
	p, err := s.deps.Accounts.Authenticate(r.Context(), httpx.CredentialsOf(r))
	if err == nil {
		err = p.RequireCap(strings.TrimSpace(r.URL.Query().Get("projectId")), domain.CapProjectView)
	}
	fileID := strings.TrimSpace(r.URL.Query().Get("fileId"))
	if err == nil && fileID == "" {
		err = domain.Invalid("knowledgebase", "缺少 fileId")
	}
	if err != nil {
		s.writeError(w, err)
		return
	}
	content, name, err := s.deps.Knowledge.DownloadKnowledgeFile(
		r.Context(), strings.TrimSpace(r.URL.Query().Get("projectId")), fileID)
	if err != nil {
		s.writeError(w, err)
		return
	}
	if name == "" {
		name = fileID
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Disposition", contentDisposition(name))
	w.Header().Set("Cache-Control", "no-store")
	//nolint:gosec // 用户上传的原始字节，按附件下载且带 octet-stream，不在页面里渲染
	_, _ = w.Write(content)
}

func contentDisposition(name string) string {
	clean := strings.NewReplacer("\"", "", "\r", "", "\n", "").Replace(name)
	ascii := clean
	if strings.ContainsFunc(clean, func(r rune) bool { return r > 127 }) {
		ascii = "download"
	}
	return `attachment; filename="` + ascii + `"; filename*=UTF-8''` + url.PathEscape(clean)
}

func knowledgeName(body map[string]any) (string, error) {
	name := strings.TrimSpace(httpx.String(body["knowledgebaseName"]))
	if name == "" {
		return "", domain.Invalid("knowledgebase", "知识库名称不能为空")
	}
	if len([]rune(name)) > maxKnowledgeNameRunes {
		return "", domain.Invalid("knowledgebase", "知识库名称过长")
	}
	return name, nil
}

func knowledgeFiles(body map[string]any) ([]domain.KnowledgeFileInput, error) {
	raw, ok := body["files"].([]any)
	if !ok {
		single, present := body["file"].(map[string]any)
		if !present {
			return nil, domain.Invalid("knowledgebase", "file 或 files 不能为空")
		}
		raw = []any{single}
	}
	out := make([]domain.KnowledgeFileInput, 0, len(raw))
	for _, item := range raw {
		entry, ok := item.(map[string]any)
		if !ok {
			return nil, domain.Invalid("knowledgebase", "file 必须是对象")
		}
		name := strings.TrimSpace(httpx.String(entry["name"]))
		content := httpx.String(entry["content"])
		if name == "" || content == "" {
			return nil, domain.Invalid("knowledgebase", "文件必须包含 name 与 content")
		}
		out = append(out, domain.KnowledgeFileInput{Name: name, Content: content, Type: httpx.String(entry["type"])})
	}
	if len(out) == 0 {
		return nil, domain.Invalid("knowledgebase", "至少需要一个文件")
	}
	return out, nil
}

func knowledgeFileIDs(body map[string]any) []string {
	raw, ok := body["fileIds"].([]any)
	if !ok {
		if id := strings.TrimSpace(httpx.String(body["fileId"])); id != "" {
			return []string{id}
		}
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		if id := strings.TrimSpace(httpx.String(item)); id != "" {
			out = append(out, id)
		}
	}
	return out
}

func kbJSON(kb domain.KnowledgeBase, projectID string) map[string]any {
	return map[string]any{
		"kbId":                     kb.ID,
		"knowledgebaseId":          kb.ID,
		"knowledgebaseName":        kb.Name,
		"knowledgebaseDescription": kb.Description,
		"fileCount":                kb.FileCount,
		"totalSize":                kb.TotalSize,
		"createTime":               formatTime(kb.CreatedAt),
		"updateTime":               formatTime(kb.UpdatedAt),
		"projectId":                projectID,
	}
}

func fileJSON(f domain.KnowledgeFile) map[string]any {
	return map[string]any{
		"id":              f.ID,
		"fileId":          f.ID,
		"knowledgebaseId": f.KnowledgeBaseID,
		"name":            f.Name,
		"type":            f.Type,
		"status":          f.Status,
		"size":            f.Size,
		"sizeMB":          f.SizeMB,
		"createTime":      formatTime(f.CreatedAt),
	}
}

func formatTime(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return domain.FormatBeijing(t)
}

// kbNoAssociation：知识库按项目（上游 mem_cube_id）隔离，不存在关联/解除关联。
func kbNoAssociation(context.Context, domain.Principal, *http.Request, map[string]any) (any, error) {
	return nil, domain.Wrap(domain.ErrConflict, "knowledgebase", "知识库按项目隔离，无需关联或移除", nil)
}

// kbMemoryUnavailable：上游只有知识库与文件管理，没有按知识库召回记忆的接口。
func kbMemoryUnavailable(context.Context, domain.Principal, *http.Request, map[string]any) (any, error) {
	return nil, disabledCapability("知识库记忆召回")
}
