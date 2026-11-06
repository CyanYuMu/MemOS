import { useCallback, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  api,
  type Account,
  type AdminAccount,
  type MemberCandidate,
  type Project,
  type ProjectInvite,
  type ProjectMember,
} from '../lib/api'
import { useAsync, usePaged } from '../lib/useAsync'
import { useLang, useMeta, useProject, useStr, useT, useToast } from '../lib/store'
import { errText, type TKey } from '../lib/i18n'
import { copyText } from '../lib/clipboard'
import { useLastValue } from '../lib/useLastValue'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Tabs } from '../components/ui/tabs'
import { Dialog, PageHeader, Pager, Panel, PrimaryButton, SearchBox, StateRow, Table, TD, TD_BREAK, Th } from '../components/ui'
import { RowActions, type RowAction } from '../components/ui/row-actions'
import { EmptyState } from '../components/ui/empty-state'
import { FilterSelect } from '../components/FilterPopover'
import { Icon } from '../components/Icon'
import { LocalDevices } from './LocalDevices'

const ROLES = ['viewer', 'editor', 'owner'] as const
type Role = (typeof ROLES)[number]
type TabKey = 'projects' | 'users' | 'devices'

const ROLE_KEY: Record<Role, TKey> = {
  viewer: 'projects.role.viewer',
  editor: 'projects.role.editor',
  owner: 'projects.role.owner',
}

const ROLE_HINT: Record<Role, TKey> = {
  viewer: 'projects.role.viewerHint',
  editor: 'projects.role.editorHint',
  owner: 'projects.role.ownerHint',
}

const NAME_RE = /^[A-Za-z0-9._-]{1,64}$/

function roleLabel(t: (k: TKey) => string, role: string) {
  const key = ROLE_KEY[role as Role]
  return key ? t(key) : role
}

function roleOptions(t: (k: TKey) => string) {
  return ROLES.map((r) => ({ value: r, label: t(ROLE_KEY[r]) }))
}

function projectStatus(p: Project, currentId: string): { label: TKey; tone: 'destructive' | 'success' | 'secondary' } {
  if (p.archived) return { label: 'projects.status.archived', tone: 'destructive' as const }
  if (p.disabled) return { label: 'projects.status.disabled', tone: 'destructive' as const }
  if (p.id === currentId) return { label: 'projects.status.current', tone: 'success' as const }
  return { label: 'projects.status.normal', tone: 'secondary' as const }
}

/** 判断哪一行是「当前项目」。
 *
 *  以 `/api/auth/me` 的 `currentProjectId` 为准：Go 端 `switchStatus` 判的就是主体的
 *  `CurrentTenant`，同一个字段。不能只看 `status === 'ENABLED'`——管理 Token 主体没有
 *  「当前项目」的概念，后端会给每个未停用项目都返回 ENABLED（见 memory-console-be
 *  `dashboard/projects.go` 的 switchStatus），那样整张表每行都会标成「当前」。
 *  `/api/auth/me` 不可用时（后端没有这个接口的旧版本）退回「恰好一条 ENABLED」的判定。 */
function resolveCurrentId(account: Account | null, rows: Project[]): string {
  if (account?.currentProjectId) return account.currentProjectId
  const enabled = rows.filter((p) => !p.disabled && p.status === 'ENABLED')
  return enabled.length === 1 ? enabled[0].id : ''
}

