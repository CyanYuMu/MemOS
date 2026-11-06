import { HighlightedCode } from '../components/HighlightedCode'
import { useState } from 'react'
import { api, EVENT_TYPES, type LogDetail, type RequestLog } from '../lib/api'
import { ListPage } from '../components/ListPage'
import { Dialog, Tag } from '../components/ui'
import { DetailError, DetailSkeleton } from '../components/ui/detail'
import { Tabs } from '../components/ui/tabs'
import { RowActions } from '../components/ui/row-actions'
import { FilterDateRange, FilterField, FilterFieldRow, FilterInput, FilterPopover, FilterSelect } from '../components/FilterPopover'
import { FILTER_LABEL } from '../lib/cls'
import { useLang, useProject, useT } from '../lib/store'
import { errText } from '../lib/i18n'

/** extractMemoryDirect 展示成 extractMemory */
const displayEvent = (e: string) => (e === 'extractMemoryDirect' ? 'extractMemory' : e)

/** 约定：SUCCESS / success 都算成功 */
const isOk = (s: string) => s === 'SUCCESS' || s === 'success'

/** 调用来源。插件与脚本走同一批 REST 端点，靠客户端自报的标识区分 */
const displaySource = (s?: string) => (s === 'mcp' ? 'MCP' : s === 'plugin' ? 'Plugin' : s === 'rest' ? 'REST' : s ? s : '—')

const joinIds = (ids?: string[]) => (ids && ids.length ? ids.join(', ') : '-')

