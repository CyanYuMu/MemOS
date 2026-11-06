/** 用量监控。
 *
 *  数据来自后端从调用审计聚合出的 `/api/dashboard/usage/query`。
 *
 *  ⚠️ 页面上**只呈现真的有的东西**。上游日志实测只记 `addMessage` / `searchMemory` 的
 *  调用次数；`chat` / `extractMemory` / `rerank` 的对象虽然在响应里，但里面**只有恒为 0
 *  的 token 字段、没有任何次数**（三个租户 14 天全量核对过）。所以：
 *    - 不摆「Token 消耗」卡片和列 —— 永远是 0，看着像页面坏了；
 *    - 那三个接口**一条都不计入**。原实现把「对象存在」当成「当天有 1 次调用」，
 *      结果是每天平白多出 3 次调用、总计虚高——这是编出来的数，不是日志里的数。
 *  底部一行口径说明把这件事讲明白，而不是让业务自己去猜为什么没有 Token。
 *
 *  ⚠️ 接口要求 `projectId` 必须是注册表里的真实项目：传空串或 `all` 都会返回
 *  `未知项目`。所以没选项目时**不发请求**，直接给「请先选择项目」的引导态。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type UsageDailyStat } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { useProject, useT } from '../lib/store'
import { Panel, PageHeader, StateRow, Table, TD, Th } from '../components/ui'
import { EmptyState } from '../components/ui/empty-state'
import { KpiSkeleton, Skeleton } from '../components/ui/skeleton'
import { Icon } from '../components/Icon'
import { BTN_PAGER } from '../lib/cls'

const RANGES = [7, 14, 30] as const
type RangeDays = (typeof RANGES)[number]

/** 堆叠顺序 = 数组顺序（从下往上）。颜色沿用控制台主色 + 记忆类型里已有的琥珀。 */
const SERIES = [
  { key: 'write', label: 'usage.add', color: '#4044ed' },
  { key: 'read', label: 'usage.search', color: '#f5a742' },
] as const

interface DayPoint {
  date: string
  write: number
  read: number
  total: number
}

/** 只有这两个接口带次数（`calls`）。其余接口在响应里只有 token 字段，不计。 */
const toPoint = (d: UsageDailyStat): DayPoint => {
  const write = d.addMessage?.calls ?? 0
  const read = d.searchMemory?.calls ?? 0
  return { date: d.date, write, read, total: write + read }
}

// 后端按北京时间切天，展示口径也用 UTC+8；不能用 toISOString 的 UTC 日期，
// 否则北京时间 0–8 点之间会落到前一天。
const ymd = (d: Date) => new Date(d.getTime() + 8 * 3600 * 1000).toISOString().slice(0, 10)

/** 纵轴刻度：把最大值抬到一个「整」的刻度上（步长只取 1/2/2.5/5/10 × 10ⁿ）。
 *  直接除以 4 会得到 149.75 这种刻度，读不出来。 */
function niceScale(max: number, ticks = 4) {
  const raw = Math.max(1, max) / ticks
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag
  return { step, max: step * ticks }
}

const compact = (v: number) => (v >= 10000 ? `${Math.round(v / 1000)}k` : v.toLocaleString())

/* ------------------------------------------------------------------ 趋势图 */

const CHART_H = 240
const PAD = { l: 48, r: 14, t: 14, b: 28 }

