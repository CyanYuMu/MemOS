export interface NavItem {
  label: string
  to: string
  icon?: string
  /** 选中态图标类名；缺省时沿用 icon */
  iconActive?: string
  /** 少数条目用的是位图而不是图标字体 */
  img?: string
  /** 只有平台管理员能看到/进入（接口本身也按 `auth.Platform` 授权，这里只是别让人点进来吃 401） */
  adminOnly?: boolean
  /** 该页还没开放（页面本身渲染 `ComingSoon`）。渲染成**置灰、不可点**的条目。
   *
   *  ⚠️ 加了这个标记的条目**仍然是链接**，只是点了不导航（见 Sidebar）——
   *  不用 `<span>` 代替 `<Link>`：同一条导航项在普通/当前两种状态下必须是同一个元素，
   *  否则侧栏的布局与图标选中态会跟着变。 */
  disabled?: boolean
  /** 暂时不在侧栏展示；页面与路由保留，直接访问地址仍可进入 */
  hidden?: boolean
}

export interface NavGroup {
  title: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    title: 'group.接入',
    items: [
      { label: 'nav.quickstart', to: '/cn/quickstart', icon: 'i-db-quickstart', iconActive: 'i-db-quickstart-fill' },
      { label: 'nav.apikeys', to: '/cn/apikeys', icon: 'i-db-key', iconActive: 'i-db-key-fill' },
    ],
  },
  {
    title: 'group.资源',
    items: [
      { label: 'nav.requests', to: '/cn/requests', icon: 'i-db-log', iconActive: 'i-db-log-fill' },
      { label: 'nav.chat', to: '/cn/chat', icon: 'i-lucide:sparkles' },
      { label: 'nav.knowledgeBase', to: '/cn/knowledgeBase', icon: 'i-db-knowledge-base', disabled: true },
      { label: 'nav.memoryList', to: '/cn/memoryList', img: '/icons/memory-list.svg' },
      { label: 'nav.localMemories', to: '/cn/localMemories', icon: 'i-lucide:plug', hidden: true },
      { label: 'nav.memoryGraph', to: '/cn/memoryGraph', icon: 'i-db-graph' },
      { label: 'nav.skills', to: '/cn/skills', icon: 'i-db-skill', iconActive: 'i-db-skill-fill' },
      { label: 'nav.models', to: '/cn/models', icon: 'i-db-model', iconActive: 'i-db-model-fill' },
    ],
  },
  {
    title: 'group.设置',
    items: [
      { label: 'nav.projects', to: '/cn/projects', icon: 'i-db-project' },
      { label: 'nav.profileTemplate', to: '/cn/profileTemplate', icon: 'i-db-book', disabled: true },
      { label: 'nav.prompts', to: '/cn/prompts', icon: 'i-db-skill', iconActive: 'i-db-skill-fill' },
      { label: 'nav.usage', to: '/cn/usage', icon: 'i-db-chart' },
      { label: 'nav.jobs', to: '/cn/jobs', icon: 'i-db-log', adminOnly: true },
    ],
  },
]

/* 这里原来还有 `PAGE_TITLES` 与 `pageTitleKey()`，是给顶栏的面包屑用的。
 * 2026-10-19 顶栏整体去掉（用户要求），这两个导出一起删掉——留着就是没人引用的死代码，
 * 而它们指向的 `nav.*` 键会被字典校验判成「死文案」，反而掩盖真正的问题。 */
