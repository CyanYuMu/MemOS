package dashboard

import (
	"context"
	"maps"
	"net/http"
	"strings"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/transport/httpx"
)

// profileMetaKeys 是模板对象里的身份与分页字段；其余顶层键都是属性树分组。
var profileMetaKeys = []string{"templateId", "templateName", "projectId", "bindingCount", "updatedAt"}

func (s *Server) profileList(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	if s.deps.Templates == nil {
		return nil, disabledCapability("上游")
	}
	projectID := httpx.String(body["projectId"])
	page, size := pageOf(body)
	found, err := s.deps.Templates.ListProfileTemplates(ctx, projectID, page, size)
	if err != nil {
		return nil, err
	}
	list := make([]map[string]any, 0, len(found.Items))
	for i := range found.Items {
		list = append(list, profileTemplateJSON(found.Items[i], projectID))
	}
	pages := max(1, (found.Total+size-1)/size)
	return map[string]any{"list": list, "total": found.Total, "current": page, "size": size, "pages": pages}, nil
}

func (s *Server) profileDetail(
	ctx context.Context,
	_ domain.Principal,
	r *http.Request,
	body map[string]any,
) (any, error) {
	if s.deps.Templates == nil {
		return nil, disabledCapability("上游")
	}
	query := r.URL.Query()
	id := strings.TrimSpace(query.Get("templateId"))
	if id == "" {
		id = strings.TrimSpace(query.Get("id"))
	}
	if id == "" {
		return nil, domain.Invalid("dashboard", "templateId 不能为空")
	}
	projectID := httpx.String(body["projectId"])
	tpl, err := s.deps.Templates.GetProfileTemplate(ctx, projectID, id)
	if err != nil {
		return nil, err
	}
	return profileTemplateJSON(tpl, projectID), nil
}

func (s *Server) profileCreate(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	if s.deps.Templates == nil {
		return nil, disabledCapability("上游")
	}
	name := strings.TrimSpace(httpx.String(body["templateName"]))
	if name == "" {
		return nil, domain.Invalid("dashboard", "templateName 不能为空")
	}
	groups := profileGroups(body)
	if len(groups) == 0 {
		return nil, domain.Invalid("dashboard", "至少需要一个属性分组")
	}
	id, err := s.deps.Templates.AddProfileTemplate(ctx, httpx.String(body["projectId"]), name, groups)
	if err != nil {
		return nil, err
	}
	return map[string]any{"profile_template_id": id}, nil
}

func (s *Server) profileUpdate(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	if s.deps.Templates == nil {
		return nil, disabledCapability("上游")
	}
	id := strings.TrimSpace(httpx.String(body["templateId"]))
	if id == "" {
		return nil, domain.Invalid("dashboard", "templateId 不能为空")
	}
	update := domain.ProfileTemplateUpdate{Metadata: profileGroups(body)}
	if _, ok := body["templateName"]; ok {
		name := strings.TrimSpace(httpx.String(body["templateName"]))
		if name == "" {
			return nil, domain.Invalid("dashboard", "templateName 不能为空")
		}
		update.Name = &name
	}
	if update.Name == nil && len(update.Metadata) == 0 {
		return nil, domain.Invalid("dashboard", "templateName 与属性分组至少提供一项")
	}
	got, err := s.deps.Templates.UpdateProfileTemplate(ctx, httpx.String(body["projectId"]), id, update)
	if err != nil {
		return nil, err
	}
	return map[string]any{"profile_template_id": got}, nil
}

func (s *Server) profileDelete(
	ctx context.Context,
	_ domain.Principal,
	_ *http.Request,
	body map[string]any,
) (any, error) {
	if s.deps.Templates == nil {
		return nil, disabledCapability("上游")
	}
	id := strings.TrimSpace(httpx.String(body["templateId"]))
	if id == "" {
		return nil, domain.Invalid("dashboard", "templateId 不能为空")
	}
	if err := s.deps.Templates.DeleteProfileTemplate(ctx, httpx.String(body["projectId"]), id); err != nil {
		return nil, err
	}
	return map[string]any{"success": true}, nil
}

// profileTemplateJSON 先铺属性树分组，再写身份字段：分组是用户数据，不能覆盖身份与分页字段。
func profileTemplateJSON(tpl domain.ProfileTemplate, projectID string) map[string]any {
	out := make(map[string]any, len(tpl.Metadata)+5)
	maps.Copy(out, tpl.Metadata)
	out["templateId"] = tpl.ID
	out["templateName"] = tpl.Name
	out["projectId"] = projectID
	out["bindingCount"] = tpl.BoundCount
	if tpl.UpdatedAt != "" {
		out["updatedAt"] = tpl.UpdatedAt
	}
	return out
}

func profileGroups(body map[string]any) map[string]any {
	groups := maps.Clone(body)
	for _, key := range profileMetaKeys {
		delete(groups, key)
	}
	return groups
}