function TrendChart({ days }: { days: DayPoint[] }) {
  const t = useT()
  const boxRef = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(880)
  const [hover, setHover] = useState<number | null>(null)

  // 宽度用 ResizeObserver 实测：SVG 用 viewBox 缩放会把字号一起拉大，宽屏下刻度字会变形
  useEffect(() => {
    const el = boxRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setW(el.clientWidth))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const n = days.length
  const plotW = Math.max(160, w - PAD.l - PAD.r)
  const plotH = CHART_H - PAD.t - PAD.b
  const slot = plotW / Math.max(n, 1)
  const barW = Math.max(4, Math.min(34, slot * 0.56))
  const { step, max: yMax } = niceScale(Math.max(1, ...days.map((d) => d.total)))
  const yOf = (v: number) => PAD.t + plotH - (v / yMax) * plotH
  const xOf = (i: number) => PAD.l + slot * (i + 0.5)

  // x 轴最多放 9 个日期，且必须包含最后一天（否则看不出「今天」在哪）
  const labelEvery = Math.max(1, Math.ceil(n / 9))
  const tickCount = Math.round(yMax / step)

  const hovered = hover !== null ? days[hover] : null
  // 浮层贴着悬停柱的顶端，并夹在画布内，免得贴边时被裁掉
  const tipW = 168
  const tipLeft = hover !== null ? Math.min(Math.max(xOf(hover) - tipW / 2, 4), Math.max(4, w - tipW - 4)) : 0

  return (
    <div ref={boxRef} className="relative w-full" style={{ height: CHART_H }}>
      <svg width={w} height={CHART_H} role="img" aria-label={t('usage.trend.title')}>
        {/* 网格与纵轴刻度 */}
        {Array.from({ length: tickCount + 1 }, (_, k) => {
          const v = step * k
          const y = yOf(v)
          return (
            <g key={k}>
              <line x1={PAD.l} x2={PAD.l + plotW} y1={y} y2={y} stroke={k === 0 ? '#CBD5E1' : '#EEF2F7'} strokeWidth={1} />
              <text x={PAD.l - 8} y={y} textAnchor="end" dominantBaseline="middle" fontSize={11} fill="#94a3b8">
                {compact(Math.round(v))}
              </text>
            </g>
          )
        })}

        {/* 悬停列底纹：比逐柱 hover 更好点中，也不用给每个柱子挂事件 */}
        {hover !== null && <rect x={xOf(hover) - slot / 2} y={PAD.t} width={slot} height={plotH} fill="#4044ed" opacity={0.06} />}

        {/* 堆叠柱 */}
        {days.map((d, i) => {
          const cx = xOf(i)
          let acc = 0
          const segs = SERIES.map((s) => ({ k: s.key, v: d[s.key], color: s.color }))
            .filter((s) => s.v > 0)
            .map((s) => {
              const y0 = yOf(acc)
              acc += s.v
              const y1 = yOf(acc)
              return { k: s.k, color: s.color, y: y1, h: Math.max(1, y0 - y1) }
            })
          return (
            <g key={d.date}>
              {segs.map((s) => (
                <rect key={s.k} x={cx - barW / 2} y={s.y} width={barW} height={s.h} fill={s.color} rx={1} />
              ))}
              {/* 当天没有调用时画一根 2px 的浅色残根：空着会让人以为是渲染漏了 */}
              {d.total === 0 && <rect x={cx - barW / 2} y={yOf(0) - 2} width={barW} height={2} fill="#E2E8F0" rx={1} />}
              {(i % labelEvery === 0 || i === n - 1) && (
                <text x={cx} y={CHART_H - 9} textAnchor="middle" fontSize={11} fill="#94a3b8">
                  {d.date.slice(5)}
                </text>
              )}
            </g>
          )
        })}

        {/* 命中区放最后：盖住柱子才收得到事件 */}
        {days.map((d, i) => (
          <rect
            key={`hit-${d.date}`}
            x={xOf(i) - slot / 2}
            y={PAD.t}
            width={slot}
            height={plotH}
            fill="transparent"
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover((v) => (v === i ? null : v))}
          />
        ))}
      </svg>

      {hovered && (
        <div
          className="pointer-events-none absolute z-10 rounded-[8px] border border-line bg-surface px-3 py-2 text-[12px] leading-5 shadow-2xl"
          style={{ left: tipLeft, top: 6, width: tipW }}
        >
          <div className="mb-1 font-medium text-ink">{hovered.date}</div>
          {SERIES.map((s) => (
            <div key={s.key} className="flex items-center gap-1.5 text-ink-muted">
              <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: s.color }} />
              <span className="flex-1 truncate">{t(s.label)}</span>
              <span className="tabular-nums text-ink">{hovered[s.key].toLocaleString()}</span>
            </div>
          ))}
          <div className="mt-1 flex items-center justify-between border-t border-line pt-1 text-ink-muted">
            <span>{t('usage.col.total')}</span>
            <span className="tabular-nums font-medium text-ink">{hovered.total.toLocaleString()}</span>
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ 页面 */

function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="min-w-[180px] flex-1 rounded-xl border border-line bg-surface p-5 shadow-xs transition-shadow hover:shadow-sm">
      <div className="text-xs font-medium text-ink-muted">{label}</div>
      <div className="mt-2 text-2xl font-bold tracking-tight tabular-nums text-ink">{value}</div>
      {hint && <div className="mt-1 text-xs text-ink-muted">{hint}</div>}
    </div>
  )
}

