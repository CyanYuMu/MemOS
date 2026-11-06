import { useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { PlatformGuideEntry } from './onboarding/PlatformGuide'
import { Logo } from './Logo'
import { Icon } from './Icon'
import { NAV_GROUPS } from '../lib/nav'
import { useLang, useMeta, useProject, useStr, useT, useToast } from '../lib/store'
import { useTheme, type ThemePref } from '../lib/theme'
import { LINKS } from '../lib/config'
import { errText, type TKey } from '../lib/i18n'

/** 侧栏组件的作用域属性，组件级 CSS 依赖它命中 */
const S = { 'data-v-bc08aa60': '' } as const
const U = { 'data-v-4079c7a3': '' } as const

const COLLAPSE_KEY = 'console.sidebar.collapsed'
const W_EXPANDED = 220
const W_COLLAPSED = 68
const RAIL_ITEM = 44

export function Sidebar({ mobile = false, onNavigate }: { mobile?: boolean; onNavigate?: () => void }) {
  const { pathname } = useLocation()
  const t = useT()
  const str = useStr()
  const { currentProjectId, currentProjectName, projects, setCurrent } = useProject()
  const { canLogout, account, realAdmin, previewUser, setPreviewUser } = useMeta()
  const { lang, setLang } = useLang()
  const toast = useToast()
  const { pref: themePref, setPref: setThemePref } = useTheme()
  const [collapsePref, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === '1')
  const collapsed = !mobile && collapsePref
  const [pickerOpen, setPickerOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const pickerRef = useRef<HTMLDivElement>(null)
  const accountRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const node = e.target as Node
      if (pickerRef.current && !pickerRef.current.contains(node)) setPickerOpen(false)
      if (accountRef.current && !accountRef.current.contains(node)) setAccountOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [])

  const toggleCollapse = () => {
    setCollapsed((c) => {
      localStorage.setItem(COLLAPSE_KEY, c ? '0' : '1')
      return !c
    })
    setPickerOpen(false)
  }

  const currentName = currentProjectName || t('nav.selectProject')
  const accountName = account?.displayName?.trim() || account?.username || t('nav.account')
  const accountInitial = (account?.displayName?.trim() || account?.username || '').charAt(0).toUpperCase() || '?'

  return (
    <aside
      {...S}
      aria-label={t('nav.navigation')}
      onClick={(event) => {
        const link = (event.target as Element).closest('a[href^="/cn/"]')
        // Router 的正常导航也会 preventDefault，禁用项要按自身状态判断。
        if (link && link.getAttribute('aria-disabled') !== 'true') onNavigate?.()
      }}
      style={{ width: collapsed ? W_COLLAPSED : W_EXPANDED }}
      className={`console-sidebar${mobile ? ' mobile' : ''} flex flex-col h-screen min-h-0 shrink-0 bg-surface-muted/80 border-r border-line transition-all duration-300 ${
        collapsed ? 'px-0 items-center' : 'px-3'
      }`}
    >
      <div {...S} className={`h-14 shrink-0 flex items-center ${collapsed ? 'justify-center' : 'px-1'}`}>
        <Link {...S} to="/cn/quickstart" aria-label={t('brand.name')} className="flex items-center">
          <Logo compact={collapsed} />
        </Link>
      </div>

      <PlatformGuideEntry compact={collapsed} />
      <div
        {...S}
        id={mobile ? 'console-mobile-project-picker' : 'playground-projects'}
        data-guide="project"
        className={`console-project-picker w-full shrink-0 mt-3 relative ${collapsed ? 'flex justify-center' : ''}`}
        ref={pickerRef}
      >
        {collapsed ? (
          <button
            type="button"
            title={currentName}
            aria-label={t('nav.selectProject')}
            aria-expanded={pickerOpen}
            onClick={() => setPickerOpen((o) => !o)}
            style={{ width: RAIL_ITEM, height: 40 }}
            className="rounded-lg border border-line bg-surface hover:bg-surface-hover/80 flex items-center justify-center cursor-pointer text-ink-secondary transition-colors shadow-2xs"
          >
            <span className="iconify i-db:arrow-right-left size-4" aria-hidden="true" />
          </button>
        ) : (
          <button
            type="button"
            aria-label={t('nav.selectProject')}
            aria-expanded={pickerOpen}
            onClick={() => setPickerOpen((o) => !o)}
            className="w-full flex items-center justify-between gap-2 h-10 px-3 rounded-lg border border-line bg-surface hover:bg-surface-muted text-ink transition-all cursor-pointer shadow-2xs text-left"
          >
            <div className="flex items-center gap-2.5 min-w-0">
              <span className="size-5.5 rounded-md bg-accent-soft border border-accent/30 text-accent text-xs font-semibold flex items-center justify-center shrink-0">
                {currentName.charAt(0).toUpperCase()}
              </span>
              <span className="truncate text-sm font-medium text-ink">{currentName}</span>
            </div>
            <Icon name="i-lucide:chevrons-up-down" className="size-4 text-ink-faint shrink-0" />
          </button>
        )}

        {pickerOpen && (
          <div
            className="absolute z-50 mt-1 rounded-xl border border-line bg-surface shadow-lg p-1.5 backdrop-blur-md"
            style={collapsed ? { left: 0, top: '100%', width: 220 } : { left: 0, right: 0, top: '100%' }}
          >
            {projects.filter((p) => !p.disabled).length === 0 && (
              <div className="px-3 py-2 text-sm text-ink-muted">{t('nav.noProject')}</div>
            )}
            {projects
              .filter((p) => !p.disabled)
              .map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className={`w-full text-left px-3 py-2 text-sm rounded-lg transition-colors flex items-center justify-between ${
                    currentProjectId === p.id
                      ? 'bg-accent-soft text-accent font-medium'
                      : 'text-ink-secondary hover:bg-surface-hover hover:text-ink'
                  }`}
                  onClick={() => {
                    // 切换失败要让人看见：以前这里静默吞掉，界面已经切过去、
                    // 后端却没切，页面只剩一片 403。
                    setCurrent(p.id)
                      .then(() => onNavigate?.())
                      .catch((e) => toast(errText(lang, e), 'err'))
                      .finally(() => setPickerOpen(false))
                  }}
                >
                  <span className="truncate">{p.name}</span>
                  {currentProjectId === p.id && <Icon name="i-lucide:check" className="ml-2 size-4 shrink-0 text-accent" />}
                </button>
              ))}
            <div className="my-1 border-t border-line" />
            <Link
              to="/cn/projects"
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-ink-secondary transition-colors hover:bg-surface-hover hover:text-ink"
              onClick={() => setPickerOpen(false)}
            >
              <Icon name="i-db-project" className="size-4 shrink-0 text-ink-muted" />
              {t('nav.projects')}
            </Link>
            {account?.platformAdmin && (
              <Link
                to="/cn/projects?create=1"
                className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-ink-secondary transition-colors hover:bg-surface-hover hover:text-ink"
                onClick={() => setPickerOpen(false)}
              >
                <Icon name="i-lucide:plus" className="size-4 shrink-0 text-ink-muted" />
                {t('onboarding.create')}
              </Link>
            )}
          </div>
        )}
      </div>

      <div
        {...S}
        className={`dashboard-aside-scroll w-full flex-1 min-h-0 mt-3 space-y-4 overflow-y-auto overflow-x-hidden ${
          collapsed ? 'dashboard-aside-scroll--rail flex flex-col items-center' : ''
        }`}
      >
        <div {...S} className={`space-y-4 ${collapsed ? 'flex flex-col items-center' : 'w-full'}`}>
          {NAV_GROUPS.map((group) => (
            <div key={group.title} className={`space-y-1 ${collapsed ? 'flex flex-col items-center' : 'w-full'}`}>
              {!collapsed && (
                <div className="px-2.5 pb-1 text-xs font-semibold tracking-wider uppercase text-ink-muted">{str(group.title as TKey)}</div>
              )}
              <ul className={`space-y-0.5 ${collapsed ? 'flex flex-col items-center' : ''}`}>
                {group.items
                  .filter((item) => !item.hidden)
                  .filter((item) => !item.adminOnly || account?.platformAdmin)
                  .map((item) => {
                    const active = pathname.replace(/\/$/, '') === item.to
                    /* 还没开放的页（`ComingSoon`）：保持原样显示但置灰不可点击。
                     hover 提示要说明**为什么点不动**——只显示页面名字，用户会以为是自己点歪了。 */
                    const off = item.disabled === true
                    const labelText = str(item.label as TKey)
                    return (
                      <li
                        key={item.to}
                        title={off ? `${labelText} · ${str('common.comingSoon')}` : labelText}
                        style={collapsed ? { width: RAIL_ITEM } : undefined}
                        className={`console-nav-item${active ? ' is-active' : ''} group rounded-lg border transition-colors duration-150 ${
                          off
                            ? 'border-transparent opacity-40 cursor-not-allowed'
                            : active
                              ? 'bg-accent-soft text-ink font-semibold shadow-2xs border-accent/25'
                              : 'border-transparent text-ink-secondary hover:bg-surface-hover hover:text-ink'
                        }`}
                      >
                        <Link
                          to={item.to}
                          aria-label={labelText}
                          aria-current={active ? 'page' : undefined}
                          aria-disabled={off || undefined}
                          tabIndex={off ? -1 : undefined}
                          onClick={off ? (e) => e.preventDefault() : undefined}
                          className={`console-nav-link flex items-center gap-2.5 px-2.5 py-1.5 text-sm font-medium w-full rounded-lg transition-colors ${
                            collapsed ? 'justify-center px-0' : ''
                          } ${off ? 'cursor-not-allowed' : ''}`}
                        >
                          {item.img ? (
                            <div className="size-4.5 flex items-center justify-center shrink-0">
                              <img src={item.img} className="size-full object-contain" alt="" />
                            </div>
                          ) : (
                            <Icon
                              name={active && item.iconActive ? item.iconActive : item.icon!}
                              className={`size-4.5 shrink-0 transition-colors ${
                                active ? 'text-accent' : 'text-ink-muted group-hover:text-ink'
                              }`}
                            />
                          )}
                          {!collapsed && <span className="truncate">{labelText}</span>}
                        </Link>
                      </li>
                    )
                  })}
              </ul>
            </div>
          ))}
        </div>
      </div>

      <div {...S} className={`shrink-0 ${collapsed ? 'flex flex-col items-center w-full' : ''}`}>
        <button
          type="button"
          title={collapsed ? t('nav.expand') : t('nav.collapse')}
          aria-label={collapsed ? t('nav.expand') : t('nav.collapse')}
          className={`console-collapse flex items-center gap-2.5 w-full px-2.5 py-1.5 rounded-lg text-sm font-medium text-ink-muted hover:text-ink hover:bg-surface-hover transition-colors cursor-pointer ${
            collapsed ? 'justify-center px-0' : ''
          }`}
          style={collapsed ? { width: RAIL_ITEM } : undefined}
          onClick={toggleCollapse}
        >
          <Icon name="i-ll-fold" className="size-4.5 shrink-0 text-ink-muted" />
          {!collapsed && <span className="truncate text-ink-secondary">{t('nav.collapse')}</span>}
        </button>

        {/* ⚠️ 收起态必须 `mx-auto`：外层是 `w-full`，链接本身只有 44px 宽，
            不加的话它贴在左边，图标中心落在 22px，而上面的导航图标在 33.5px（侧栏中线）。 */}
        <div className="w-full mt-1">
          <Link
            to={LINKS.docs}
            title={t('nav.docs')}
            style={collapsed ? { width: RAIL_ITEM } : undefined}
            className={`flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg text-sm font-medium text-ink-muted hover:text-ink hover:bg-surface-hover transition-colors ${
              collapsed ? 'justify-center px-0 mx-auto' : ''
            }`}
          >
            <Icon name="i-db-book" className="size-4.5 shrink-0 text-ink-muted" />
            {!collapsed && <span className="truncate">{t('nav.docs')}</span>}
          </Link>
        </div>

        {/* 账号菜单 */}
        <div {...U} ref={accountRef} className="relative mt-2 pt-2 border-t border-line w-full">
          <button
            {...U}
            type="button"
            title={accountName}
            aria-expanded={accountOpen}
            onClick={() => setAccountOpen((o) => !o)}
            style={{ width: collapsed ? RAIL_ITEM : undefined }}
            className={`flex items-center cursor-pointer gap-2.5 w-full rounded-lg p-2 transition-colors hover:bg-surface-hover ${
              collapsed ? 'justify-center mx-auto' : 'justify-between'
            }`}
          >
            <div className="flex items-center gap-2.5 min-w-0">
              <div className="rounded-full size-7 bg-line text-ink-secondary font-semibold text-xs flex items-center justify-center shrink-0 border border-line-strong">
                {accountInitial}
              </div>
              {!collapsed && <div className="truncate text-sm font-medium text-ink-secondary text-left">{accountName}</div>}
            </div>
            {!collapsed && (
              <Icon
                name="i-lucide:chevron-up"
                className={`size-4 text-ink-faint shrink-0 transition-transform ${accountOpen ? 'rotate-180' : ''}`}
              />
            )}
          </button>

          {accountOpen && (
            <div
              {...U}
              className="absolute z-50 bottom-full mb-1.5 rounded-xl border border-line bg-surface shadow-lg p-1.5 backdrop-blur-md"
              style={collapsed ? { left: '100%', bottom: 0, marginLeft: 8, width: 160 } : { left: 0, right: 0 }}
            >
              {account && (
                <div className="px-3 py-2 border-b border-line mb-1">
                  <div className="truncate text-sm font-medium text-ink">{accountName}</div>
                  {account.username && account.username !== accountName && (
                    <div className="truncate text-xs text-ink-muted">{account.username}</div>
                  )}
                </div>
              )}
              <Link
                {...U}
                to={LINKS.about}
                className="w-full text-left px-3 py-2 text-sm text-ink-secondary hover:bg-surface-hover hover:text-ink rounded-lg transition-colors block"
                onClick={() => setAccountOpen(false)}
              >
                {t('nav.about')}
              </Link>
              <button
                {...U}
                type="button"
                className="w-full text-left px-3 py-2 text-sm text-ink-secondary hover:bg-surface-hover hover:text-ink rounded-lg transition-colors flex items-center justify-between"
                onClick={() => {
                  setLang(lang === 'zh' ? 'en' : 'zh')
                  setAccountOpen(false)
                }}
              >
                <span>{t(lang === 'zh' ? 'lang.en' : 'lang.zh')}</span>
                <span className="text-xs text-ink-muted font-mono uppercase">{lang === 'zh' ? 'EN' : 'ZH'}</span>
              </button>

              {/* 外观：三档分段控件。用 title 而不是可见文字——侧栏收起时也要能操作，
                  而展开态下三个图标已经能看懂了。 */}
              <div {...U} className="px-3 pt-2 pb-2.5 border-t border-line mt-1">
                <div className="text-[11px] font-medium tracking-wider uppercase text-ink-muted mb-1.5">{t('theme.label')}</div>
                <div className="grid grid-cols-3 gap-0.5 rounded-lg bg-surface-hover/80 p-0.5 border border-line">
                  {(
                    [
                      ['light', 'i-lucide:sun'],
                      ['dark', 'i-lucide:moon'],
                      ['system', 'i-lucide:monitor'],
                    ] as [ThemePref, string][]
                  ).map(([key, icon]) => (
                    <button
                      key={key}
                      {...U}
                      type="button"
                      title={str(`theme.${key}` as TKey)}
                      aria-pressed={themePref === key}
                      className={`flex items-center justify-center h-6.5 rounded-md transition-all cursor-pointer ${
                        themePref === key ? 'bg-surface text-ink shadow-2xs' : 'text-ink-muted hover:text-ink'
                      }`}
                      onClick={() => setThemePref(key)}
                    >
                      <Icon name={icon} className="size-3.5" />
                    </button>
                  ))}
                </div>
              </div>
              {realAdmin && (
                <button
                  {...U}
                  type="button"
                  className="w-full text-left px-3 py-2 text-sm text-ink-secondary hover:bg-surface-hover hover:text-ink rounded-lg transition-colors border-t border-line mt-1 pt-2 flex items-center justify-between"
                  onClick={() => {
                    setPreviewUser(!previewUser)
                    setAccountOpen(false)
                  }}
                >
                  <span>{t(previewUser ? 'nav.exitPreview' : 'nav.previewUser')}</span>
                  {previewUser && <span className="text-[11px] font-medium text-warning">ON</span>}
                </button>
              )}
              {canLogout && (
                <button
                  {...U}
                  type="button"
                  className="w-full text-left px-3 py-2 text-sm text-danger hover:bg-danger-soft rounded-lg transition-colors border-t border-line mt-1 pt-2 block"
                  onClick={() => {
                    window.location.assign('/__logout')
                  }}
                >
                  {t('nav.logout')}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </aside>
  )
}
