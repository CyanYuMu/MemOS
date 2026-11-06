import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api, type LocalMemory, type LocalMemoryDetail, type LocalSyncResult } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { formatBytes, formatDateTime, toRFC3339 } from '../lib/format'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Checkbox } from '../components/ui/checkbox'
import { Drawer } from '../components/ui/drawer'
import { EmptyState } from '../components/ui/empty-state'
import { RowActions } from '../components/ui/row-actions'
import { Dialog, Pager, PageHeader, Panel, PrimaryButton, SearchBox, StateRow, Table, TD, Th } from '../components/ui'
import { FilterDateRange, FilterField, FilterFieldRow, FilterPopover, FilterSelect } from '../components/FilterPopover'
import { FILTER_LABEL } from '../lib/cls'
import { errText, type TKey } from '../lib/i18n'
import { useCanWriteProject, useLang, useProject, useStr, useToast, useT } from '../lib/store'

const LAYERS = ['trace', 'policy', 'world_model'] as const

const LAYER_KEY: Record<(typeof LAYERS)[number], TKey> = {
  trace: 'localMemories.layer.trace',
  policy: 'localMemories.layer.policy',
  world_model: 'localMemories.layer.world_model',
}

const PAGE_SIZE = 10

/** 转存是后台作业：点完只拿得到「已入队」，终态要回查列表。
 *  轮询 2s 一次、最多 30s（作业失败还有 worker 退避重试，30s 只是前台忍耐上限）。 */
const POLL_INTERVAL_MS = 2000
const POLL_MAX_MS = 30_000

/** 后端 `promote_status`：queued | running | succeeded | failed，空串表示没有作业。
 *  `promoted` 等价于 succeeded；failed 的行允许再次入队（后端会清空旧的 promote_error）。 */
type PromoteState = 'promoted' | 'pending' | 'failed' | 'idle'

function promoteStateOf(m: LocalMemory): PromoteState {
  if (m.promoted) return 'promoted'
  if (m.promoteStatus === 'queued' || m.promoteStatus === 'running') return 'pending'
  if (m.promoteStatus === 'failed' || m.promoteError) return 'failed'
  return 'idle'
}

/** 只有 idle 与 failed 能发起转存；转存中的行重复入队没有意义，已转存的行禁止操作。 */
function canStartPromote(m: LocalMemory): boolean {
  const state = promoteStateOf(m)
  return state === 'idle' || state === 'failed'
}

interface Filters {
  deviceId: string
  layer: string
  startTime: string
  endTime: string
}

const NO_FILTER: Filters = { deviceId: '', layer: '', startTime: '', endTime: '' }

/** payload 可能是对象，也可能被后端序列化成了字符串；两种都尽量显示成结构。 */
function normalizePayload(payload: unknown): unknown {
  if (typeof payload !== 'string') return payload
  const text = payload.trim()
  if (!text) return payload
  try {
    return JSON.parse(text)
  } catch {
    return payload
  }
}

/** 层次化渲染任意 JSON。只负责把结构摊开，不假设某一层的字段名。 */
function PayloadView({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-ink-faint">—</span>
  if (typeof value === 'string') return <span className="whitespace-pre-wrap break-words">{value || '—'}</span>
  if (typeof value === 'number' || typeof value === 'boolean') return <span className="font-mono">{String(value)}</span>
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-ink-faint">[]</span>
    return (
      <div className="space-y-1.5">
        {value.map((item, index) => (
          <div key={index} className="flex gap-2">
            <span className="w-6 shrink-0 text-right font-mono text-[11px] leading-5 text-ink-faint">{index}</span>
            <div className="min-w-0 flex-1">
              <PayloadView value={item} />
            </div>
          </div>
        ))}
      </div>
    )
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length === 0) return <span className="text-ink-faint">{'{}'}</span>
    return (
      <div className="space-y-1.5">
        {entries.map(([key, item]) => (
          <div key={key} className="grid grid-cols-[minmax(96px,160px)_1fr] gap-x-3 gap-y-0.5">
            <span className="break-words font-mono text-[11px] leading-5 text-ink-muted">{key}</span>
            <div className="min-w-0">
              <PayloadView value={item} />
            </div>
          </div>
        ))}
      </div>
    )
  }
  return <span>{String(value)}</span>
}

type EmptyKind = 'capabilityOff' | 'deviceError' | 'noDevices' | 'revoked' | 'quota' | 'filtered' | 'syncOff'