export function Usage() {
  const t = useT()
  const { currentProjectId, currentProjectName } = useProject()
  const [range, setRange] = useState<RangeDays>(14)
  const [tick, setTick] = useState(0)

  const days = useMemo(() => {
    const end = new Date()
    return { startDate: ymd(new Date(end.getTime() - (range - 1) * 86400000)), endDate: ymd(end) }
  }, [range])

  const { data, state, error } = useAsync(
    // 没有项目就不发请求：接口对空 projectId 只会回「未知项目」，那不是业务该看到的错
    () => (currentProjectId ? api.usage.query({ projectId: currentProjectId, ...days }) : Promise.resolve({ dailyStats: [] })),
    [currentProjectId, days, tick],
  )

  const stats = useMemo(() => (data?.dailyStats ?? []).map(toPoint), [data])
  const totals = useMemo(() => {
    const sum = { write: 0, read: 0, total: 0 }
    for (const d of stats) {
      sum.write += d.write
      sum.read += d.read
      sum.total += d.total
    }
    return sum
  }, [stats])

  const activeDays = stats.filter((d) => d.total > 0).length
  const peak = stats.reduce<DayPoint | null>((best, d) => (d.total > (best?.total ?? 0) ? d : best), null)
  const share = (v: number) => (totals.total > 0 ? `${Math.round((v / totals.total) * 100)}%` : '—')

  // 区间切换键。不用 BTN_PAGER 再覆盖选中态：那样要压 base.css 里的 hover:bg-elevated，
  // 靠加载顺序取胜太脆；这里只用已确认存在的原子类自己拼。
  const rangeBtn = (r: RangeDays) =>
    `px-3 py-1.5 text-xs font-medium rounded-md transition-all cursor-pointer ${
      r === range ? 'bg-accent text-on-accent shadow-xs' : 'text-ink-secondary hover:text-ink hover:bg-surface-hover'
    }`

  return (
    <>
      <PageHeader
        title={t('usage.title')}
        desc={currentProjectName ? `${t('usage.desc')} · ${currentProjectName}` : t('usage.desc')}
        actions={
          <div className="flex items-center gap-2.5">
            <div className="flex items-center gap-1 p-0.5 rounded-lg border border-line bg-surface-hover/80">
              {RANGES.map((r) => (
                <button key={r} type="button" className={rangeBtn(r)} onClick={() => setRange(r)} aria-pressed={r === range}>
                  <span className="text-xs">{t(`usage.range.${r}` as 'usage.range.7')}</span>
                </button>
              ))}
            </div>
            <button type="button" className={BTN_PAGER} onClick={() => setTick((x) => x + 1)} title={t('common.refresh')}>
              <Icon name="i-lucide:rotate-ccw" className="size-4" />
            </button>
          </div>
        }
      />

      <div className="flex flex-col gap-4">
        {!currentProjectId ? (
          <Panel className="px-5">
            <EmptyState icon="i-lucide:compass" title={t('usage.pickProject')} />
          </Panel>
        ) : (
          <>
            <div className="flex flex-wrap gap-3">
              {state === 'loading' ? (
                <>
                  <KpiSkeleton />
                  <KpiSkeleton />
                  <KpiSkeleton />
                  <KpiSkeleton />
                </>
              ) : (
                <>
                  <Kpi label={t('usage.totalApiRequests')} value={totals.total.toLocaleString()} hint={t('usage.kpi.rangeHint')(range)} />
                  <Kpi label={t('usage.add')} value={totals.write.toLocaleString()} hint={t('usage.kpi.shareHint')(share(totals.write))} />
                  <Kpi label={t('usage.search')} value={totals.read.toLocaleString()} hint={t('usage.kpi.shareHint')(share(totals.read))} />
                  <Kpi
                    label={t('usage.kpi.dailyAvg')}
                    value={Math.round(totals.total / range).toLocaleString()}
                    hint={t('usage.kpi.activeDays')(activeDays, range)}
                  />
                </>
              )}
            </div>

            <Panel className="p-5">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-sm font-semibold text-ink">{t('usage.trend.title')}</div>
                  <div className="mt-0.5 text-xs text-ink-muted">{t('usage.trend.desc')}</div>
                </div>
                <div className="flex items-center gap-3">
                  {SERIES.map((s) => (
                    <span key={s.key} className="flex items-center gap-1.5 text-xs text-ink-muted">
                      <span className="size-2 rounded-full" style={{ backgroundColor: s.color }} />
                      {t(s.label)}
                      <span className="tabular-nums text-ink">{totals[s.key].toLocaleString()}</span>
                    </span>
                  ))}
                </div>
              </div>

              {state === 'loading' ? (
                <div className="h-[240px] flex items-end gap-3 px-12 pb-6 pt-10">
                  {[40, 65, 30, 85, 45, 90, 60, 75, 50, 80, 40, 70, 55, 95].map((h, i) => (
                    <div key={i} className="flex-1 flex flex-col justify-end h-full">
                      <Skeleton className="w-full rounded-t-md" style={{ height: `${h}%` }} />
                    </div>
                  ))}
                </div>
              ) : state === 'error' ? (
                <EmptyState compact icon="i-lucide:alert-circle" title={error || t('common.failed')} />
              ) : totals.total === 0 ? (
                <EmptyState compact icon="i-lucide:inbox" title={t('usage.noData')} />
              ) : (
                <TrendChart days={stats} />
              )}

              {state === 'ready' && (
                <div className="mt-3 border-t border-line pt-2 text-xs leading-5 text-ink-muted">
                  {t('usage.scope')}
                  {peak && peak.total > 0 ? ` ${t('usage.peak')(peak.date, peak.total.toLocaleString())}` : ''}
                </div>
              )}
            </Panel>

            <div className="flex flex-wrap gap-4">
              <Panel className="min-w-[420px] flex-1 overflow-hidden">
                <div className="border-b border-line px-5 py-3 text-sm font-semibold text-ink">{t('usage.table.title')}</div>
                <Table
                  head={
                    <>
                      <Th>{t('usage.col.date')}</Th>
                      <Th>{t('usage.add')}</Th>
                      <Th>{t('usage.search')}</Th>
                      <Th>{t('usage.col.total')}</Th>
                      <Th>{t('usage.col.share')}</Th>
                    </>
                  }
                >
                  {state !== 'ready' && <StateRow colSpan={5} state={state} error={error} />}
                  {state === 'ready' && stats.length === 0 && <StateRow colSpan={5} state="ready" />}
                  {state === 'ready' &&
                    [...stats].reverse().map((d) => (
                      <tr key={d.date} className={d.total === 0 ? 'text-ink-muted' : undefined}>
                        <td data-pinned="false" className={TD}>
                          {d.date}
                        </td>
                        <td data-pinned="false" className={`${TD} tabular-nums`}>
                          {d.write.toLocaleString()}
                        </td>
                        <td data-pinned="false" className={`${TD} tabular-nums`}>
                          {d.read.toLocaleString()}
                        </td>
                        <td data-pinned="false" className={`${TD} tabular-nums font-medium text-ink`}>
                          {d.total.toLocaleString()}
                        </td>
                        <td data-pinned="false" className={`${TD} tabular-nums`}>
                          {totals.total > 0 ? `${((d.total / totals.total) * 100).toFixed(1)}%` : '—'}
                        </td>
                      </tr>
                    ))}
                </Table>
              </Panel>

              <Panel className="min-w-[280px] flex-1">
                <div className="border-b border-line px-5 py-3 text-sm font-semibold text-ink">{t('usage.breakdown.title')}</div>
                <div className="flex flex-col gap-4 px-5 py-4">
                  {SERIES.map((s) => {
                    const pct = totals.total > 0 ? (totals[s.key] / totals.total) * 100 : 0
                    return (
                      <div key={s.key}>
                        <div className="mb-1.5 flex items-center justify-between text-sm">
                          <span className="flex items-center gap-2 text-ink-secondary">
                            <span className="size-2 rounded-full" style={{ backgroundColor: s.color }} />
                            {t(s.label)}
                          </span>
                          <span className="tabular-nums text-ink">
                            {totals[s.key].toLocaleString()}
                            <span className="ml-2 text-xs text-ink-muted">{pct.toFixed(1)}%</span>
                          </span>
                        </div>
                        <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-hover">
                          <div
                            className="h-full rounded-full"
                            style={{ width: `${Math.round(pct * 100) / 100}%`, backgroundColor: s.color }}
                          />
                        </div>
                      </div>
                    )
                  })}
                  {totals.total === 0 && <EmptyState compact icon="i-lucide:inbox" title={t('usage.noData')} />}
                </div>
              </Panel>
            </div>
          </>
        )}
      </div>
    </>
  )
}
