package domain

// ProfileTemplate 是租户下的画像属性树模板，权威数据在上游，控制面不落库。
type ProfileTemplate struct {
	ID         string
	Name       string
	Metadata   map[string]any
	BoundCount int
	UpdatedAt  string
}

// ProfileTemplatePage 是一页模板与总数。
type ProfileTemplatePage struct {
	Items []ProfileTemplate
	Total int
}

// ProfileTemplateUpdate 是模板的部分更新：nil 表示不改。
type ProfileTemplateUpdate struct {
	Name     *string
	Metadata map[string]any
}