export function LocalMemories() {
  const t = useT()
  const str = useStr()
  const { lang } = useLang()
  const toast = useToast()
  const { currentProjectId, currentProjectName, ready } = useProject()
  /** viewer 只能看：写入口在后端也是 403，这里先隐藏，避免点了才报错。 */
  const canWrite = useCanWriteProject()

  const [filters, setFilters] = useState<Filters>(NO_FILTER)
  const [draft, setDraft] = useState<Filters>(NO_FILTER)
  const [filterOpen, setFilterOpen] = useState(false)
  const [q, setQ] = useState('')
  const [draftQ, setDraftQ] = useState('')
  const [page, setPage] = useState(1)
  const [tick, setTick] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const [detail, setDetail] = useState<{ loading: boolean; error: string; memory: LocalMemoryDetail | null } | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string[] | null>(null)
  const [promoteIds, setPromoteIds] = useState<string[] | null>(null)
  const [promoting, setPromoting] = useState(false)
  const [promoteResult, setPromoteResult] = useState<LocalSyncResult | null>(null)
  /** 轮询截止时间戳；0 表示不轮询。只由 promote 入队触发，不随筛选条件变化。 */
  const [pollDeadline, setPollDeadline] = useState(0)
  const sawPendingRef = useRef(false)

  const devicesQuery = useAsync(() => api.localSync.devices(currentProjectId), [currentProjectId, tick], {
    skip: !ready || !currentProjectId,
    key: 'localSync.devices',
  })
  const devices = useMemo(() => devicesQuery.data?.devices ?? [], [devicesQuery.data])
  const quota = devicesQuery.data?.quota
  const deviceMap = useMemo(() => new Map(devices.map((d) => [d.deviceId, d])), [devices])
  const summaryQuery = useAsync(() => api.localSync.summary(currentProjectId), [currentProjectId, tick], {
    skip: !ready || !currentProjectId,
    key: 'localSync.summary',
  })
  const summary = summaryQuery.data

  const {
    data,
    state,
    error,
    rawError,
    reload: reloadMemories,
  } = useAsync(
    () =>
      api.localSync.memories({
        projectId: currentProjectId,
        layer: filters.layer,
        deviceId: filters.deviceId,
        q,
        startTime: toRFC3339(filters.startTime),
        endTime: toRFC3339(filters.endTime),
        page,
        pageSize: PAGE_SIZE,
      }),
    [currentProjectId, filters, q, page, tick],
    { skip: !ready || !currentProjectId, key: 'localSync.memories' },
  )
  const rows = useMemo(() => data?.records ?? [], [data])
  const total = data?.total ?? 0
  const pendingCount = useMemo(() => rows.filter((m) => promoteStateOf(m) === 'pending').length, [rows])

  /** 有进行中的转存就回查，最多 POLL_MAX_MS；卸载或截止由 cleanup 清掉定时器。
   *  轮询只 refetch 当前 query，不改 filters/page，列表筛选状态不受影响。
   *  一旦看见过 queued/running，等它们全部落终态就提前停止，不空转到 30s。 */
  useEffect(() => {
    if (!pollDeadline) return
    if (pendingCount > 0) sawPendingRef.current = true
    const timer = window.setInterval(() => {
      if (Date.now() >= pollDeadline || (sawPendingRef.current && pendingCount === 0)) {
        window.clearInterval(timer)
        setPollDeadline(0)
        return
      }
      reloadMemories()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [pollDeadline, pendingCount, reloadMemories])

  // 准入口径是“已落库 + 未提交批次”：只看 usedBytes 会显示还有空间，上传却 413。
  const quotaPendingBytes = quota?.pendingBytes ?? 0
  const quotaPendingRows = quota?.pendingRows ?? 0
  const quotaBytesUsed = (quota?.usedBytes ?? 0) + quotaPendingBytes
  const quotaRowsUsed = (quota?.usedRows ?? 0) + quotaPendingRows
  const quotaBytesExceeded = !!quota && quota.limitBytes > 0 && quotaBytesUsed >= quota.limitBytes
  const quotaRowsExceeded = !!quota && quota.limitRows > 0 && quotaRowsUsed >= quota.limitRows
  const quotaExceeded = quotaBytesExceeded || quotaRowsExceeded
  const quotaPercent = quota && quota.limitBytes > 0 ? Math.min(100, Math.round((quotaBytesUsed / quota.limitBytes) * 100)) : 0

  const filtersActive = Boolean(filters.deviceId || filters.layer || filters.startTime || filters.endTime || q)

  /** 能力未开启（50300）要单独成态：这时候不是「没有数据」，而是整条通道不可用 */
  const capabilityOff =
    (devicesQuery.rawError instanceof ApiError && devicesQuery.rawError.code === 50300) ||
    (rawError instanceof ApiError && rawError.code === 50300)
  const deviceFailed = !capabilityOff && devicesQuery.state === 'error'

  const emptyKind: EmptyKind = useMemo(() => {
    if (capabilityOff) return 'capabilityOff'
    if (deviceFailed) return 'deviceError'
    if (devicesQuery.state === 'ready' && devices.length === 0) return 'noDevices'
    if (devices.length > 0 && devices.every((d) => d.status === 'revoked')) return 'revoked'
    if (quotaExceeded) return 'quota'
    // 设备从未成功上传过时，即便带着筛选条件也不该说「筛选结果为空」——
    // 那会把人引向改筛选，而真正该做的是去插件里开启上传。
    if (devices.length > 0 && devices.every((d) => !d.lastSeenAt)) return 'syncOff'
    if (filtersActive) return 'filtered'
    return 'syncOff'
  }, [capabilityOff, deviceFailed, devicesQuery.state, devices, quotaExceeded, filtersActive])

  /** 只有未转存且不在转存中的行能进队列。失败行可以重试；转存中的行重复入队没有意义。 */
  const selectableIds = useMemo(() => rows.filter((m) => canStartPromote(m)).map((m) => m.id), [rows])

  /** 只认当前页还在的、且仍可转存的 id；翻页、换筛选、转存完成后旧选中自动失效 */
  const liveSelected = useMemo(() => {
    const ids = new Set(selectableIds)
    return selected.filter((id) => ids.has(id))
  }, [selectableIds, selected])

  const promotableSelected = liveSelected
  const allChecked = selectableIds.length > 0 && liveSelected.length === selectableIds.length
  const someChecked = liveSelected.length > 0 && !allChecked

  /** 选中的全是失败行时，确认框按「重试转存」取词；混合选择仍按普通转存展示。 */
  const promotingRetry = useMemo(() => {
    if (!promoteIds || promoteIds.length === 0) return false
    const byId = new Map(rows.map((m) => [m.id, m]))
    return promoteIds.every((id) => {
      const memory = byId.get(id)
      return !!memory && promoteStateOf(memory) === 'failed'
    })
  }, [promoteIds, rows])

  const resetFilters = () => {
    setDraft(NO_FILTER)
    setFilters(NO_FILTER)
    setDraftQ('')
    setQ('')
    setPage(1)
  }

  const openDetail = useCallback(
    async (id: string) => {
      setDetail({ loading: true, error: '', memory: null })
      try {
        const memory = await api.localSync.memory(currentProjectId, id)
        setDetail({ loading: false, error: '', memory })
      } catch (e) {
        setDetail({ loading: false, error: errText(lang, e), memory: null })
      }
    },
    [currentProjectId, lang],
  )

  const doDelete = async (ids: string[]) => {
    setPendingDelete(null)
    try {
      const results = await Promise.allSettled(ids.map((id) => api.localSync.removeMemory(currentProjectId, id)))
      const ok = results.filter((r) => r.status === 'fulfilled').length
      const failed = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected')
      if (ok > 0) toast(t('localMemories.deleteDone')(ok))
      if (failed.length > 0) toast(errText(lang, failed[0].reason), 'err')
      setDetail(null)
      setTick((x) => x + 1)
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const doPromote = async () => {
    if (!promoteIds || promoteIds.length === 0) return
    setPromoting(true)
    try {
      const result = await api.localSync.promote(currentProjectId, promoteIds)
      setPromoteIds(null)
      setPromoteResult(result)
      // 抽屉里的按钮/状态基于旧详情；队列完成后要重新打开才有新状态
      setDetail(null)
      if (result.queued.length > 0) toast(t('localMemories.promoteQueued')(result.queued.length))
      if (result.failed.length > 0) toast(t('localMemories.promotePartial')(result.queued.length, result.failed.length), 'err')
      // 入队即开始回查终态；最多 30s，看到 pending 落终态就提前停。
      if (result.queued.length > 0) {
        sawPendingRef.current = false
        setPollDeadline(Date.now() + POLL_MAX_MS)
      }
      setTick((x) => x + 1)
    } catch (e) {
      // 失败时保留确认框，用户可以直接重试
      toast(errText(lang, e), 'err')
    } finally {
      setPromoting(false)
    }
  }

  /** 批量结果里的失败项单独再入队：不让「批量按钮已禁用」把单条重试路径堵死。 */
  const retryFailedPromotions = () => {
    if (!promoteResult || promoteResult.failed.length === 0) return
    setPromoteIds(promoteResult.failed.map((f) => f.id))
    setPromoteResult(null)
  }

  const deviceLabel = (m: LocalMemory) => m.deviceLabel || deviceMap.get(m.deviceId)?.label || m.deviceId
  const layerLabel = (layer: string) => {
    const key = LAYER_KEY[layer as (typeof LAYERS)[number]]
    return key ? str(key) : layer
  }
  /** 转存状态。失败原因与作业状态都是服务端原文：失败原因在状态列直接显示，
   *  并放进 title 供悬停查看，不翻译。 */
  const promoteBadge = (m: LocalMemory) => {
    const state = promoteStateOf(m)
    if (state === 'promoted') return <Badge variant="success">{t('localMemories.status.promoted')}</Badge>
    if (state === 'failed') {
      return (
        <Badge variant="destructive" title={m.promoteError || undefined}>
          {t('localMemories.status.promoteFailed')}
        </Badge>
      )
    }
    if (state === 'pending') {
      return (
        <Badge variant="warning" title={m.promoteStatus}>
          {t('localMemories.status.promoting')(m.promoteStatus)}
        </Badge>
      )
    }
    return <Badge variant="secondary">{t('localMemories.status.localOnly')}</Badge>
  }
  /** 失败行给的是「重试转存」，其余可转存行是「转存为云端记忆」。 */
  const promoteActionLabel = (m: LocalMemory) =>
    promoteStateOf(m) === 'failed' ? t('localMemories.action.retryPromote') : t('localMemories.action.promote')
  /** 不可操作时说明原因：已转存 / 转存进行中。 */
  const promoteLockedLabel = (m: LocalMemory) => {
    const state = promoteStateOf(m)
    if (state === 'promoted') return t('localMemories.action.promotedLocked')
    if (state === 'pending') return t('localMemories.action.promotingLocked')
    return undefined
  }
  const deviceOptions = devices.map((d) => ({
    value: d.deviceId,
    label: d.status === 'revoked' ? t('localMemories.deviceRevoked')(d.label || d.deviceId) : d.label || d.deviceId,
  }))

  const emptyCopy = (kind: EmptyKind) => {
    switch (kind) {
      case 'capabilityOff':
        return {
          icon: 'i-lucide:plug',
          title: t('localMemories.empty.capabilityOff.title'),
          desc: t('localMemories.empty.capabilityOff.desc'),
          action: null,
        }
      case 'deviceError':
        return {
          icon: 'i-lucide:alert-circle',
          title: t('localMemories.empty.deviceError.title'),
          // 服务端原文不翻译：这是排障依据
          desc: devicesQuery.error || t('common.failed'),
          action: (
            <Button variant="secondary" onClick={devicesQuery.reload}>
              {t('common.refresh')}
            </Button>
          ),
        }
      case 'noDevices':
        return {
          icon: 'i-lucide:monitor',
          title: t('localMemories.empty.noDevices.title'),
          desc: t('localMemories.empty.noDevices.desc'),
          action: (
            <Link
              to="/cn/projects?tab=devices"
              className="inline-flex h-8 items-center rounded-lg bg-accent px-3 text-xs font-medium text-on-accent transition-colors hover:bg-accent-hover"
            >
              {t('localMemories.empty.noDevices.action')}
            </Link>
          ),
        }
      case 'revoked':
        return {
          icon: 'i-lucide:shield-off',
          title: t('localMemories.empty.revoked.title'),
          desc: t('localMemories.empty.revoked.desc'),
          action: (
            <Link
              to="/cn/projects?tab=devices"
              className="inline-flex h-8 items-center rounded-lg border border-line bg-surface px-3 text-xs font-medium text-ink-secondary transition-colors hover:bg-surface-hover hover:text-ink"
            >
              {t('localMemories.empty.revoked.action')}
            </Link>
          ),
        }
      case 'quota':
        return {
          icon: 'i-lucide:alert-circle',
          title: t('localMemories.empty.quota.title'),
          desc: t('localMemories.empty.quota.desc')(quota ? formatBytes(quota.limitBytes) : '-'),
          action: (
            <Button variant="secondary" onClick={() => setTick((x) => x + 1)}>
              {t('common.refresh')}
            </Button>
          ),
        }
      case 'filtered':
        return {
          icon: 'i-lucide:file-search',
          title: t('localMemories.empty.filtered.title'),
          desc: t('localMemories.empty.filtered.desc'),
          action: (
            <Button variant="secondary" onClick={resetFilters}>
              {t('filter.reset')}
            </Button>
          ),
        }
      default:
        return {
          icon: 'i-lucide:plug',
          title: t('localMemories.empty.syncOff.title'),
          desc: t('localMemories.empty.syncOff.desc'),
          action: null,
        }
    }
  }

  const empty = emptyCopy(emptyKind)
  const colCount = 7
  /** 能力未开启时整页不可用；设备列表失败但镜像还有数据时保留列表，另给告警条。
   *  设备信息没回来之前不要判「无设备 / 未开启同步」——那是误判，不是空态。 */
  const showEmptyState =
    capabilityOff || (deviceFailed && rows.length === 0) || (state === 'ready' && devicesQuery.state === 'ready' && rows.length === 0)
  const showTableLoading = !showEmptyState && (state !== 'ready' || (devicesQuery.state === 'loading' && rows.length === 0))

  /** 没有当前项目时不发请求：空 projectId 会被 BFF 以 40000 拒绝，先把人引去选项目。
   *  项目字典还没落定时先走下面的加载态，避免首帧闪一下「没有项目」。
   *  与本地设备页共用同一套 noProject 文案。 */
  if (ready && !currentProjectId) {
    return (
      <>
        <PageHeader title={t('localMemories.title')} desc={t('localMemories.desc')} />
        <div className="flex min-h-0 flex-1 flex-col">
          <Panel className="min-h-72 flex-1">
            <EmptyState icon="i-lucide:folder-plus" title={t('devices.noProject.title')} desc={t('devices.noProject.desc')} />
          </Panel>
        </div>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title={t('localMemories.title')}
        desc={canWrite ? t('localMemories.desc') : `${t('localMemories.desc')} · ${t('localMemories.readOnly')}`}
        actions={
          <>
            {pendingCount > 0 && (
              <Button variant="secondary" size="md" onClick={reloadMemories}>
                {t('localMemories.action.refreshStatus')}
              </Button>
            )}
            {canWrite && (
              <PrimaryButton
                icon="i-lucide:arrow-up-right"
                disabled={promotableSelected.length === 0}
                onClick={() => setPromoteIds(promotableSelected)}
              >
                {promotableSelected.length > 0
                  ? t('localMemories.action.batchPromoteCount')(promotableSelected.length)
                  : t('localMemories.action.batchPromote')}
              </PrimaryButton>
            )}
            <SearchBox
              value={draftQ}
              onChange={setDraftQ}
              onSubmit={() => {
                setQ(draftQ.trim())
                setPage(1)
              }}
              onFilter={() => setFilterOpen((o) => !o)}
              filterOpen={filterOpen}
              filter={
                <FilterPopover
                  open
                  onFilter={() => {
                    setFilters(draft)
                    setPage(1)
                    setFilterOpen(false)
                  }}
                  onReset={resetFilters}
                >
                  <FilterFieldRow>
                    <div>
                      <label className={FILTER_LABEL}>{t('localMemories.filter.device')}</label>
                      <FilterSelect
                        value={draft.deviceId}
                        onChange={(deviceId) => setDraft((d) => ({ ...d, deviceId }))}
                        placeholder={t('localMemories.filter.allDevices')}
                        options={deviceOptions}
                      />
                    </div>
                    <div>
                      <label className={FILTER_LABEL}>{t('localMemories.filter.layer')}</label>
                      <FilterSelect
                        value={draft.layer}
                        onChange={(layer) => setDraft((d) => ({ ...d, layer }))}
                        placeholder={t('localMemories.filter.allLayers')}
                        options={LAYERS.map((layer) => ({ value: layer, label: str(LAYER_KEY[layer]) }))}
                      />
                    </div>
                  </FilterFieldRow>
                  <FilterField label={t('localMemories.filter.updatedAt')}>
                    <FilterDateRange
                      start={draft.startTime}
                      end={draft.endTime}
                      onChange={(startTime, endTime) => setDraft((d) => ({ ...d, startTime, endTime }))}
                    />
                  </FilterField>
                </FilterPopover>
              }
              placeholder={t('localMemories.searchPlaceholder')}
            />
          </>
        }
      />

      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs leading-5 text-ink-muted">
        <Badge variant="outline">{t('localMemories.mirrorBadge')}</Badge>
        <span>{t('localMemories.mirrorNote')}</span>
        {quota && (
          <span className={quotaExceeded || quotaPercent >= 80 ? 'font-medium text-warning' : undefined}>
            {t('localMemories.quota.summary')(
              formatBytes(quota.usedBytes),
              formatBytes(quota.limitBytes),
              quota.usedRows.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US'),
              quota.limitRows.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US'),
            )}
            {(quotaPendingBytes > 0 || quotaPendingRows > 0) &&
              t('localMemories.quota.pending')(
                formatBytes(quotaPendingBytes),
                quotaPendingRows.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US'),
              )}
          </span>
        )}
        {summary && summary.total > 0 && (
          <span className="flex flex-wrap items-center gap-x-2">
            <span>{t('localMemories.summary.layers')}</span>
            {LAYERS.map((layer) => (
              <span key={layer}>
                {layerLabel(layer)} {summary.layers[layer] ?? 0}
              </span>
            ))}
            <span>·</span>
            <span>
              {t('localMemories.summary.promoted')} {summary.promoted}
            </span>
            {summary.deleted > 0 && (
              <span>
                {t('localMemories.summary.deleted')} {summary.deleted}
              </span>
            )}
          </span>
        )}
      </div>

      {deviceFailed && rows.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-xs text-warning">
          <span className="min-w-0 break-words">
            {t('localMemories.empty.deviceError.title')}: {devicesQuery.error}
          </span>
          <Button size="sm" variant="secondary" onClick={devicesQuery.reload}>
            {t('common.refresh')}
          </Button>
        </div>
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-3">
        <Panel className="flex-1 min-h-0">
          {showEmptyState ? (
            <EmptyState icon={empty.icon} title={empty.title} desc={empty.desc} action={empty.action} />
          ) : (
            <Table
              head={
                <>
                  <Th>
                    <div className="relative flex flex-row items-start justify-center">
                      <div className="flex h-5 items-center">
                        <Checkbox
                          checked={allChecked}
                          indeterminate={someChecked}
                          disabled={selectableIds.length === 0}
                          onCheckedChange={(v) => setSelected(v ? selectableIds : [])}
                        />
                      </div>
                    </div>
                  </Th>
                  <Th>{t('localMemories.col.content')}</Th>
                  <Th>{t('localMemories.col.layer')}</Th>
                  <Th>{t('localMemories.col.device')}</Th>
                  <Th>{t('localMemories.col.updatedAt')}</Th>
                  <Th>{t('localMemories.col.status')}</Th>
                  <Th>{t('localMemories.col.action')}</Th>
                </>
              }
            >
              {showTableLoading && <StateRow colSpan={colCount} state={state === 'error' ? 'error' : 'loading'} error={error} />}
              {!showTableLoading &&
                state === 'ready' &&
                rows.map((m) => (
                  <tr key={m.id} data-selectable="true" className="data-[selected=true]:bg-elevated/50">
                    <td data-pinned="false" className={TD}>
                      <div className="flex justify-center">
                        <Checkbox
                          checked={liveSelected.includes(m.id)}
                          disabled={!canStartPromote(m)}
                          title={promoteLockedLabel(m)}
                          onCheckedChange={(v) => setSelected((prev) => (v ? [...prev, m.id] : prev.filter((x) => x !== m.id)))}
                        />
                      </div>
                    </td>
                    <td data-pinned="false" className={TD}>
                      <div className="max-w-md">
                        <div className="truncate text-sm font-medium text-ink">{m.title || m.sourceId}</div>
                        {m.summary && <div className="mt-0.5 truncate text-xs text-ink-muted">{m.summary}</div>}
                      </div>
                    </td>
                    <td data-pinned="false" className={TD}>
                      <Badge variant="secondary">{layerLabel(m.layer)}</Badge>
                    </td>
                    <td data-pinned="false" className={TD}>
                      <span className="inline-flex items-center gap-1.5">
                        <span className="truncate">{deviceLabel(m)}</span>
                        {deviceMap.get(m.deviceId)?.status === 'revoked' && (
                          <Badge variant="destructive">{t('devices.status.revoked')}</Badge>
                        )}
                      </span>
                    </td>
                    <td data-pinned="false" className={TD}>
                      {formatDateTime(m.sourceUpdatedAt, lang)}
                    </td>
                    <td data-pinned="false" className={TD}>
                      <div className="max-w-64 space-y-1">
                        {promoteBadge(m)}
                        {/* 失败原因必须直接可见，不能只藏在悬停提示里（触屏没有悬停） */}
                        {promoteStateOf(m) === 'failed' && m.promoteError && (
                          <div className="whitespace-normal break-words text-[11px] leading-4 text-warning">{m.promoteError}</div>
                        )}
                      </div>
                    </td>
                    <td data-pinned="false" className={TD}>
                      <RowActions
                        actions={[
                          {
                            label: t('localMemories.action.detail'),
                            icon: 'i-lucide:eye',
                            onClick: () => void openDetail(m.id),
                          },
                          {
                            label: promoteLockedLabel(m) ?? promoteActionLabel(m),
                            icon: 'i-lucide:arrow-up-right',
                            disabled: !canStartPromote(m),
                            onClick: () => setPromoteIds([m.id]),
                          },
                          {
                            label: t('localMemories.action.delete'),
                            icon: 'i-lucide:trash-2',
                            // 数组先经过 filter（无上下文类型），字面量需收窄，否则 tone 会被放宽成 string
                            tone: 'danger' as const,
                            onClick: () => setPendingDelete([m.id]),
                          },
                        ].filter((action) => canWrite || action.label === t('localMemories.action.detail'))}
                      />
                    </td>
                  </tr>
                ))}
            </Table>
          )}
        </Panel>
        <Pager page={page} pageSize={PAGE_SIZE} total={total} onPage={setPage} />
      </div>

      <Drawer
        open={!!detail}
        title={t('localMemories.detailTitle')}
        onClose={() => setDetail(null)}
        footer={
          <>
            {canWrite && detail?.memory && canStartPromote(detail.memory) && (
              <Button variant="primary" onClick={() => setPromoteIds([detail.memory!.id])}>
                {promoteActionLabel(detail.memory)}
              </Button>
            )}
            {canWrite && detail?.memory && (
              <Button variant="destructive" onClick={() => setPendingDelete([detail.memory!.id])}>
                {t('localMemories.action.delete')}
              </Button>
            )}
            <Button variant="secondary" onClick={() => setDetail(null)}>
              {t('common.close')}
            </Button>
          </>
        }
      >
        {detail?.loading && <p className="text-sm text-ink-muted">{t('common.loading')}</p>}
        {detail?.error && <p className="text-sm text-warning">{detail.error}</p>}
        {detail?.memory && (
          <div className="space-y-5">
            <div className="rounded-xl border border-line bg-surface-muted/60 p-3 text-xs leading-5 text-ink-muted">
              {t('localMemories.detail.mirrorNote')}
            </div>

            <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-sm">
              <dt className="text-ink-muted">{t('localMemories.col.layer')}</dt>
              <dd className="text-ink-secondary">{layerLabel(detail.memory.layer)}</dd>
              <dt className="text-ink-muted">{t('localMemories.col.device')}</dt>
              <dd className="text-ink-secondary">{deviceLabel(detail.memory)}</dd>
              <dt className="text-ink-muted">{t('localMemories.detail.sourceId')}</dt>
              <dd className="break-all font-mono text-xs text-ink-secondary">{detail.memory.sourceId}</dd>
              <dt className="text-ink-muted">{t('localMemories.col.updatedAt')}</dt>
              <dd className="text-ink-secondary">{formatDateTime(detail.memory.sourceUpdatedAt, lang)}</dd>
              <dt className="text-ink-muted">{t('localMemories.detail.syncedAt')}</dt>
              <dd className="text-ink-secondary">{formatDateTime(detail.memory.syncedAt, lang)}</dd>
              {detail.memory.promotedMemoryId && (
                <>
                  <dt className="text-ink-muted">{t('localMemories.detail.promotedId')}</dt>
                  <dd className="break-all font-mono text-xs text-ink-secondary">{detail.memory.promotedMemoryId}</dd>
                </>
              )}
              {detail.memory.promoteStatus && (
                <>
                  <dt className="text-ink-muted">{t('localMemories.detail.promoteStatus')}</dt>
                  <dd className="break-all font-mono text-xs text-ink-secondary">{detail.memory.promoteStatus}</dd>
                </>
              )}
              {detail.memory.promoteError && (
                <>
                  <dt className="text-ink-muted">{t('localMemories.detail.promoteError')}</dt>
                  {/* 服务端原文不翻译：这是排障依据 */}
                  <dd className="break-words text-xs text-warning">{detail.memory.promoteError}</dd>
                </>
              )}
              {detail.memory.contentHash && (
                <>
                  <dt className="text-ink-muted">{t('localMemories.detail.contentHash')}</dt>
                  <dd className="break-all font-mono text-xs text-ink-secondary">{detail.memory.contentHash}</dd>
                </>
              )}
            </dl>

            {detail.memory.title && (
              <section>
                <h3 className="mb-1.5 text-xs font-semibold tracking-wide text-ink-muted">{t('localMemories.detail.title')}</h3>
                <p className="whitespace-pre-wrap break-words text-sm text-ink">{detail.memory.title}</p>
              </section>
            )}
            {detail.memory.summary && (
              <section>
                <h3 className="mb-1.5 text-xs font-semibold tracking-wide text-ink-muted">{t('localMemories.detail.summary')}</h3>
                <p className="whitespace-pre-wrap break-words text-sm text-ink-secondary">{detail.memory.summary}</p>
              </section>
            )}

            <section>
              <h3 className="mb-2 text-xs font-semibold tracking-wide text-ink-muted">{t('localMemories.detail.payload')}</h3>
              {detail.memory.payload === undefined || detail.memory.payload === null ? (
                <p className="text-sm text-ink-faint">{t('localMemories.detail.noPayload')}</p>
              ) : (
                <div className="rounded-xl border border-line bg-surface-muted/40 p-3 text-sm leading-6">
                  <PayloadView value={normalizePayload(detail.memory.payload)} />
                </div>
              )}
            </section>
          </div>
        )}
      </Drawer>

      <Dialog
        open={!!pendingDelete}
        title={t('localMemories.deleteTitle')}
        onClose={() => setPendingDelete(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => pendingDelete && void doDelete(pendingDelete)}>
              {t('common.confirm')}
            </Button>
          </>
        }
      >
        <p>{t('localMemories.deleteConfirm')(pendingDelete?.length ?? 0)}</p>
        <p className="mt-2 text-xs leading-5 text-ink-muted">{t('localMemories.deleteNote')}</p>
      </Dialog>

      <Dialog
        open={!!promoteIds}
        title={promotingRetry ? t('localMemories.action.retryPromote') : t('localMemories.promoteTitle')}
        onClose={() => !promoting && setPromoteIds(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPromoteIds(null)} disabled={promoting}>
              {t('common.cancel')}
            </Button>
            <Button variant="primary" onClick={() => void doPromote()} disabled={promoting}>
              {promoting ? t('common.loading') : promotingRetry ? t('localMemories.action.retryPromote') : t('common.confirm')}
            </Button>
          </>
        }
      >
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-sm">
          <dt className="text-ink-muted">{t('localMemories.promoteTarget')}</dt>
          <dd className="text-ink-secondary">{currentProjectName || currentProjectId}</dd>
          <dt className="text-ink-muted">{t('localMemories.promoteCount')}</dt>
          <dd className="text-ink-secondary">{promoteIds?.length ?? 0}</dd>
        </dl>
        <p className="mt-3 text-xs leading-5 text-ink-muted">{t('localMemories.promoteNote')}</p>
      </Dialog>

      <Dialog
        open={!!promoteResult}
        title={t('localMemories.promoteResultTitle')}
        onClose={() => setPromoteResult(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPromoteResult(null)}>
              {t('common.close')}
            </Button>
            {canWrite && promoteResult && promoteResult.failed.length > 0 && (
              <Button variant="primary" onClick={retryFailedPromotions}>
                {t('localMemories.action.retryFailed')(promoteResult.failed.length)}
              </Button>
            )}
          </>
        }
      >
        {promoteResult && (
          <div className="space-y-3">
            <p className="text-sm text-ink-secondary">{t('localMemories.promoteQueued')(promoteResult.queued.length)}</p>
            {promoteResult.failed.length > 0 && (
              <div>
                <p className="mb-2 text-xs font-medium text-warning">
                  {t('localMemories.promoteFailedTitle')(promoteResult.failed.length)}
                </p>
                <ul className="space-y-1.5">
                  {promoteResult.failed.map((f) => (
                    <li key={f.id} className="rounded-lg border border-line bg-surface-muted/50 px-3 py-2">
                      <div className="break-all font-mono text-[11px] text-ink-faint">{f.id}</div>
                      {/* 服务端原文不翻译：这是排障依据 */}
                      <div className="mt-0.5 break-words text-xs text-warning">{f.message || t('common.failed')}</div>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </Dialog>
    </>
  )
}
