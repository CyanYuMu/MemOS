import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAsync } from '../lib/useAsync'
import { api } from '../lib/api'
import { Dialog, PageHeader, Pager, Panel, SearchBox, StateRow, Table, TD, Th } from '../components/ui'
import { Tabs } from '../components/ui/tabs'
import { RowActions } from '../components/ui/row-actions'
import { Checkbox } from '../components/ui/checkbox'
import { Button } from '../components/ui/button'
import { MemoryDrawer } from '../components/MemoryDrawer'
import { usePlatformGuide } from '../lib/onboarding'
import { useProject, useT, useToast, useLang } from '../lib/store'
import { errText } from '../lib/i18n'

/** 标签：类型值 + i18n 词条 */
const TABS = [
  { type: 'text_mem', label: 'memory.tab.text_mem' },
  { type: 'pref_mem', label: 'memory.tab.pref_mem' },
  { type: 'event_mem', label: 'memory.tab.event_mem' },
  { type: 'profile_mem', label: 'memory.tab.profile_mem' },
  { type: 'tool_mem', label: 'memory.tab.tool_mem' },
] as const

export function Memories() {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const { currentProjectId, ready } = useProject()
  const [chosenTab, setChosenTab] = useState<(typeof TABS)[number]['type'] | null>(null)
  const [page, setPage] = useState(1)
  const [draft, setDraft] = useState('')
  const [q, setQ] = useState('')
  const [tick, setTick] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const guide = usePlatformGuide()
  const guiding = !!guide?.available && guide.state?.status === 'active' && guide.state.step === 4
  /** 指引第 5 步要点开一条记忆：默认页签是空的而别的页签有数据时，先显示有数据的页签，
   *  不然高亮目标不存在、用户无从下手。用户自己点过页签就不再替他选。 */
  const guideTab = guiding && (counts[TABS[0].type] ?? 0) === 0 ? TABS.find((x) => (counts[x.type] ?? 0) > 0)?.type : undefined
  const tab = chosenTab ?? guideTab ?? TABS[0].type
  const [pendingDelete, setPendingDelete] = useState<string[] | null>(null)
  const [detail, setDetail] = useState<{
    memoryId: string
    tab: (typeof TABS)[number]['type']
    projectId: string
    opener: HTMLElement | null
  } | null>(null)

  const pageSize = 10

  const { data, state, error, reload } = useAsync(
    () =>
      api.memory.list({
        projectId: currentProjectId,
        memoryDetailType: tab,
        pageNum: page,
        pageSize,
        memSearchContent: q,
      }),
    [currentProjectId, tab, page, q, tick],
    // 字典没回来之前 `currentProjectId` 是空串，发出去必被 BFF 拒（40000 未知项目），
    // 页面会先闪一段「请求失败」再打第二遍
    { skip: !ready },
  )
  const rows = useMemo(() => data?.memListItemResponseList ?? [], [data])
  const total = data?.total ?? 0

  /** 标签上的条数：每种类型各取一次 total，只取 1 条 */
  useEffect(() => {
    if (!currentProjectId) return
    let alive = true
    Promise.all(
      TABS.map((x) =>
        api.memory
          .list({ projectId: currentProjectId, memoryDetailType: x.type, pageNum: 1, pageSize: 1 })
          .then((r) => [x.type, r.total] as const)
          .catch(() => [x.type, 0] as const),
      ),
    ).then((pairs) => {
      if (alive) setCounts(Object.fromEntries(pairs))
    })
    return () => {
      alive = false
    }
  }, [currentProjectId, tick])

  const doDelete = useCallback(
    async (ids: string[]) => {
      setPendingDelete(null)
      try {
        await api.memory.remove(currentProjectId, ids, tab)
        toast(t('memory.deleteDone')(ids.length))
        setTick((x) => x + 1)
      } catch (e) {
        toast(errText(lang, e), 'err')
      }
    },
    [currentProjectId, tab, t, toast, lang],
  )

  const openDetail = useCallback(
    (memoryId: string, type: (typeof TABS)[number]['type'] = tab) => {
      setDetail({ memoryId, tab: type, projectId: currentProjectId, opener: document.activeElement as HTMLElement | null })
    },
    [currentProjectId, tab],
  )

  /** 「事件记忆」里混着两种类型（Context / InsightMemory），必须列出来才分得清。
   *  其余页签一个页签只有一种类型，再列一列是噪音。 */
  const showMemoryType = tab === 'event_mem'
  const colCount = 4 + (showMemoryType ? 1 : 0) + 2

  /* 「用户记忆」标签上原来还有一列「画像模板 ID」（模板 id / 实例 id）。
   * 2026-10-19 去掉：这个部署的上游**不下发**模板 id（实测 110 条 UserMemory 里 0 条有
   * `template_id`），而实例 id 早先是从 `working_binding` 取的——那是 WorkingMemory 的
   * 血缘 id，多数还等于这条记忆自己的 id，等于在「画像模板 ID」底下把行 id 又印一遍。
   * 两处都取不到值，那一列在页面上就是一片 `-`。
   *
   * 详情抽屉里那段（`detail.memory.profileTemplateId && …`）**留着**：它自带判断，
   * 上游哪天真的下发模板 id 就会自己出现。 */

  /** 只认当前页还在的 id，翻页、换标签、重新取数后旧选中自动失效 */
  const liveSelected = useMemo(() => {
    const ids = new Set(rows.map((m) => m.memoryId))
    return selected.filter((id) => ids.has(id))
  }, [rows, selected])

  const allChecked = rows.length > 0 && liveSelected.length === rows.length
  const someChecked = liveSelected.length > 0 && !allChecked

  return (
    <div className="platform-guide-page">
      <div data-v-1df75f9b="" className="w-full h-full flex flex-col gap-y-4">
        <PageHeader
          title={t('memory.title')}
          desc={t('memory.desc')}
          className=""
          actions={
            <>
              <Button
                variant={liveSelected.length === 0 ? 'secondary' : 'destructive'}
                size="sm"
                disabled={liveSelected.length === 0}
                onClick={() => setPendingDelete(liveSelected)}
              >
                {t('memory.action.batchDelete')}
              </Button>
              <SearchBox
                value={draft}
                onChange={setDraft}
                onSubmit={() => {
                  setQ(draft.trim())
                  setPage(1)
                }}
                placeholder={t('memory.searchPlaceholder')}
              />
            </>
          }
        />

        <Tabs
          label={t('memory.title')}
          value={tab}
          onChange={(k) => {
            setChosenTab(k)
            setPage(1)
          }}
          items={TABS.map((x) => ({ key: x.type, label: t(x.label), count: counts[x.type] ?? 0 }))}
        />

        <div className="flex-1 min-h-0 overflow-hidden">
          <div className="flex h-full min-h-0 flex-col gap-3">
            <Panel className="memory-list-table flex-1 min-h-0">
              <Table
                head={
                  <>
                    <Th>
                      <div className="relative items-start flex-row flex justify-center">
                        <div className="flex items-center h-5">
                          <Checkbox
                            checked={allChecked}
                            indeterminate={someChecked}
                            onCheckedChange={(v) => setSelected(v ? rows.map((m) => m.memoryId) : [])}
                          />
                        </div>
                      </div>
                    </Th>
                    <Th>{t('memory.col.createdAt')}</Th>
                    <Th>{t('memory.col.subjectType')}</Th>
                    <Th>{t('memory.col.subjectId')}</Th>
                    {showMemoryType && <Th>{t('memory.col.memoryType')}</Th>}
                    <Th>{t('memory.col.content')}</Th>
                    <Th>{t('memory.col.action')}</Th>
                  </>
                }
              >
                {state !== 'ready' && <StateRow colSpan={colCount} state={state} error={error} onRetry={reload} />}
                {state === 'ready' && rows.length === 0 && (
                  <StateRow
                    colSpan={colCount}
                    state="ready"
                    emptyTitle={q ? t('common.noMatch') : undefined}
                    emptyAction={
                      q ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => {
                            setDraft('')
                            setQ('')
                            setPage(1)
                          }}
                        >
                          {t('common.clearSearch')}
                        </Button>
                      ) : undefined
                    }
                  />
                )}
                {state === 'ready' &&
                  rows.map((m, index) => (
                    <tr
                      data-guide={index === 0 ? 'memory-row' : undefined}
                      key={m.memoryId}
                      className="data-[selected=true]:bg-elevated/50"
                    >
                      <td data-pinned="false" className={TD}>
                        <div className="flex justify-center">
                          <Checkbox
                            checked={liveSelected.includes(m.memoryId)}
                            onCheckedChange={(v) =>
                              setSelected((prev) => (v ? [...prev, m.memoryId] : prev.filter((x) => x !== m.memoryId)))
                            }
                          />
                        </div>
                      </td>
                      <td data-pinned="false" className={TD}>
                        {m.createTime}
                      </td>
                      <td data-pinned="false" className={TD}>
                        {t(`memory.subjectType.${m.subjectType}` as 'memory.subjectType.user')}
                      </td>
                      <td data-pinned="false" className={TD}>
                        {m.externalUserId}
                      </td>
                      {showMemoryType && (
                        <td data-pinned="false" className={`${TD} whitespace-nowrap`}>
                          {m.memoryType ? t(`graph.type.${m.memoryType}` as 'graph.type.Other') : '-'}
                        </td>
                      )}
                      <td data-pinned="false" className={`${TD} max-w-[520px] truncate`}>
                        {m.memoryContent}
                      </td>
                      <td data-pinned="false" className={TD}>
                        <RowActions
                          actions={[
                            { label: t('memory.action.detail'), icon: 'i-lucide:eye', onClick: () => openDetail(m.memoryId) },
                            {
                              label: t('memory.action.delete'),
                              icon: 'i-lucide:trash-2',
                              tone: 'danger',
                              onClick: () => setPendingDelete([m.memoryId]),
                            },
                          ]}
                        />
                      </td>
                    </tr>
                  ))}
              </Table>
            </Panel>
            <Pager page={page} pageSize={pageSize} total={total} onPage={setPage} />
          </div>
        </div>
      </div>

      <Dialog
        open={!!pendingDelete}
        title={t('memory.action.deleteConfirmTitle')}
        onClose={() => setPendingDelete(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => pendingDelete && doDelete(pendingDelete)}>
              {t('common.delete')}
            </Button>
          </>
        }
      >
        {t('memory.deleteConfirm')(pendingDelete?.length ?? 0)}
      </Dialog>

      {detail?.projectId === currentProjectId && (
        <MemoryDrawer
          key={`${currentProjectId}:${detail.memoryId}`}
          projectId={currentProjectId}
          memoryId={detail.memoryId}
          memType={detail.tab}
          returnFocus={detail.opener}
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  )
}