/** 把后端下发的 JSON 字符串解析出来；解析不了就原样展示 */
function pretty(raw?: string) {
  if (!raw) return '-'
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

interface LogFilters {
  eventType: string
  source: string
  subjectType: string
  subjectId: string
  startTime: string
  endTime: string
}

const NO_FILTER: LogFilters = {
  eventType: '',
  source: '',
  subjectType: '',
  subjectId: '',
  startTime: '',
  endTime: '',
}

export function Requests() {
  const t = useT()
  const { lang } = useLang()
  const { currentProjectId, ready } = useProject()
  const [filters, setFilters] = useState<LogFilters>(NO_FILTER)
  const [draft, setDraft] = useState<LogFilters>(NO_FILTER)
  const [detail, setDetail] = useState<{ id: string; loading: boolean; error: string; data: LogDetail | null } | null>(null)
  const [tab, setTab] = useState<'request' | 'response'>('request')

  const patch = (p: Partial<LogFilters>) => setDraft((d) => ({ ...d, ...p }))

  const openDetail = async (id: string) => {
    setDetail({ id, loading: true, error: '', data: null })
    try {
      setDetail({ id, loading: false, error: '', data: await api.log.detail(id) })
    } catch (e) {
      // 取详情失败就明确报错。原来会退化成「用列表行 + 空正文」渲染，
      // 看起来像这次调用本来就没有请求体，排障时会往错误的方向查。
      // 错误只放在弹窗里（带重试），不再额外弹 toast——同一个错误说两遍，
      // 而且 toast 会在用户读到之前就消失。
      setDetail({ id, loading: false, error: errText(lang, e), data: null })
    }
  }

  return (
    <div className="platform-guide-page" data-guide="requests">
      <ListPage<RequestLog>
        title={t('requests.title')}
        desc={t('requests.desc')}
        searchable
        searchPlaceholder={t('requests.searchPlaceholder')}
        rowKey={(r) => r.id}
        deps={[currentProjectId, filters]}
        skip={!ready}
        onRowClick={(r) => openDetail(r.id)}
        filter={(close) => (
          <FilterPopover
            open
            onFilter={() => {
              setFilters(draft)
              close()
            }}
            onReset={() => {
              setDraft(NO_FILTER)
              setFilters(NO_FILTER)
            }}
          >
            <FilterField label={t('filter.createdAt')}>
              <FilterDateRange
                start={draft.startTime}
                end={draft.endTime}
                onChange={(startTime, endTime) => patch({ startTime, endTime })}
              />
            </FilterField>

            <FilterFieldRow>
              <div>
                <label className={FILTER_LABEL}>{t('filter.subjectType')}</label>
                <FilterSelect
                  value={draft.subjectType}
                  onChange={(subjectType) => patch({ subjectType })}
                  placeholder={t('filter.all')}
                  options={[
                    { value: 'user', label: t('filter.subjectUser') },
                    { value: 'agent', label: t('filter.subjectAgent') },
                  ]}
                />
              </div>
              <div>
                <label className={FILTER_LABEL}>{t('filter.subjectId')}</label>
                <FilterInput
                  value={draft.subjectId}
                  onChange={(subjectId) => patch({ subjectId })}
                  placeholder={t('filter.subjectIdPlaceholder')}
                />
              </div>
            </FilterFieldRow>

            <FilterFieldRow>
              <div>
                <label className={FILTER_LABEL}>{t('filter.eventType')}</label>
                <FilterSelect
                  value={draft.eventType}
                  onChange={(eventType) => patch({ eventType })}
                  placeholder={t('filter.selectEventType')}
                  options={EVENT_TYPES.map((x) => ({ value: x, label: x }))}
                />
              </div>
              <div>
                <label className={FILTER_LABEL}>{t('filter.source')}</label>
                <FilterSelect
                  value={draft.source}
                  onChange={(source) => patch({ source })}
                  placeholder={t('filter.all')}
                  options={[
                    { value: 'mcp', label: t('source.mcp') },
                    { value: 'plugin', label: t('source.plugin') },
                    { value: 'rest', label: t('source.rest') },
                  ]}
                />
              </div>
            </FilterFieldRow>
          </FilterPopover>
        )}
        load={async (pageNum, pageSize, query) => {
          const res = await api.log.list({
            projectId: currentProjectId,
            pageNum,
            pageSize,
            query,
            // 这个页面只展示记忆服务的调用；控制台的 dashboard/admin 审计不在这里出现。
            scope: 'gateway',
            eventType: filters.eventType,
            source: filters.source,
            subjectType: filters.subjectType,
            subjectId: filters.subjectId,
            startTime: filters.startTime,
            endTime: filters.endTime,
          })
          return { items: res.logs, total: res.total }
        }}
        cols={[
          { key: 'createTime', label: t('requests.col.createdAt'), render: (r) => r.createTime },
          {
            key: 'eventType',
            label: t('requests.col.eventType'),
            render: (r) => <div className="font-mono text-sm">{displayEvent(r.eventType)}</div>,
          },
          {
            key: 'source',
            label: t('requests.col.source'),
            render: (r) => <div className="font-mono text-sm">{displaySource(r.source)}</div>,
          },
          { key: 'userIds', label: t('requests.col.userId'), render: (r) => joinIds(r.userIds) },
          { key: 'agentIds', label: t('requests.col.agentId'), render: (r) => joinIds(r.agentIds) },
          {
            key: 'status',
            label: t('requests.col.status'),
            render: (r) => <Tag text={r.status} tone={isOk(r.status) ? 'ok' : 'warn'} />,
          },
          {
            key: 'duration',
            label: t('requests.col.latency'),
            // 异步 add 还没跑完时后端给 null（真实耗时还没产生）。这里留白，
            // 不写 '-'、更不把入队耗时当真实延时显示。
            render: (r) => (r.duration != null ? `${r.duration}ms` : ''),
          },
          {
            key: 'action',
            label: t('requests.col.action'),
            render: (r) => (
              <RowActions actions={[{ label: t('requests.viewDetails'), icon: 'i-lucide:eye', onClick: () => openDetail(r.id) }]} />
            ),
          },
        ]}
      />

      <Dialog open={!!detail} size="lg" title={t('requests.detail.title')} onClose={() => setDetail(null)}>
        {detail?.loading && <DetailSkeleton rows={6} />}
        {detail?.error && <DetailError text={detail.error} onRetry={() => openDetail(detail.id)} />}
        {detail?.data && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              {(
                [
                  [t('requests.detail.traceId'), detail.data.traceId],
                  [t('requests.detail.callId'), detail.data.callId],
                  [t('requests.detail.requestId'), detail.data.requestId],
                  [t('requests.col.status'), detail.data.status],
                  [t('requests.col.source'), displaySource(detail.data.source)],
                  [t('requests.col.latency'), detail.data.duration != null ? `${detail.data.duration}ms` : ''],
                  [t('requests.col.createdAt'), detail.data.createTime],
                  [t('requests.col.userId'), detail.data.userId],
                  [t('requests.col.agentId'), detail.data.agentId],
                  [t('requests.detail.requestTokens'), detail.data.requestTokens],
                  [t('requests.detail.responseTokens'), detail.data.responseTokens],
                  [t('requests.detail.llmCalls'), detail.data.llmCalls],
                ] as const
              ).map(([label, value]) => (
                <div key={label} className="flex gap-2">
                  <span className="w-[120px] shrink-0 text-ink-muted">{label}</span>
                  <span className="min-w-0 break-all text-ink-secondary">{value ?? '-'}</span>
                </div>
              ))}
            </div>

            <Tabs
              label={t('requests.detail.title')}
              value={tab}
              onChange={setTab}
              items={[
                { key: 'request', label: t('requests.detail.request') },
                { key: 'response', label: t('requests.detail.response') },
              ]}
            />

            <pre className="max-h-[320px] overflow-auto rounded-lg border border-line bg-surface-muted p-3 font-mono text-xs text-ink whitespace-pre-wrap break-all">
              <HighlightedCode language="json" text={pretty(tab === 'request' ? detail.data.requestBody : detail.data.responseBody)} />
            </pre>
          </div>
        )}
      </Dialog>
    </div>
  )
}
