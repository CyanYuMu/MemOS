/** 提示词工作台。左侧按分组列提示词，右侧编辑当前语言，可以先试跑再保存。
 *
 *  数据流：`prompts/list` 给左侧与「是否自定义」，`prompts/detail` 给当前提示词的全部语言。
 *  编辑区只维护一个 `draft`；草稿、校验结果、试跑结果都带着「槽位签名」，签名一变就自动
 *  回到服务端的内容，不需要用一个 effect 去同步状态。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError, api, conflictCurrent, retryAfterSec, type PromptValidation, type PromptVersion } from '../lib/api'
import { errText } from '../lib/i18n'
import type { TKey } from '../lib/i18n'
import {
  customizedSlots,
  defaultCompareLayout,
  folderOf,
  folderOpen,
  promptFolders,
  needsSaveConfirm,
  pickLang,
  versionLabel,
  type CompareLayout,
} from '../lib/prompts'
import { formatBeijingTime } from '../lib/datetime'
import { useAsync } from '../lib/useAsync'
import { useLang, useProject, useProjectRole, useStr, useT, useToast } from '../lib/store'
import { PageHeader, Dialog } from '../components/ui'
import { Button } from '../components/ui/button'
import { Badge } from '../components/ui/badge'
import { Tabs } from '../components/ui/tabs'
import { EmptyState } from '../components/ui/empty-state'
import { Skeleton } from '../components/ui/skeleton'
import { Icon } from '../components/Icon'
import { PromptCodeEditor } from '../components/prompts/PromptCodeEditor'
import { TestPanel } from '../components/prompts/TestPanel'
import { PanelBoundary } from '../components/prompts/PanelBoundary'
import { SaveDialog } from '../components/prompts/SaveDialog'
import { ResetDialog, type ResetScope } from '../components/prompts/ResetDialog'
import { AiDialog } from '../components/prompts/AiDialog'
import { VersionDrawer } from '../components/prompts/VersionDrawer'
import { DiffDialog, type DiffSource } from '../components/prompts/DiffDialog'

const FOLDER_LABEL: Record<string, TKey> = {
  extract: 'prompts.group.extract',
  process: 'prompts.group.process',
  dream: 'prompts.group.dream',
}

const LANG_LABEL: Record<string, TKey> = {
  zh: 'prompts.lang.zh',
  en: 'prompts.lang.en',
}

const MAX_NOTE = 200

interface Conflict {
  currentVersionId: number | null
  currentVersionNo?: number | null
  latestBody?: string
}

export function Extractions() {
  const t = useT()
  const str = useStr()
  const { lang } = useLang()
  const toast = useToast()
  const navigate = useNavigate()
  const { currentProjectId, ready } = useProject()
  const { canEdit, isViewer } = useProjectRole()

  const [query, setQuery] = useState('')
  const [toggled, setToggled] = useState<Record<string, boolean>>({})
  const [selectedKey, setSelectedKey] = useState('')
  const [langId, setLangId] = useState('')

  /* ------------------------------------------------------------ 取数 */

  const listQuery = useAsync(() => api.prompts.list(currentProjectId), [currentProjectId], {
    skip: !ready || !currentProjectId,
    key: 'prompts-list',
  })
  const items = useMemo(() => listQuery.data?.prompts ?? [], [listQuery.data])

  // 选中的提示词：记住的那一条还在就用它，否则退回对话抽取（列表里总有）或第一条
  const activeKey = useMemo(() => {
    if (selectedKey && items.some((p) => p.key === selectedKey)) return selectedKey
    return items.find((p) => p.key === 'extract.chat')?.key ?? items[0]?.key ?? ''
  }, [items, selectedKey])

  const detailQuery = useAsync(() => api.prompts.detail(currentProjectId, activeKey), [currentProjectId, activeKey], {
    skip: !currentProjectId || !activeKey,
    key: 'prompts-detail',
  })
  const detail = detailQuery.data

  const activeLang = detail ? pickLang(detail.languages, langId) : ''
  const effective = detail && activeLang ? detail.effective[activeLang] : null
  const defaultBody = detail && activeLang ? (detail.defaults[activeLang] ?? '') : ''
  const baseline = effective?.body ?? ''

  // 数据面还没下发 versionNo 时，用版本列表的序号兜底（列表新到旧，序号 = 总数 - 下标）。
  // 只在真的是项目覆盖、且接口没给号时才取，避免给每个默认槽位多发一次请求。
  const slotVersionsQuery = useAsync(
    () => api.prompts.versions({ projectId: currentProjectId, key: activeKey, lang: activeLang, limit: 100 }),
    [currentProjectId, activeKey, activeLang, effective?.versionId ?? 0],
    {
      skip: !detail || !activeLang || !effective || effective.source !== 'project' || (effective.versionNo ?? 0) > 0,
      key: 'prompts-slot-versions',
    },
  )
  const effectiveNo = useMemo(() => {
    if (!effective) return 0
    if ((effective.versionNo ?? 0) > 0) return effective.versionNo ?? 0
    const rows = slotVersionsQuery.data?.versions ?? []
    const i = rows.findIndex((v) => v.versionId === effective.versionId)
    if (i < 0) return 0
    return rows[i].versionNo ?? rows.length - i
  }, [effective, slotVersionsQuery.data])

  /* ------------------------------------------- 草稿：按槽位签名自动归零 */

  // 槽位签名 = 项目里的哪一条 × 哪种语言 × 当前版本。签名变了（切提示词、切语言、保存后
  // 版本号变化），草稿就回到服务端内容；用户打字只改 draft，不动签名。
  const slotSig = detail && activeLang ? `${activeKey}:${activeLang}:${effective?.versionId ?? 0}:${effective?.updatedAt ?? ''}` : ''
  const [edit, setEdit] = useState<{ sig: string; draft: string }>({ sig: '', draft: '' })
  const draft = detail && edit.sig === slotSig ? edit.draft : baseline
  const setDraft = useCallback((v: string) => setEdit({ sig: slotSig, draft: v }), [slotSig])
  const dirty = Boolean(detail) && draft !== baseline

  const draftSig = `${slotSig}|${draft}`

  /* ------------------------------------------------------------ 校验 */

  const [validationState, setValidationState] = useState<{ sig: string; value: PromptValidation | null; error: string }>({
    sig: '',
    value: null,
    error: '',
  })
  const validation = validationState.sig === draftSig ? validationState.value : null
  const validateError = validationState.sig === draftSig ? validationState.error : ''
  const validating = Boolean(canEdit && detail && activeLang) && validationState.sig !== draftSig

  useEffect(() => {
    if (!canEdit || !detail || !activeLang) return
    const sig = draftSig
    const body = draft
    const key = detail.key
    const timer = setTimeout(() => {
      api.prompts
        .validate({ projectId: currentProjectId, key, lang: activeLang, body })
        .then((v) => setValidationState({ sig, value: v, error: '' }))
        .catch((e) => setValidationState({ sig, value: null, error: errText(lang, e) }))
    }, 350)
    return () => clearTimeout(timer)
  }, [draftSig, draft, detail, activeLang, canEdit, currentProjectId, lang])

  /* -------------------------------------------------------- 试跑结果 */

  const [runInfo, setRunInfo] = useState<{ sig: string; ok: boolean } | null>(null)
  const run = runInfo && runInfo.sig === draftSig ? { draft, ok: runInfo.ok } : null
  const confirmRequired = needsSaveConfirm(run, draft)

  const [testSig, setTestSig] = useState('')
  const testOpen = Boolean(testSig && testSig === slotSig)
  // 对比结果排布：1440 及以上默认并排，更窄默认上下；选择留在本页，切换提示词不丢
  const [compareLayout, setCompareLayout] = useState<CompareLayout>(() =>
    defaultCompareLayout(typeof window === 'undefined' ? 0 : window.innerWidth),
  )

  /* ------------------------------------------------------------ 对话框 */

  const [saveOpen, setSaveOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [conflict, setConflict] = useState<Conflict | null>(null)

  const [resetOpen, setResetOpen] = useState(false)
  const [resetScope, setResetScope] = useState<ResetScope>('lang')
  const [resetting, setResetting] = useState(false)
  const [resetError, setResetError] = useState('')

  const [aiOpen, setAiOpen] = useState(false)
  const [aiBusy, setAiBusy] = useState(false)
  const [aiError, setAiError] = useState('')
  const [aiResult, setAiResult] = useState<{ body: string; validation: PromptValidation } | null>(null)
  const [aiSeconds, setAiSeconds] = useState(0)

  const [versionsOpen, setVersionsOpen] = useState(false)
  const [versions, setVersions] = useState<PromptVersion[]>([])
  const [vHasMore, setVHasMore] = useState(false)
  const [vLoading, setVLoading] = useState(false)
  const [vError, setVError] = useState('')
  const [rollingBackId, setRollingBackId] = useState<number | null>(null)

  const [diff, setDiff] = useState<{ title: string; sources: DiffSource[]; left: string; right: string } | null>(null)

  const [leaveTo, setLeaveTo] = useState<string | null>(null)
  const [pendingSwitch, setPendingSwitch] = useState<{ kind: 'key' | 'lang'; value: string } | null>(null)

  useEffect(() => {
    if (aiSeconds <= 0) return
    const id = setTimeout(() => setAiSeconds((n) => Math.max(0, n - 1)), 1000)
    return () => clearTimeout(id)
  }, [aiSeconds])

  // 有未保存修改时：关标签页/刷新要拦，站内跳转（侧栏链接）也要拦
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    const onClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      const anchor = target?.closest?.('a[href]') as HTMLAnchorElement | null
      const href = anchor?.getAttribute('href') ?? ''
      if (!href.startsWith('/') || href === window.location.pathname) return
      e.preventDefault()
      e.stopPropagation()
      setLeaveTo(href)
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    document.addEventListener('click', onClick, true)
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload)
      document.removeEventListener('click', onClick, true)
    }
  }, [dirty])

  /* -------------------------------------------------------------- 操作 */

  const slots = useMemo(() => customizedSlots(items), [items])

  // 数据面还没给 versionNo 时，用「该槽位加载到的第几个版本」兜底（列表新到旧）
  const versionRows = useMemo(() => versions.map((v, i) => ({ ...v, versionNo: v.versionNo ?? versions.length - i })), [versions])

  const requestKey = (key: string) => {
    if (key === activeKey) return
    if (dirty) setPendingSwitch({ kind: 'key', value: key })
    else setSelectedKey(key)
  }

  const requestLang = (next: string) => {
    if (next === activeLang) return
    setVersionsOpen(false)
    if (dirty) setPendingSwitch({ kind: 'lang', value: next })
    else setLangId(next)
  }

  const reloadBoth = () => {
    listQuery.reload()
    detailQuery.reload()
  }

  const openSave = () => {
    setSaveError('')
    setConflict(null)
    setSaveOpen(true)
  }

  const submitSave = async (note: string) => {
    if (!detail || !effective) return
    setSaving(true)
    setSaveError('')
    try {
      await api.prompts.save({
        projectId: currentProjectId,
        key: detail.key,
        lang: activeLang,
        body: draft,
        note,
        baseVersionId: effective.versionId,
      })
      toast(t('prompts.save.done'))
      setSaveOpen(false)
      reloadBoth()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const current = conflictCurrent(e)
        setConflict({ currentVersionId: current.versionId, currentVersionNo: current.versionNo })
        try {
          const latest = await api.prompts.detail(currentProjectId, detail.key)
          const eff = latest.effective[activeLang]
          setConflict({
            currentVersionId: current.versionId,
            currentVersionNo: current.versionNo ?? eff?.versionNo ?? null,
            latestBody: eff?.body ?? '',
          })
        } catch {
          // 拿不到最新内容也能刷新后重试，这里不额外报错
        }
      } else {
        setSaveError(errText(lang, e))
      }
    } finally {
      setSaving(false)
    }
  }

  const submitReset = async () => {
    if (!detail) return
    setResetting(true)
    setResetError('')
    try {
      const res = await api.prompts.reset({
        projectId: currentProjectId,
        ...(resetScope === 'project' ? {} : { key: detail.key }),
        ...(resetScope === 'lang' ? { lang: activeLang } : {}),
      })
      const n = res.reset.length
      toast(n ? t('prompts.reset.done')(n) : t('prompts.reset.none'))
      setResetOpen(false)
      reloadBoth()
    } catch (e) {
      setResetError(errText(lang, e))
    } finally {
      setResetting(false)
    }
  }

  const loadVersions = useCallback(
    async (reset: boolean) => {
      if (!detail) return
      setVLoading(true)
      setVError('')
      try {
        const beforeVersionId = reset ? undefined : versions[versions.length - 1]?.versionId
        const res = await api.prompts.versions({
          projectId: currentProjectId,
          key: detail.key,
          lang: activeLang,
          limit: 20,
          beforeVersionId,
        })
        setVersions((prev) => (reset ? res.versions : [...prev, ...res.versions]))
        setVHasMore(res.hasMore)
      } catch (e) {
        setVError(errText(lang, e))
      } finally {
        setVLoading(false)
      }
    },
    [detail, currentProjectId, activeLang, versions, lang],
  )

  const openVersions = () => {
    setVersions([])
    setVersionsOpen(true)
    void loadVersions(true)
  }

  const rollback = async (v: PromptVersion) => {
    if (!detail || !effective) return
    setRollingBackId(v.versionId)
    try {
      await api.prompts.save({
        projectId: currentProjectId,
        key: detail.key,
        lang: activeLang,
        restoreFromVersionId: v.versionId,
        baseVersionId: effective.versionId,
      })
      toast(t('prompts.versions.done'))
      reloadBoth()
      await loadVersions(true)
    } catch (e) {
      toast(errText(lang, e), 'err')
      if (e instanceof ApiError && e.status === 409) reloadBoth()
      // 要回滚到的版本已经不存在：刷新版本列表，让用户看到最新历史
      if (e instanceof ApiError && e.status === 404) await loadVersions(true)
    } finally {
      setRollingBackId(null)
    }
  }

  const openAi = () => {
    setAiResult(null)
    setAiError('')
    setAiOpen(true)
  }

  const generate = async (instruction: string) => {
    if (!detail) return
    setAiBusy(true)
    setAiError('')
    try {
      const res = await api.prompts.generate({
        projectId: currentProjectId,
        key: detail.key,
        lang: activeLang,
        instruction,
        baseBody: draft,
      })
      setAiResult(res)
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) setAiSeconds(retryAfterSec(e) ?? 10)
      setAiError(errText(lang, e))
    } finally {
      setAiBusy(false)
    }
  }

  const openDiff = (left = 'current', right = 'draft') => {
    if (!detail || !effective) return
    const sources: DiffSource[] = [
      { id: 'draft', label: t('prompts.diff.source.draft'), text: draft },
      { id: 'current', label: t('prompts.diff.source.current'), text: effective.body },
      { id: 'default', label: t('prompts.diff.source.default'), text: defaultBody },
      ...versionRows
        .filter((v) => v.kind !== 'reset')
        .map((v) => ({
          id: `v${v.versionId}`,
          label: t('prompts.diff.source.version')(versionLabel(v.versionNo, v.versionId)),
          text: v.body,
        })),
    ]
    setDiff({ title: t('prompts.diff.title'), sources, left, right })
  }

  /* -------------------------------------------------------------- 渲染 */

  const localizedTitle = (item: { title: Record<string, string> }) => item.title[lang] ?? item.title.zh ?? ''

  const filtered = items.filter((i) => !query || localizedTitle(i).toLowerCase().includes(query.trim().toLowerCase()))
  const folders = promptFolders(filtered)
  const activeItem = items.find((i) => i.key === activeKey)
  const activeFolder = activeItem ? folderOf(activeItem) : ''
  const searching = query.trim().length > 0
  const isCustomized = (item: (typeof items)[number]) => item.languages.some((l) => item.state[l]?.source === 'project')
  const openReset = (scope: ResetScope) => {
    setResetScope(scope)
    setResetError('')
    setResetOpen(true)
  }

  const title = detail ? localizedTitle(detail) : ''
  const summary = detail ? (detail.summary[lang] ?? detail.summary.zh ?? '') : ''
  const listLoading = listQuery.state === 'loading'
  const listFailed = listQuery.state === 'error'

  return (
    <>
      <PageHeader title={t('prompts.title')} desc={t('prompts.desc')} />

      <div className="flex flex-1 min-h-0 gap-4">
        {/* 左：分组列表 */}
        <aside className={testOpen ? 'studio-sidebar studio-sidebar--hidden' : 'studio-sidebar'} aria-label={t('prompts.listLabel')}>
          <div className="border-b border-line p-2.5">
            <div className="relative">
              <Icon
                name="i-lucide:search"
                className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-faint"
              />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('prompts.filterPlaceholder')}
                aria-label={t('prompts.filter')}
                className="studio-input studio-input--search"
              />
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            {listLoading && (
              <div className="space-y-2 p-1">
                {[0, 1, 2, 3, 4, 5].map((i) => (
                  <Skeleton key={i} className="h-9 w-full" />
                ))}
              </div>
            )}
            {!listLoading && listFailed && (
              <div className="p-2">
                <p className="text-xs leading-5 text-warning" role="alert">
                  {listQuery.error}
                </p>
                <Button size="sm" variant="secondary" className="mt-2" onClick={listQuery.reload}>
                  {t('common.retry')}
                </Button>
              </div>
            )}
            {!listLoading && !listFailed && folders.length === 0 && (
              <p className="px-2 py-6 text-center text-xs text-ink-faint">{t('prompts.emptyList')}</p>
            )}
            {!listLoading &&
              !listFailed &&
              folders.map((folder) => {
                const open = folderOpen(folder.id, { toggled, activeFolder, searching })
                const changed = folder.items.filter(isCustomized).length
                return (
                  <div key={folder.id} className="studio-folder">
                    <button
                      type="button"
                      className="studio-folder__head"
                      aria-expanded={open}
                      onClick={() => setToggled((prev) => ({ ...prev, [folder.id]: !open }))}
                    >
                      <Icon name="i-lucide:chevron-right" className={`size-3.5 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
                      <span className="min-w-0 flex-1 truncate text-left">
                        {FOLDER_LABEL[folder.id] ? str(FOLDER_LABEL[folder.id]) : folder.id}
                      </span>
                      {changed > 0 && <span className="studio-folder__changed">{t('prompts.folder.changed')(changed)}</span>}
                      <span className="studio-folder__count">{folder.items.length}</span>
                    </button>
                    {open && (
                      <ul className="studio-folder__list">
                        {folder.items.map((item) => {
                          const active = item.key === activeKey
                          const customized = isCustomized(item)
                          return (
                            <li key={item.key}>
                              <button
                                type="button"
                                onClick={() => requestKey(item.key)}
                                aria-current={active ? 'true' : undefined}
                                title={localizedTitle(item)}
                                className={`studio-list-item ${active ? 'studio-list-item--active' : ''}`}
                              >
                                <span className="min-w-0 flex-1 truncate text-left text-xs">{localizedTitle(item)}</span>
                                {customized && (
                                  <span className="studio-dot studio-dot--on" role="img" aria-label={t('prompts.status.source.project')} />
                                )}
                              </button>
                            </li>
                          )
                        })}
                      </ul>
                    )}
                  </div>
                )
              })}
          </div>

          <div className="border-t border-line p-2.5">
            <button
              type="button"
              className="studio-link-btn"
              disabled={!canEdit || slots.length === 0}
              onClick={() => openReset('project')}
            >
              <Icon name="i-lucide:rotate-ccw" className="size-3.5" />
              {t('prompts.reset.projectButton')}
              {slots.length > 0 && <span className="text-ink-faint">（{slots.length}）</span>}
            </button>
          </div>
        </aside>

        {/* 右：编辑区 + 试跑面板 */}
        <section className="flex min-w-0 flex-1 flex-col gap-4 min-h-0">
          <div className={`studio-editor-card ${testOpen ? 'studio-editor-card--testing' : ''}`}>
            {detailQuery.state === 'loading' && (
              <div className="space-y-3 p-5">
                <Skeleton className="h-5 w-48" />
                <Skeleton className="h-3.5 w-72" />
                <Skeleton className="h-3.5 w-2/3" />
                <Skeleton className="mt-4 h-64 w-full" />
              </div>
            )}

            {detailQuery.state === 'error' && (
              <EmptyState
                icon="i-lucide:alert-circle"
                title={detailQuery.error || t('common.failed')}
                action={
                  <Button variant="secondary" onClick={detailQuery.reload}>
                    {t('common.retry')}
                  </Button>
                }
              />
            )}

            {detailQuery.state === 'ready' && detail && effective && (
              <>
                <header className="shrink-0 border-b border-line px-5 py-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-2">
                        <h3 className="truncate text-base font-semibold text-ink">{title}</h3>
                        {isViewer && <Badge variant="secondary">{t('prompts.badge.viewer')}</Badge>}
                        {dirty && <Badge variant="warning">{t('prompts.dirty')}</Badge>}
                      </div>
                      <p className="mt-0.5 truncate text-xs leading-5 text-ink-muted">{summary}</p>
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                      <Button size="sm" variant="ghost" onClick={() => openDiff('current', dirty ? 'draft' : 'default')}>
                        <Icon name="i-lucide:code" className="size-3.5" />
                        {t('prompts.action.compare')}
                      </Button>
                      {canEdit && (
                        <>
                          <Button size="sm" variant="secondary" onClick={() => setTestSig(testOpen ? '' : slotSig)}>
                            <Icon name="i-lucide:play" className="size-3.5" />
                            {t('prompts.action.test')}
                          </Button>
                          <Button size="sm" variant="secondary" onClick={openAi}>
                            <Icon name="i-lucide:sparkles" className="size-3.5" />
                            {t('prompts.action.ai')}
                          </Button>
                          <Button size="sm" variant="ghost" disabled={effective.source === 'default'} onClick={() => openReset('lang')}>
                            {t('prompts.action.reset')}
                          </Button>
                        </>
                      )}
                      <Button size="sm" variant="secondary" onClick={openVersions}>
                        {t('prompts.action.versions')}
                      </Button>
                      {canEdit && (
                        <Button size="sm" variant="primary" disabled={!dirty || Boolean(validation && !validation.ok)} onClick={openSave}>
                          {t('prompts.action.save')}
                        </Button>
                      )}
                    </div>
                  </div>

                  <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2">
                    {detail.languages.length > 1 ? (
                      <Tabs
                        items={detail.languages.map((l) => ({ key: l, label: LANG_LABEL[l] ? str(LANG_LABEL[l]) : l }))}
                        value={activeLang}
                        onChange={requestLang}
                        label={t('prompts.langLabel')}
                      />
                    ) : (
                      <span className="text-xs text-ink-muted">{LANG_LABEL[activeLang] ? str(LANG_LABEL[activeLang]) : activeLang}</span>
                    )}

                    <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                      <Badge variant={effective.source === 'project' ? 'success' : 'secondary'}>
                        {effective.source === 'project' ? t('prompts.status.source.project') : t('prompts.status.source.default')}
                      </Badge>
                      {effective.source === 'project' && (
                        <span>
                          {[
                            versionLabel(effectiveNo, 0) || '—',
                            effective.updatedAt ? formatBeijingTime(effective.updatedAt) : '',
                            effective.author,
                          ]
                            .filter(Boolean)
                            .join(' · ')}
                        </span>
                      )}
                    </div>
                  </div>
                </header>

                <div className="studio-editor-body min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
                  <PromptCodeEditor
                    value={draft}
                    syntax={detail.syntax}
                    readOnly={!canEdit}
                    onChange={setDraft}
                    ariaLabel={t('prompts.editor.aria')}
                    placeholder={t('prompts.editor.placeholder')}
                    className={testOpen ? 'studio-code--testing' : 'studio-code--editor'}
                  />

                  <details className="studio-details">
                    <summary>
                      <Icon name="i-lucide:chevron-right" className="studio-details__chevron size-3.5" />
                      {t('prompts.details.title')}
                      <span className="text-ink-faint">{t('prompts.details.summary')(detail.placeholders.length)}</span>
                    </summary>
                    <div className="mt-2 space-y-3">
                      {detail.lockedSuffix && (
                        <div className="rounded-lg border border-line bg-surface-muted px-3.5 py-2.5">
                          <p className="text-[11px] font-medium text-ink-secondary">{t('prompts.lockedSuffix.title')}</p>
                          <pre className="studio-suffix">{detail.lockedSuffix}</pre>
                          <p className="mt-1 text-[11px] text-ink-faint">{t('prompts.lockedSuffix.desc')}</p>
                        </div>
                      )}

                      <div className="grid gap-3 rounded-lg border border-line bg-surface-muted px-3.5 py-3 sm:grid-cols-2">
                        <div className="min-w-0">
                          <p className="text-[11px] font-medium text-ink-secondary">{t('prompts.placeholders')}</p>
                          <div className="mt-1.5 flex flex-wrap gap-1.5">
                            {detail.placeholders.length === 0 && (
                              <span className="text-[11px] text-ink-faint">{t('prompts.placeholders.none')}</span>
                            )}
                            {detail.placeholders.map((p) => (
                              <span key={p.name} className="studio-chip" title={p.description[lang] ?? p.description.zh ?? ''}>
                                <code>{detail.syntax === 'dollar' ? `\${${p.name}}` : `{${p.name}}`}</code>
                                {p.required && <span className="studio-chip__star">*</span>}
                              </span>
                            ))}
                          </div>
                          <p className="mt-1.5 text-[11px] leading-5 text-ink-faint">{t('prompts.placeholders.hint')}</p>
                        </div>
                        <div className="min-w-0">
                          <p className="text-[11px] font-medium text-ink-secondary">{t('prompts.output')}</p>
                          <p className="mt-1 text-xs leading-5 text-ink-secondary">
                            {detail.outputContract.requiredKeys.length || detail.outputContract.itemKeys.length
                              ? t('prompts.output.line')(
                                  detail.outputContract.requiredKeys.join(', '),
                                  detail.outputContract.itemKeys.join(', '),
                                )
                              : t('prompts.output.free')}
                          </p>
                          <p className="mt-1 text-[11px] leading-5 text-ink-faint">
                            {detail.outputContract.description[lang] ?? detail.outputContract.description.zh ?? ''}
                          </p>
                        </div>
                      </div>
                    </div>
                  </details>

                  <div className="space-y-1.5" aria-live="polite">
                    {validating && <p className="text-[11px] text-ink-faint">{t('prompts.validation.checking')}</p>}
                    {validateError && (
                      <p className="text-xs leading-5 text-warning" role="alert">
                        {validateError}
                      </p>
                    )}
                    {validation?.errors.map((e) => (
                      <div key={`${e.code}-${e.message}`} className="studio-alert studio-alert--danger" role="alert">
                        <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
                        <span className="text-xs leading-5">{e.message}</span>
                      </div>
                    ))}
                    {validation?.warnings.map((w) => (
                      <div key={`${w.code}-${w.message}`} className="studio-alert studio-alert--warning">
                        <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
                        <span className="text-xs leading-5">{w.message}</span>
                      </div>
                    ))}
                    {validation && validation.ok && validation.warnings.length === 0 && !validating && (
                      <p className="flex items-center gap-1.5 text-[11px] text-success">
                        <Icon name="i-lucide:check-circle-2" className="size-3.5" />
                        {t('prompts.validation.ok')}
                      </p>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>

          {testOpen && detail && canEdit && (
            <PanelBoundary
              key={slotSig}
              title={t('prompts.test.crashed')}
              desc={t('prompts.test.boundaryDesc')}
              action={t('prompts.test.reopen')}
              onReset={() => setTestSig('')}
            >
              <TestPanel
                detail={detail}
                lang={activeLang}
                draft={draft}
                projectId={currentProjectId}
                layout={compareLayout}
                onLayoutChange={setCompareLayout}
                onRunResult={(r) => setRunInfo(r ? { sig: draftSig, ok: r.ok } : null)}
                onClose={() => setTestSig('')}
              />
            </PanelBoundary>
          )}
        </section>
      </div>

      {/* 下面这些对话框只在打开时挂载：内部状态（备注、勾选、展开）每次打开都是新的 */}
      {saveOpen && (
        <SaveDialog
          open
          onClose={() => setSaveOpen(false)}
          validation={validation}
          confirmRequired={confirmRequired}
          saving={saving}
          error={saveError}
          conflict={conflict}
          maxNote={MAX_NOTE}
          onSubmit={(note) => void submitSave(note)}
          onReloadLatest={() => {
            setSaveOpen(false)
            reloadBoth()
          }}
          onCompareConflict={() => {
            setSaveOpen(false)
            if (!detail || !effective) return
            setDiff({
              title: t('prompts.diff.title'),
              sources: [
                { id: 'latest', label: t('prompts.conflict.latest'), text: conflict?.latestBody ?? effective.body },
                { id: 'draft', label: t('prompts.diff.source.draft'), text: draft },
              ],
              // 旧 → 新：左边是服务端最新版本，右边是我的草稿
              left: 'latest',
              right: 'draft',
            })
          }}
        />
      )}

      {resetOpen && (
        <ResetDialog
          open
          onClose={() => setResetOpen(false)}
          scope={resetScope}
          onScopeChange={setResetScope}
          currentBody={effective?.body ?? ''}
          defaultBody={defaultBody}
          languageCount={detail?.languages.length ?? 1}
          customizedCount={slots.length}
          resetting={resetting}
          error={resetError}
          onConfirm={() => void submitReset()}
        />
      )}

      {aiOpen && (
        <AiDialog
          open
          onClose={() => setAiOpen(false)}
          syntax={detail?.syntax ?? 'dollar'}
          busy={aiBusy}
          retryIn={aiSeconds}
          error={aiError}
          result={aiResult}
          onGenerate={(instruction) => void generate(instruction)}
          onApply={(body) => {
            setDraft(body)
            setAiOpen(false)
            toast(t('prompts.ai.done'))
          }}
        />
      )}

      {versionsOpen && (
        <VersionDrawer
          open
          onClose={() => setVersionsOpen(false)}
          versions={versions}
          hasMore={vHasMore}
          loading={vLoading}
          error={vError}
          canEdit={canEdit}
          rollingBackId={rollingBackId}
          onLoadMore={() => void loadVersions(false)}
          onCompare={(v) => {
            if (!detail || !effective) return
            setDiff({
              title: t('prompts.versions.compare'),
              sources: [
                { id: `v${v.versionId}`, label: t('prompts.diff.source.version')(versionLabel(v.versionNo, v.versionId)), text: v.body },
                { id: 'current', label: t('prompts.diff.source.current'), text: effective.body },
              ],
              // 旧 → 新：左边是历史版本，右边是当前生效
              left: `v${v.versionId}`,
              right: 'current',
            })
          }}
          onRollback={(v) => void rollback(v)}
        />
      )}

      {diff && (
        <DiffDialog open title={diff.title} sources={diff.sources} leftId={diff.left} rightId={diff.right} onClose={() => setDiff(null)} />
      )}

      {/* 未保存：切换提示词 / 语言 */}
      <Dialog
        open={Boolean(pendingSwitch)}
        title={t('prompts.unsaved.title')}
        onClose={() => setPendingSwitch(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingSwitch(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (pendingSwitch?.kind === 'key') setSelectedKey(pendingSwitch.value)
                else if (pendingSwitch?.kind === 'lang') setLangId(pendingSwitch.value)
                setPendingSwitch(null)
              }}
            >
              {t('prompts.unsaved.confirm')}
            </Button>
          </>
        }
      >
        <p className="text-sm leading-6 text-ink-secondary">{t('prompts.unsaved.desc')}</p>
      </Dialog>

      {/* 未保存：离开页面 */}
      <Dialog
        open={Boolean(leaveTo)}
        title={t('prompts.leave.title')}
        onClose={() => setLeaveTo(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setLeaveTo(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                const to = leaveTo
                setLeaveTo(null)
                if (to) navigate(to)
              }}
            >
              {t('prompts.leave.confirm')}
            </Button>
          </>
        }
      >
        <p className="text-sm leading-6 text-ink-secondary">{t('prompts.leave.desc')}</p>
      </Dialog>
    </>
  )
}