export function Projects() {
  const t = useT()
  const str = useStr()
  const { lang } = useLang()
  const toast = useToast()
  const { account, reloadAccount, previewUser } = useMeta()
  const isAdmin = account?.platformAdmin === true
  const { currentProjectId, currentProjectName, setCurrent, reload: reloadDict, ready } = useProject()
  const [searchParams, setSearchParams] = useSearchParams()
  /** 页签以 URL 为准：从「本地记忆」空态跳过来会带 `?tab=devices`，
   *  点侧栏回到 `/cn/projects` 时也要真的回到项目页签（同一个组件不会重挂）。 */
  const tabParam = searchParams.get('tab')
  const tab: TabKey = tabParam === 'users' || tabParam === 'devices' ? tabParam : 'projects'
  const [createOpen, setCreateOpen] = useState(false)
  const [editProject, setEditProject] = useState<Project | null>(null)
  const [memberProject, setMemberProject] = useState<Project | null>(null)
  const [assignUser, setAssignUser] = useState<AdminAccount | null>(null)
  const [pendingDisable, setPendingDisable] = useState<AdminAccount | null>(null)
  const [pendingDelete, setPendingDelete] = useState<Project | null>(null)
  const [pendingAccountDelete, setPendingAccountDelete] = useState<AdminAccount | null>(null)
  const [pendingProjectDisable, setPendingProjectDisable] = useState<Project | null>(null)
  const [userTick, setUserTick] = useState(0)

  const activeTab: TabKey = tab === 'users' && !isAdmin ? 'projects' : tab
  const switchTab = (key: TabKey) => {
    const next = new URLSearchParams(searchParams)
    if (key === 'projects') next.delete('tab')
    else next.set('tab', key)
    setSearchParams(next, { replace: true })
  }
  const createFromQuery = searchParams.get('create') === '1'
  const showCreate = createOpen || createFromQuery
  const closeCreate = () => {
    setCreateOpen(false)
    if (createFromQuery) setSearchParams({}, { replace: true })
  }

  const {
    data: projectPage,
    state: projectState,
    error: projectError,
    reload: reloadProjects,
  } = useAsync(() => api.project.list(1, 200), [currentProjectId], { key: 'projects.list' })

  const pg = usePaged(20)
  const {
    data: userPage,
    state: userState,
    error: userError,
    reload: reloadUsers,
  } = useAsync(() => api.admin.accounts.list(pg.q, pg.page, pg.pageSize), [pg.q, pg.page, pg.pageSize, userTick], {
    skip: activeTab !== 'users',
    key: 'admin.accounts',
  })

  // 预览普通用户视角时，接口仍按平台管理员返回全部项目；表格要再按真实成员
  // 关系裁一次，否则页面看起来还是「普通成员能看到所有项目」。
  const allProjectRows = projectPage?.records ?? []
  const projectRows = previewUser ? allProjectRows.filter((p) => p.member) : allProjectRows
  const userRows = userPage?.records ?? []
  const currentId = resolveCurrentId(account, projectRows)
  /** 吊销设备是项目写操作：后端 DELETE 要求 CapProjectWrite，editor 及以上即可（平台管理员不限）。
   *  角色必须从账号的项目列表取：`projectRows` 是当前分页，当前项目不在本页时会取空。 */
  const currentRole = account?.projects.find((p) => p.id === currentProjectId)?.role
  const canManageDevices = isAdmin || currentRole === 'owner' || currentRole === 'editor'

  const refreshProjects = useCallback(() => {
    reloadProjects()
    reloadDict()
  }, [reloadProjects, reloadDict])

  const copyId = async (id: string) => {
    try {
      await copyText(id)
      toast(t('common.copied'))
    } catch {
      toast(t('common.copyFailed'), 'err')
    }
  }

  const switchTo = async (id: string) => {
    try {
      // setCurrent 内部会重取 /api/auth/me，页面上的「当前」徽标随主体一起更新。
      await setCurrent(id)
      toast(t('projects.switched'))
      refreshProjects()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const confirmProjectDisable = async () => {
    if (!pendingProjectDisable) return
    const target = pendingProjectDisable
    setPendingProjectDisable(null)
    try {
      await api.project.update({ id: target.id, status: 'DISABLED' })
      toast(t('projects.disabled'))
      refreshProjects()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const enableProject = async (id: string) => {
    try {
      await api.project.update({ id, status: 'ENABLED' })
      toast(t('projects.enabled'))
      refreshProjects()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const confirmDelete = async () => {
    if (!pendingDelete) return
    const target = pendingDelete
    setPendingDelete(null)
    try {
      await api.project.remove(target.id)
      toast(t('projects.deleted'))
      refreshProjects()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const restoreProject = async (id: string) => {
    try {
      await api.project.restore(id)
      toast(t('projects.restored'))
      refreshProjects()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const toggleAdmin = async (a: AdminAccount) => {
    try {
      await api.admin.accounts.update(a.id, { platform_admin: !a.platform_admin })
      toast(a.platform_admin ? t('users.adminRevoked') : t('users.adminGranted'))
      reloadUsers()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const confirmAccountDelete = async () => {
    if (!pendingAccountDelete) return
    const target = pendingAccountDelete
    setPendingAccountDelete(null)
    try {
      await api.admin.accounts.remove(target.id)
      toast(t('users.deleted'))
      reloadUsers()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const toggleDisabled = async (a: AdminAccount) => {
    if (a.disabled) {
      try {
        await api.admin.accounts.update(a.id, { disabled: false })
        toast(t('users.enabledToast'))
        reloadUsers()
      } catch (e) {
        toast(errText(lang, e), 'err')
      }
      return
    }
    setPendingDisable(a)
  }

  const confirmDisable = async () => {
    if (!pendingDisable) return
    const target = pendingDisable
    setPendingDisable(null)
    try {
      await api.admin.accounts.update(target.id, { disabled: true })
      toast(t('users.disabledToast'))
      reloadUsers()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const tabItems = [
    { key: 'projects' as const, label: t('projects.tab.projects'), count: previewUser ? projectRows.length : projectPage?.total },
    ...(isAdmin ? [{ key: 'users' as const, label: t('projects.tab.users'), count: userPage?.total }] : []),
    { key: 'devices' as const, label: t('projects.tab.devices') },
  ]

  const headerActions =
    activeTab === 'projects' ? (
      <PrimaryButton icon="i-lucide:plus" onClick={() => setCreateOpen(true)}>
        {t('projects.create')}
      </PrimaryButton>
    ) : activeTab === 'users' ? (
      <SearchBox value={pg.draft} onChange={pg.setDraft} onSubmit={pg.submit} placeholder={t('users.searchPlaceholder')} />
    ) : undefined

  // 弹窗关闭时对象已置空，退场动画里会看到「确定删除“”？」；用最后的值兜住。
  const deleteShown = useLastValue(pendingDelete)
  const projectDisableShown = useLastValue(pendingProjectDisable)
  const accountDeleteShown = useLastValue(pendingAccountDelete)
  const accountDisableShown = useLastValue(pendingDisable)

  return (
    <>
      <PageHeader title={t('projects.title')} desc={t('projects.desc')} actions={headerActions} />

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <Tabs label={t('projects.title')} items={tabItems} value={activeTab} onChange={switchTab} />
        {activeTab === 'projects' && account?.accountId && <span className="text-xs text-ink-muted">{t('projects.anyoneCanCreate')}</span>}
      </div>

      {activeTab === 'devices' ? (
        <LocalDevices projectId={currentProjectId} projectName={currentProjectName} ready={ready} canManage={canManageDevices} />
      ) : activeTab === 'projects' ? (
        <div className="flex min-h-0 flex-1 flex-col gap-4">
          <Panel className="flex-1 min-h-[320px]">
            {projectState === 'ready' && projectRows.length === 0 ? (
              <EmptyState
                icon="i-lucide:folder-plus"
                title={t('projects.emptyTitle')}
                desc={isAdmin ? t('projects.emptyAdmin') : t('projects.emptyUser')}
                action={
                  <PrimaryButton icon="i-lucide:plus" onClick={() => setCreateOpen(true)}>
                    {t('projects.create')}
                  </PrimaryButton>
                }
              />
            ) : (
              <Table
                head={
                  <>
                    <Th>{t('projects.tab.projects')}</Th>
                    <Th>{t('projects.description')}</Th>
                    <Th>{t('projects.member.role')}</Th>
                    <Th>{t('users.col.status')}</Th>
                    <Th>{t('users.col.actions')}</Th>
                  </>
                }
              >
                {projectState !== 'ready' && <StateRow colSpan={5} state={projectState} error={projectError} />}
                {projectState === 'ready' &&
                  projectRows.map((p) => {
                    const st = projectStatus(p, currentId)
                    const canManage = isAdmin || p.role === 'owner'
                    const actions: RowAction[] = [{ label: t('projects.copyId'), icon: 'i-lucide:copy', onClick: () => copyId(p.id) }]
                    if (p.archived) {
                      if (canManage) {
                        actions.push({
                          label: t('projects.restore'),
                          icon: 'i-lucide:archive-restore',
                          onClick: () => void restoreProject(p.id),
                        })
                      }
                    } else {
                      if (canManage) {
                        actions.push({ label: t('projects.members'), icon: 'i-lucide:users', onClick: () => setMemberProject(p) })
                        actions.push({ label: t('projects.edit'), icon: 'i-lucide:pencil', onClick: () => setEditProject(p) })
                      }
                      // 个人项目是账号的默认落脚点，删掉后新会话无处可去；后端同样拒绝。
                      if (canManage && p.kind !== 'personal') {
                        actions.push({ label: t('projects.delete'), icon: 'i-lucide:archive', onClick: () => setPendingDelete(p) })
                      }
                      // 已停用的项目必须能恢复，否则「停用」是不可逆的。恢复后不自动切换。
                      if (canManage && p.disabled && p.kind !== 'personal') {
                        actions.push({ label: t('projects.enable'), icon: 'i-lucide:rotate-ccw', onClick: () => enableProject(p.id) })
                      }
                      if (canManage && !p.disabled && p.kind !== 'personal') {
                        actions.push({
                          label: t('projects.disable'),
                          icon: 'i-lucide:ban',
                          tone: 'danger',
                          onClick: () => setPendingProjectDisable(p),
                        })
                      }
                      // 没有「当前项目」概念的凭据（管理 Token）不给这个按钮：后端 switch_project
                      // 会直接报「当前凭据没有会话」，是个点了必失败的入口。
                      if (!p.disabled && currentId && p.id !== currentId) {
                        actions.push({ label: t('projects.setCurrent'), icon: 'i-lucide:check', onClick: () => switchTo(p.id) })
                      }
                    }
                    return (
                      <tr key={p.id} data-selectable="true" className="data-[selected=true]:bg-elevated/50">
                        <td data-pinned="false" className={TD}>
                          <div className="min-w-0">
                            <div className="flex items-center gap-1.5">
                              <span className="truncate text-sm font-medium text-ink">{p.name}</span>
                              {p.kind === 'personal' && p.id === `u-${account?.accountId ?? ''}` && (
                                <Badge variant="outline">{t('projects.personal')}</Badge>
                              )}
                            </div>
                            {/* 只有“显示名与项目 ID 不同”的团队项目才需要把 ID 露出来；
                                个人空间的 ID 是 u-<账号哈希>，对用户没有意义，不展示。 */}
                            {p.kind !== 'personal' && p.name !== p.id && (
                              <div className="mt-0.5 font-mono text-[11px] text-ink-muted">{p.id}</div>
                            )}
                          </div>
                        </td>
                        <td data-pinned="false" className={TD_BREAK}>
                          {p.description || <span className="text-ink-muted">—</span>}
                        </td>
                        <td data-pinned="false" className={TD}>
                          {/* 角色列只放真实成员角色。平台管理员的跨项目权限不是角色，
                              写进来会让人以为「平台管理员」是第四种角色、或以为自己是
                              所有者；没有成员关系就留白。 */}
                          {p.role ? (
                            <Badge variant={p.role === 'owner' ? 'default' : p.role === 'editor' ? 'warning' : 'secondary'}>
                              {roleLabel(str, p.role)}
                            </Badge>
                          ) : (
                            <span className="text-ink-muted">—</span>
                          )}
                        </td>
                        <td data-pinned="false" className={TD}>
                          <Badge variant={st.tone}>{str(st.label)}</Badge>
                        </td>
                        <td data-pinned="false" className={TD}>
                          <RowActions actions={actions} />
                        </td>
                      </tr>
                    )
                  })}
              </Table>
            )}
          </Panel>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-4">
          <Panel className="flex-1 min-h-[320px]">
            <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-ink">{t('users.title')}</div>
                <div className="mt-0.5 text-xs text-ink-muted">{t('users.desc')}</div>
              </div>
              <span className="shrink-0 text-xs text-ink-muted">{t('common.total')(userPage?.total ?? 0)}</span>
            </div>
            {userState === 'ready' && userRows.length === 0 ? (
              <EmptyState icon="i-lucide:users" title={t('users.empty')} desc={t('users.emptyDesc')} />
            ) : (
              <Table
                head={
                  <>
                    <Th>{t('users.col.user')}</Th>
                    <Th>{t('users.col.displayName')}</Th>
                    <Th>{t('users.col.platformAdmin')}</Th>
                    <Th>{t('users.col.status')}</Th>
                    <Th>{t('users.col.lastLogin')}</Th>
                    <Th>{t('users.col.actions')}</Th>
                  </>
                }
              >
                {userState !== 'ready' && <StateRow colSpan={6} state={userState} error={userError} />}
                {userState === 'ready' &&
                  userRows.map((a) => {
                    const self = a.username === account?.username
                    const actions: RowAction[] = [
                      {
                        label: t('users.assign'),
                        icon: 'i-lucide:user-plus',
                        onClick: () => {
                          reloadDict()
                          setAssignUser(a)
                        },
                      },
                    ]
                    if (!self) {
                      actions.push({
                        label: a.platform_admin ? t('users.unsetAdmin') : t('users.setAdmin'),
                        icon: a.platform_admin ? 'i-lucide:shield-off' : 'i-lucide:shield',
                        onClick: () => toggleAdmin(a),
                      })
                      actions.push({
                        label: a.disabled ? t('users.enable') : t('users.disable'),
                        icon: a.disabled ? 'i-lucide:rotate-ccw' : 'i-lucide:ban',
                        tone: a.disabled ? 'default' : 'danger',
                        onClick: () => toggleDisabled(a),
                      })
                      actions.push({
                        label: t('users.delete'),
                        icon: 'i-lucide:trash-2',
                        tone: 'danger',
                        onClick: () => setPendingAccountDelete(a),
                      })
                    }
                    return (
                      <tr key={a.id} data-selectable="true" className="data-[selected=true]:bg-elevated/50">
                        <td data-pinned="false" className={TD}>
                          <div className="flex items-center gap-2.5">
                            <span className="flex size-7 shrink-0 items-center justify-center rounded-full border border-line bg-surface-muted text-xs font-semibold text-ink-secondary">
                              {(a.display_name || a.username).charAt(0).toUpperCase()}
                            </span>
                            <div className="min-w-0">
                              <div className="flex items-center gap-1.5">
                                <span className="truncate text-sm font-medium text-ink">{a.username}</span>
                                {self && <Badge variant="outline">{t('users.self')}</Badge>}
                              </div>
                            </div>
                          </div>
                        </td>
                        <td data-pinned="false" className={TD}>
                          {a.display_name || <span className="text-ink-muted">—</span>}
                        </td>
                        <td data-pinned="false" className={TD}>
                          {a.platform_admin ? (
                            <Badge variant="default">{t('users.col.platformAdmin')}</Badge>
                          ) : (
                            <span className="text-ink-muted">—</span>
                          )}
                        </td>
                        <td data-pinned="false" className={TD}>
                          <Badge variant={a.disabled ? 'destructive' : 'success'}>
                            {a.disabled ? t('users.disabledStatus') : t('users.active')}
                          </Badge>
                        </td>
                        <td data-pinned="false" className={TD}>
                          {a.last_login_at ? (
                            new Date(a.last_login_at).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US')
                          ) : (
                            <span className="text-ink-muted">—</span>
                          )}
                        </td>
                        <td data-pinned="false" className={TD}>
                          <RowActions actions={actions} />
                          {self && <p className="mt-1 text-[11px] leading-4 text-ink-muted">{t('users.selfProtected')}</p>}
                        </td>
                      </tr>
                    )
                  })}
              </Table>
            )}
          </Panel>
          <Pager page={pg.page} pageSize={pg.pageSize} total={userPage?.total ?? 0} onPage={pg.setPage} />
        </div>
      )}

      {showCreate && (
        <CreateProjectDialog
          open
          onClose={closeCreate}
          onCreated={async (id) => {
            closeCreate()
            await setCurrent(id).catch(() => {})
            void reloadAccount()
            refreshProjects()
            toast(t('projects.createSuccess'))
          }}
        />
      )}

      {editProject && (
        <EditProjectDialog
          project={editProject}
          onClose={() => setEditProject(null)}
          onSaved={() => {
            setEditProject(null)
            refreshProjects()
            toast(t('projects.updateSuccess'))
          }}
        />
      )}

      {memberProject && (
        <MembersDialog
          project={memberProject}
          open
          onClose={() => setMemberProject(null)}
          canManage={isAdmin || memberProject.role === 'owner'}
          isAdmin={isAdmin}
          onChanged={refreshProjects}
        />
      )}

      {assignUser && (
        <AssignProjectDialog
          user={assignUser}
          onClose={() => setAssignUser(null)}
          onDone={() => {
            setAssignUser(null)
            setUserTick((x) => x + 1)
          }}
          onCreate={() => {
            setAssignUser(null)
            switchTab('projects')
            setCreateOpen(true)
          }}
        />
      )}

      <Dialog
        open={!!pendingDelete}
        title={t('projects.deleteTitle')}
        onClose={() => setPendingDelete(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={confirmDelete}>
              {t('projects.delete')}
            </Button>
          </>
        }
      >
        <div className="space-y-2">
          <p>{t('projects.deleteConfirm')(deleteShown?.name ?? '')}</p>
          <p className="text-sm text-ink-muted">{t('projects.deleteHint')}</p>
        </div>
      </Dialog>

      <Dialog
        open={!!pendingProjectDisable}
        title={t('projects.disableTitle')}
        onClose={() => setPendingProjectDisable(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingProjectDisable(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={confirmProjectDisable}>
              {t('projects.disable')}
            </Button>
          </>
        }
      >
        <p>{t('projects.disableConfirm')(projectDisableShown?.name ?? '')}</p>
      </Dialog>

      <Dialog
        open={!!pendingAccountDelete}
        title={t('users.deleteTitle')}
        onClose={() => setPendingAccountDelete(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingAccountDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={confirmAccountDelete}>
              {t('users.delete')}
            </Button>
          </>
        }
      >
        <div className="space-y-2">
          <p>{t('users.deleteConfirm')(accountDeleteShown?.username ?? '')}</p>
          <p className="text-sm text-ink-muted">{t('users.deleteHint')}</p>
        </div>
      </Dialog>

      <Dialog
        open={!!pendingDisable}
        title={t('users.disable')}
        onClose={() => setPendingDisable(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingDisable(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={confirmDisable}>
              {t('users.disable')}
            </Button>
          </>
        }
      >
        {t('users.confirmDisable')(accountDisableShown?.username ?? '')}
      </Dialog>
    </>
  )
}

function CreateProjectDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const t = useT()
  const { lang } = useLang()
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const value = name.trim()
    if (!value) return setError(t('projects.nameRequired'))
    if (!NAME_RE.test(value)) return setError(t('projects.nameInvalid'))
    setBusy(true)
    setError('')
    try {
      const p = await api.project.create({ name: value, description: desc.trim(), isMultiView: 0 })
      onCreated(p.id)
    } catch (err) {
      setError(errText(lang, err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={open}
      title={t('projects.createTitle')}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="primary" type="submit" form="create-project-form" loading={busy}>
            {t('common.create')}
          </Button>
        </>
      }
    >
      <form id="create-project-form" className="space-y-4" onSubmit={submit}>
        <div className="space-y-1.5">
          <label className="block text-sm font-medium text-ink-secondary">{t('projects.name')}</label>
          <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder={t('projects.namePlaceholder')} />
          <p className="text-xs leading-5 text-ink-muted">{t('projects.nameHint')}</p>
        </div>
        <div className="space-y-1.5">
          <label className="block text-sm font-medium text-ink-secondary">
            {t('projects.description')} <span className="font-normal text-ink-muted">({t('common.optional')})</span>
          </label>
          <textarea
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder={t('projects.descriptionPlaceholder')}
            rows={3}
            className="w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink shadow-xs outline-none transition-colors placeholder:text-ink-muted hover:border-line-strong focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20"
          />
        </div>
        {error && <p className="text-xs font-medium text-danger">{error}</p>}
      </form>
    </Dialog>
  )
}

function EditProjectDialog({ project, onClose, onSaved }: { project: Project | null; onClose: () => void; onSaved: () => void }) {
  const t = useT()
  const { lang } = useLang()
  const [name, setName] = useState(project?.name ?? '')
  const [desc, setDesc] = useState(project?.description ?? '')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!project) return
    const value = name.trim()
    if (!value) return setError(t('projects.nameRequired'))
    if (!NAME_RE.test(value)) return setError(t('projects.nameInvalid'))
    setBusy(true)
    setError('')
    try {
      await api.project.update({ id: project.id, name: value, description: desc.trim() })
      onSaved()
    } catch (err) {
      setError(errText(lang, err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={!!project}
      title={t('projects.editTitle')}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="primary" type="submit" form="edit-project-form" loading={busy}>
            {t('common.save')}
          </Button>
        </>
      }
    >
      <form id="edit-project-form" className="space-y-4" onSubmit={submit}>
        <div className="space-y-1.5">
          <label className="block text-sm font-medium text-ink-secondary">{t('projects.displayName')}</label>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
          <p className="text-xs leading-5 text-ink-muted">{t('projects.displayNameHint')}</p>
          <p className="font-mono text-[11px] text-ink-muted">
            {t('projects.id')}: {project?.id}
          </p>
        </div>
        <div className="space-y-1.5">
          <label className="block text-sm font-medium text-ink-secondary">
            {t('projects.description')} <span className="font-normal text-ink-muted">({t('common.optional')})</span>
          </label>
          <textarea
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            rows={3}
            className="w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink shadow-xs outline-none transition-colors placeholder:text-ink-muted hover:border-line-strong focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20"
          />
        </div>
        {error && <p className="text-xs font-medium text-danger">{error}</p>}
      </form>
    </Dialog>
  )
}

function MembersDialog({
  project,
  open,
  onClose,
  canManage,
  isAdmin,
  onChanged,
}: {
  project: Project | null
  open: boolean
  onClose: () => void
  canManage: boolean
  isAdmin: boolean
  onChanged: () => void
}) {
  const t = useT()
  const str = useStr()
  const { lang } = useLang()
  const toast = useToast()
  const { account, reloadAccount } = useMeta()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<MemberCandidate[]>([])
  const [selected, setSelected] = useState<MemberCandidate | null>(null)
  const [searched, setSearched] = useState(false)
  const [role, setRole] = useState<Role>('viewer')
  const [busy, setBusy] = useState(false)
  const [pendingRemove, setPendingRemove] = useState<string | null>(null)
  const [inviteOpen, setInviteOpen] = useState(false)

  const {
    data: memberPage,
    state,
    error,
    reload: reloadMembers,
  } = useAsync(() => api.admin.members.list(project?.id ?? ''), [project?.id], {
    skip: !project,
    key: 'projects.members',
  })
  const members = memberPage?.records ?? []
  const ownerCount = members.filter((m) => m.role === 'owner').length
  // 服务端会把“移出/降级最后一个 owner”拒成 409；前端提前锁住入口，
  // 用户不用先点一次失败才知道原因。
  const isLastOwner = (m: ProjectMember) => m.role === 'owner' && ownerCount <= 1
  const isSelf = (m: ProjectMember) => !!account?.username && m.username === account.username

  const search = async () => {
    const q = query.trim()
    if (!q) return
    setBusy(true)
    try {
      const r = await api.admin.members.candidates(project?.id ?? '', q)
      setResults(r.records)
      setSearched(true)
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setBusy(false)
    }
  }

  const add = async () => {
    if (!project) return
    if (!selected) {
      toast(t('projects.member.select'), 'err')
      return
    }
    setBusy(true)
    try {
      await api.admin.members.set(project.id, { account_id: selected.accountId, role })
      toast(t('projects.member.added'))
      setSelected(null)
      setQuery('')
      setResults([])
      setSearched(false)
      reloadMembers()
      onChanged()
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setBusy(false)
    }
  }

  const changeRole = async (m: ProjectMember, next: string) => {
    if (!project) return
    try {
      await api.admin.members.set(project.id, { account_id: m.account_id, role: next })
      toast(t('projects.member.roleChanged'))
      onChanged()
      // 自己把 owner 降级后如果还开着弹窗，下一次成员请求就会 403；
      // 平台管理员不受成员关系影响，可以留在弹窗里继续操作。
      if (isSelf(m) && next !== 'owner' && !isAdmin) {
        void reloadAccount()
        onClose()
        return
      }
      reloadMembers()
    } catch (e) {
      toast(errText(lang, e), 'err')
      reloadMembers()
    }
  }

  const remove = async (m: ProjectMember) => {
    if (!project) return
    setPendingRemove(null)
    try {
      if (isSelf(m)) {
        // 自己退出走自助接口：owner 之外的成员没有管理权限，不能调用管理端的移除接口。
        await api.admin.members.leave(project.id)
        toast(t('projects.member.left'))
      } else {
        await api.admin.members.remove(project.id, m.account_id)
        toast(t('projects.member.removed'))
      }
      onChanged()
      if (isSelf(m)) {
        // 自己已经不是成员了，再请求成员列表只会 403；关掉弹窗并刷新主体，
        // 让当前项目回退到仍有权限的项目（没有项目时由 AppShell 显示引导）。
        void reloadAccount()
        onClose()
        return
      }
      reloadMembers()
    } catch (e) {
      toast(errText(lang, e), 'err')
      reloadMembers()
    }
  }

  return (
    <Dialog open={open} size="lg" title={`${t('projects.memberTitle')}${project ? ` · ${project.name}` : ''}`} onClose={onClose}>
      <p className="mb-4 text-xs leading-5 text-ink-muted">{t('projects.memberDesc')}</p>

      {canManage && (
        <div className="mb-5 space-y-2.5 rounded-xl border border-line bg-surface-muted/50 p-3">
          <div className="flex gap-2">
            <Input
              value={query}
              onChange={(e) => {
                setQuery(e.target.value)
                setSelected(null)
                setResults([])
                setSearched(false)
              }}
              aria-label={t('projects.member.search')}
              placeholder={t('projects.member.searchPlaceholder')}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  void search()
                }
              }}
            />
            <Button variant="secondary" className="shrink-0 whitespace-nowrap" onClick={search} loading={busy} disabled={!query.trim()}>
              {t('common.search')}
            </Button>
          </div>
          {results.length > 0 && (
            <div className="max-h-36 overflow-y-auto rounded-lg border border-line bg-surface">
              {results.map((a) => (
                <button
                  key={a.accountId}
                  type="button"
                  disabled={a.member || a.disabled}
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-50"
                  onClick={() => {
                    setSelected(a)
                    setQuery(`${a.displayName || a.username} (${a.username})`)
                    setResults([])
                  }}
                >
                  <span className="min-w-0 truncate text-ink">{a.displayName || a.username}</span>
                  <span className="flex shrink-0 items-center gap-2">
                    {a.member && <Badge variant="outline">{t('projects.member.alreadyMember')}</Badge>}
                    {a.disabled && <Badge variant="destructive">{t('users.disabledStatus')}</Badge>}
                    <span className="font-mono text-[11px] text-ink-muted">{a.username}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
          {searched && results.length === 0 && <p className="text-xs leading-5 text-ink-muted">{t('projects.member.notFound')}</p>}
          {selected && (
            <div className="flex items-center justify-between rounded-lg border border-accent/30 bg-accent-soft px-3 py-2 text-xs">
              <span className="truncate text-ink">
                {selected.displayName || selected.username} <span className="text-ink-muted">({selected.username})</span>
              </span>
              <button type="button" className="text-ink-muted hover:text-ink" onClick={() => setSelected(null)}>
                <Icon name="i-lucide:x" className="size-3.5" />
              </button>
            </div>
          )}
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-36 flex-1">
              <label className="mb-1.5 block text-xs font-medium text-ink-secondary">{t('projects.member.role')}</label>
              <FilterSelect
                value={role}
                onChange={(v) => v && setRole(v as Role)}
                options={roleOptions(str)}
                placeholder={t('projects.member.role')}
              />
            </div>
            <Button variant="primary" onClick={add} loading={busy}>
              <Icon name="i-lucide:user-plus" className="size-4" />
              {t('projects.member.add')}
            </Button>
          </div>
          <div className="flex items-center justify-between gap-2 pt-1">
            <p className="min-w-0 text-[11px] leading-4 text-ink-muted">{t('projects.member.hint')}</p>
            <Button variant="secondary" size="sm" className="shrink-0" onClick={() => setInviteOpen(true)}>
              <Icon name="i-lucide:link" className="size-3.5" />
              {t('projects.invite.open')}
            </Button>
          </div>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-line">
        <Table
          head={
            <>
              <Th>{t('users.col.user')}</Th>
              <Th>{t('projects.member.role')}</Th>
              <Th>{t('users.col.actions')}</Th>
            </>
          }
        >
          {state !== 'ready' && <StateRow colSpan={3} state={state} error={error} />}
          {state === 'ready' && members.length === 0 && (
            <tr>
              <td colSpan={3}>
                <EmptyState compact icon="i-lucide:users" title={t('projects.member.empty')} />
              </td>
            </tr>
          )}
          {state === 'ready' &&
            members.map((m) => (
              <tr key={m.account_id}>
                <td className={TD}>
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-ink">{m.display_name || m.username}</div>
                    <div className="truncate font-mono text-[11px] text-ink-muted">{m.username}</div>
                  </div>
                </td>
                <td className={TD}>
                  {canManage && !isLastOwner(m) && !isSelf(m) ? (
                    <FilterSelect
                      value={m.role}
                      onChange={(v) => v && void changeRole(m, v)}
                      options={roleOptions(str)}
                      placeholder={t('projects.member.role')}
                    />
                  ) : (
                    <Badge
                      variant={m.role === 'owner' ? 'default' : m.role === 'editor' ? 'warning' : 'secondary'}
                      title={
                        isSelf(m) ? t('projects.member.selfRoleHint') : isLastOwner(m) ? t('projects.member.lastOwnerHint') : undefined
                      }
                      className={isSelf(m) || isLastOwner(m) ? 'cursor-not-allowed' : undefined}
                    >
                      {roleLabel(str, m.role)}
                    </Badge>
                  )}
                </td>
                <td className={TD}>
                  {(canManage || isSelf(m)) &&
                    (pendingRemove === m.account_id ? (
                      <div className="flex items-center gap-1.5">
                        <Button
                          size="sm"
                          variant="destructive"
                          title={
                            isSelf(m) ? t('projects.member.confirmLeave') : t('projects.member.confirmRemove')(m.display_name || m.username)
                          }
                          onClick={() => void remove(m)}
                        >
                          {isSelf(m) ? t('projects.member.leave') : t('projects.member.remove')}
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setPendingRemove(null)}>
                          {t('common.cancel')}
                        </Button>
                      </div>
                    ) : (
                      <RowActions
                        actions={[
                          {
                            label: isSelf(m)
                              ? t('projects.member.leave')
                              : isLastOwner(m)
                                ? t('projects.member.lastOwnerLocked')
                                : t('projects.member.remove'),
                            icon: isSelf(m) ? 'i-lucide:log-out' : 'i-lucide:trash-2',
                            tone: 'danger',
                            disabled: isLastOwner(m),
                            onClick: () => setPendingRemove(m.account_id),
                          },
                        ]}
                      />
                    ))}
                </td>
              </tr>
            ))}
        </Table>
      </div>

      {ownerCount <= 1 && state === 'ready' && (
        <p className="mt-2 text-[11px] leading-4 text-ink-muted">{t('projects.member.lastOwnerHint')}</p>
      )}

      <div className="mt-4 flex justify-end">
        <Button variant="secondary" onClick={onClose}>
          {t('common.close')}
        </Button>
      </div>

      {inviteOpen && project && <InviteDialog project={project} isAdmin={isAdmin} open onClose={() => setInviteOpen(false)} />}
    </Dialog>
  )
}

const INVITE_STATUS_KEY: Record<ProjectInvite['status'], TKey> = {
  active: 'projects.invite.status.active',
  used: 'projects.invite.status.used',
  expired: 'projects.invite.status.expired',
  revoked: 'projects.invite.status.revoked',
}

const INVITE_STATUS_TONE: Record<ProjectInvite['status'], 'success' | 'secondary' | 'destructive'> = {
  active: 'success',
  used: 'secondary',
  expired: 'secondary',
  revoked: 'destructive',
}

function InviteDialog({ project, isAdmin, open, onClose }: { project: Project; isAdmin: boolean; open: boolean; onClose: () => void }) {
  const t = useT()
  const str = useStr()
  const { lang } = useLang()
  const toast = useToast()
  const [role, setRole] = useState<Role>('viewer')
  const [days, setDays] = useState('7')
  const [busy, setBusy] = useState(false)
  const [link, setLink] = useState('')
  const [tick, setTick] = useState(0)
  /** 撤销后链接立即失效，先确认 */
  const [pendingRevoke, setPendingRevoke] = useState<string | null>(null)

  const { data, state, error, reload } = useAsync(() => api.admin.invites.list(project.id), [project.id, tick], {
    key: 'projects.invites',
  })
  const invites = data?.records ?? []
  const roleChoices = (isAdmin ? ROLES : (['viewer', 'editor'] as const)).map((r) => ({
    value: r,
    label: roleLabel(str, r),
  }))

  const create = async () => {
    setBusy(true)
    try {
      const created = await api.admin.invites.create(project.id, { role, expiresIn: `${Number(days) * 24}h` })
      setLink(`${window.location.origin}${created.path}`)
      toast(t('projects.invite.created'))
      reload()
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (id: string) => {
    setPendingRevoke(null)
    try {
      await api.admin.invites.revoke(project.id, id)
      toast(t('projects.invite.revoked'))
      setTick((x) => x + 1)
    } catch (e) {
      toast(errText(lang, e), 'err')
      reload()
    }
  }

  const changeInviteRole = async (inviteId: string, next: string) => {
    try {
      await api.admin.invites.setRole(project.id, inviteId, next)
      toast(t('projects.invite.roleChanged'))
      setTick((x) => x + 1)
    } catch (e) {
      toast(errText(lang, e), 'err')
      reload()
    }
  }

  const copyLink = async () => {
    try {
      await copyText(link)
      toast(t('common.copied'))
    } catch {
      toast(t('common.copyFailed'), 'err')
    }
  }

  return (
    <Dialog open={open} size="lg" title={`${t('projects.invite.title')} · ${project.name}`} onClose={onClose}>
      <p className="mb-4 text-xs leading-5 text-ink-muted">{t('projects.invite.desc')}</p>

      <div className="mb-5 space-y-2.5 rounded-xl border border-line bg-surface-muted/50 p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-32 flex-1">
            <label className="mb-1.5 block text-xs font-medium text-ink-secondary">{t('projects.invite.role')}</label>
            <FilterSelect
              value={role}
              onChange={(v) => v && setRole(v as Role)}
              options={roleChoices}
              placeholder={t('projects.invite.role')}
            />
          </div>
          <div className="min-w-32 flex-1">
            <label className="mb-1.5 block text-xs font-medium text-ink-secondary">{t('projects.invite.expiry')}</label>
            <FilterSelect
              value={days}
              onChange={(v) => v && setDays(v)}
              options={['1', '7', '14', '30'].map((d) => ({ value: d, label: t('projects.invite.days')(d) }))}
              placeholder={t('projects.invite.expiry')}
            />
          </div>
          <Button variant="primary" onClick={create} loading={busy}>
            <Icon name="i-lucide:link" className="size-4" />
            {t('projects.invite.create')}
          </Button>
        </div>
        {link && (
          <div className="flex items-center gap-2 rounded-lg border border-accent/30 bg-accent-soft px-3 py-2">
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink">{link}</span>
            <Button size="sm" variant="secondary" onClick={copyLink}>
              {t('projects.invite.copy')}
            </Button>
          </div>
        )}
        <p className="text-[11px] leading-4 text-ink-muted">{t('projects.invite.onceHint')}</p>
      </div>

      <div className="overflow-hidden rounded-xl border border-line">
        <Table
          head={
            <>
              <Th>{t('projects.invite.role')}</Th>
              <Th>{t('users.col.status')}</Th>
              <Th>{t('projects.invite.expiresAt')}</Th>
              <Th>{t('users.col.actions')}</Th>
            </>
          }
        >
          {state !== 'ready' && <StateRow colSpan={4} state={state} error={error} />}
          {state === 'ready' && invites.length === 0 && (
            <tr>
              <td colSpan={4}>
                <EmptyState compact icon="i-lucide:link" title={t('projects.invite.empty')} />
              </td>
            </tr>
          )}
          {state === 'ready' &&
            invites.map((inv) => (
              <tr key={inv.id}>
                <td className={TD}>
                  {inv.status === 'active' ? (
                    <FilterSelect
                      value={inv.role}
                      onChange={(v) => v && v !== inv.role && void changeInviteRole(inv.id, v)}
                      options={roleChoices}
                      placeholder={t('projects.invite.role')}
                    />
                  ) : (
                    roleLabel(str, inv.role)
                  )}
                </td>
                <td className={TD}>
                  <Badge variant={INVITE_STATUS_TONE[inv.status]}>{str(INVITE_STATUS_KEY[inv.status])}</Badge>
                </td>
                <td className={TD}>{new Date(inv.expiresAt).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US')}</td>
                <td className={TD}>
                  {inv.status === 'active' && (
                    <RowActions
                      actions={[
                        {
                          label: t('projects.invite.revoke'),
                          icon: 'i-lucide:ban',
                          tone: 'danger',
                          onClick: () => setPendingRevoke(inv.id),
                        },
                      ]}
                    />
                  )}
                </td>
              </tr>
            ))}
        </Table>
      </div>

      <div className="mt-4 flex justify-end">
        <Button variant="secondary" onClick={onClose}>
          {t('common.close')}
        </Button>
      </div>

      <Dialog
        open={!!pendingRevoke}
        size="sm"
        title={t('projects.invite.revokeTitle')}
        onClose={() => setPendingRevoke(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingRevoke(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => pendingRevoke && revoke(pendingRevoke)}>
              {t('projects.invite.revoke')}
            </Button>
          </>
        }
      >
        {t('projects.invite.revokeConfirm')}
      </Dialog>
    </Dialog>
  )
}

function AssignProjectDialog({
  user,
  onClose,
  onDone,
  onCreate,
}: {
  user: AdminAccount | null
  onClose: () => void
  onDone: () => void
  onCreate: () => void
}) {
  const t = useT()
  const str = useStr()
  const { lang } = useLang()
  const toast = useToast()
  const { projects, currentProjectId } = useProject()
  const available = projects.filter((p) => !p.disabled)
  const [projectId, setProjectId] = useState(() => {
    const current = available.find((p) => p.id === currentProjectId)
    return current?.id ?? available[0]?.id ?? ''
  })
  const [role, setRole] = useState<Role>('viewer')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!user || !projectId) return
    setBusy(true)
    try {
      await api.admin.members.set(projectId, { account_id: user.id, role })
      const project = available.find((p) => p.id === projectId)
      toast(t('users.assigned')(user.username, project?.name ?? projectId))
      onDone()
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={!!user}
      title={t('users.assignTitle')}
      onClose={onClose}
      footer={
        available.length > 0 ? (
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button variant="primary" onClick={submit} loading={busy} disabled={!projectId}>
              {t('users.assign')}
            </Button>
          </>
        ) : (
          <Button variant="secondary" onClick={onClose}>
            {t('common.close')}
          </Button>
        )
      }
    >
      <p className="mb-4 text-sm text-ink-secondary">{t('users.assignDesc')(user?.username ?? '')}</p>
      {available.length === 0 ? (
        <EmptyState
          compact
          icon="i-lucide:folder-plus"
          title={t('users.noProject')}
          action={
            <Button variant="primary" onClick={onCreate}>
              {t('projects.create')}
            </Button>
          }
        />
      ) : (
        <div className="space-y-4">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-ink-secondary">{t('users.assignProject')}</label>
            <FilterSelect
              value={projectId}
              onChange={(v) => v && setProjectId(v)}
              options={available.map((p) => ({ value: p.id, label: p.name }))}
              placeholder={t('users.assignProject')}
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-ink-secondary">{t('users.assignRole')}</label>
            <FilterSelect
              value={role}
              onChange={(v) => v && setRole(v as Role)}
              options={roleOptions(str)}
              placeholder={t('users.assignRole')}
            />
            <p className="mt-1.5 text-[11px] leading-4 text-ink-muted">{str(ROLE_HINT[role])}</p>
          </div>
        </div>
      )}
    </Dialog>
  )
}
