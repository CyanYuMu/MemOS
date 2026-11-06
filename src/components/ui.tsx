import { useEffect, useRef, type ReactNode } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { Icon } from './Icon'
import { EmptyState } from './ui/empty-state'
import { Skeleton } from './ui/skeleton'
import { Button } from './ui/button'
import { useT } from '../lib/store'

/** 标签的展示色。成功 / 失败两种，另留一档中性色 */
export type Tone = 'ok' | 'warn' | 'default'

/** 表格所在页面的作用域属性，表格样式挂在这个属性下 */
export const T = { 'data-v-13b6c0ea': '' } as const

export const TH = 'px-4 py-3.5 text-sm text-left rtl:text-right [&:has([role=checkbox])]:pe-0 text-second-gray font-normal'

/** 表格 td 基类，默认不换行；需要折行的列自己追加 break-all */
export const TD = 'p-4 text-sm whitespace-nowrap [&:has([role=checkbox])]:pe-0 py-3 text-second-gray leading-5'

/** 允许折行的 td（长文本列用）。两个类在 CSS 里的先后决定胜负 */
export const TD_BREAK = `${TD} whitespace-break-spaces break-all`

export function PageHeader({
  title,
  desc,
  actions,
  className = 'mb-4',
}: {
  title: string
  desc?: string
  actions?: ReactNode
  className?: string
}) {
  return (
    // ⚠️ 右侧必须 `shrink-0`：左边是「标题 + 一段可能很长的说明」，不加的话 flex 会去压
    // 右边那一栏，操作按钮被挤出可视区（用量监控页的三个区间键就是这么被截掉的）。
    // 左边配 `min-w-0` + `truncate`，让它自己先收缩。
    <div className={`console-page-header flex justify-between items-center gap-4 min-h-9.5 ${className}`}>
      <div className="flex items-center gap-2 min-w-0">
        <div className="console-page-heading flex min-w-0 items-end gap-2">
          <h2 className="text-lg text-ink font-semibold leading-6.25 shrink-0">{title}</h2>
          {desc && <p className="text-sm text-ink-secondary leading-5.5 truncate">{desc}</p>}
        </div>
      </div>
      {actions && (
        <div className="console-page-actions shrink-0">
          <div className="console-page-controls flex items-center gap-3">{actions}</div>
        </div>
      )}
    </div>
  )
}

/** 表格外框。只有「调用日志」页加高度上限，其余页不限高 */
export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div {...T} className={`console-panel relative overflow-auto border border-line rounded-xl bg-surface shadow-2xs ${className}`}>
      {children}
    </div>
  )
}

export function Table({ head, children }: { head: ReactNode; children: ReactNode }) {
  return (
    <table className="console-table min-w-full overflow-clip text-left">
      <thead className="sticky top-0 inset-x-0 z-[1] bg-surface-muted/90 backdrop-blur-sm border-b border-line text-xs font-medium text-ink-muted">
        <tr>{head}</tr>
      </thead>
      <tbody className="motion-rows divide-y divide-line [&>tr]:transition-colors [&>tr]:data-[selectable=true]:hover:bg-surface-hover/70">
        {children}
      </tbody>
    </table>
  )
}

export function Th({ children }: { children: ReactNode }) {
  return (
    <th data-pinned="false" scope="col" className={TH}>
      {children}
    </th>
  )
}

/** 功能还没开放的页面，统一的占位面板。
 *
 *  ⚠️ 为什么要有这个组件：画像模板与知识库原来各写了一份，**长得不一样**——
 *  一个带 🚧、一个不带，间距和字号也不同。2026-10-19 用户的原话是
 *  「建设中的文案，样式，你可以统一一点呀。复用组件会不会？」。
 *  两份手写的空态还有个更实际的问题：改文案要改两处，漏一处就又不一样了。
 *
 *  ⚠️ 不带 🚧 / ✨ 这类装饰字符：`AGENTS.md` 第二节明确写了不用它们撑气氛。
 *  知识库原来那版是带 🚧 的，统一时去掉了。
 *
 *  ⚠️ `flex-1` 让它吃掉 PageHeader 之外的全部高度；外层**不要**加 `max-w-[…]`，
 *  那会把页面卡成一条窄栏（2026-10-19 用户报过的布局 bug）。 */
export function ComingSoon() {
  const t = useT()
  return (
    <Panel className="flex-1 min-h-[420px] flex flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      <div className="size-14 rounded-2xl bg-surface-hover/80 border border-line flex items-center justify-center text-ink-faint mb-1 shadow-2xs">
        <Icon name="i-lucide:compass" className="size-6 text-ink-faint" />
      </div>
      <h3 className="text-base font-semibold text-ink tracking-tight">{t('common.comingSoon')}</h3>
      <p className="max-w-[460px] text-xs leading-relaxed text-ink-muted">{t('common.comingSoonDesc')}</p>
    </Panel>
  )
}

/** 加载 / 空 / 出错三种状态的整行占位 */
export function StateRow({
  colSpan,
  state,
  error,
  onRetry,
  emptyTitle,
  emptyAction,
}: {
  colSpan: number
  state: 'loading' | 'ready' | 'error'
  error?: string
  /** 失败态的重试。列表加载失败原来只能整页刷新，等于把筛选条件和页码一起丢掉 */
  onRetry?: () => void
  /** 空列表时换一句话。搜索无结果和「这个项目本来就没数据」是两件事 */
  emptyTitle?: string
  emptyAction?: ReactNode
}) {
  const t = useT()
  if (state === 'ready') {
    return (
      <tr>
        <td colSpan={colSpan}>
          <EmptyState compact title={emptyTitle ?? t('common.noData')} action={emptyAction} />
        </td>
      </tr>
    )
  }
  if (state === 'loading') {
    return (
      <>
        {[1, 2, 3, 4, 5].map((idx) => (
          <tr key={idx} className="border-b border-line">
            <td colSpan={colSpan} className="px-4 py-3.5">
              <div className="flex items-center gap-4">
                <Skeleton className="size-4 shrink-0" />
                <Skeleton className="h-4 w-28" />
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 flex-1" />
                <Skeleton className="h-4 w-16 shrink-0" />
              </div>
            </td>
          </tr>
        ))}
      </>
    )
  }
  // `error` 已经是一句完整的话（见 useAsync/errText），不要再在前面拼一次「请求失败：」
  const text = error || t('common.failed')
  return (
    <tr>
      <td colSpan={colSpan}>
        {/* 失败态用 warning 色图标 + 正文色文字：错误原文要能读清，而不是一行灰字 */}
        <EmptyState
          compact
          icon="i-lucide:alert-circle"
          title={text}
          action={
            onRetry ? (
              <Button variant="secondary" onClick={onRetry}>
                {t('common.retry')}
              </Button>
            ) : undefined
          }
        />
      </td>
    </tr>
  )
}

/** 分页。
 *
 *  ⚠️ 别再退回「只渲染当前页那一个数字」的写法：业务看不出总共有多少页、也看不出能不能
 *  往后翻，只能靠试探着点箭头（2026-10-19 用户报的）。现在给出两样东西：
 *    - 左侧一句 `共 N 条 · 第 X / Y 页`，一眼看到规模和位置；
 *    - 右侧窗口式页码：首页、末页、当前页 ±1 一定显示，其余折叠成 `…`，
 *      这样 200 页也不会把一行撑爆。
 *
 *  首/末/上/下一页仍然保留，`aria-label` 不变——验收脚本按它找按钮。 */
export function Pager({ page, pageSize, total, onPage }: { page: number; pageSize: number; total: number; onPage: (p: number) => void }) {
  const t = useT()
  const pages = Math.max(1, Math.ceil(total / pageSize))
  const btn = (label: string, icon: string, target: number, disabled: boolean) => (
    <button
      type="button"
      disabled={disabled}
      aria-label={label}
      className="p-1 rounded-md border border-line bg-surface-muted hover:bg-surface-hover text-ink-secondary disabled:opacity-40 disabled:pointer-events-none transition-colors cursor-pointer shadow-2xs"
      onClick={() => onPage(target)}
    >
      <Icon name={icon} className="shrink-0 size-4 text-ink-muted" />
    </button>
  )

  // 窗口式页码：首末页 + 当前页 ±1，中间的连续区间折叠成一个 `…`
  const items: (number | '…')[] = []
  for (let p = 1; p <= pages; p++) {
    if (p === 1 || p === pages || Math.abs(p - page) <= 1) items.push(p)
    else if (items[items.length - 1] !== '…') items.push('…')
  }

  return (
    <div {...T} className="flex flex-wrap justify-between items-center gap-3 pt-4">
      <div {...T} className="text-xs text-ink-muted">
        {t('common.total')(total)}
        <span className="mx-2 text-ink-faint">·</span>
        {t('common.pageOf')(page, pages)}
      </div>
      <nav {...T} className="flex items-center gap-1">
        {btn(t('common.firstPage'), 'i-lucide:chevrons-left', 1, page <= 1)}
        {btn(t('common.prevPage'), 'i-lucide:chevron-left', page - 1, page <= 1)}
        {items.map((it, i) =>
          it === '…' ? (
            <span key={`gap-${i}`} className="px-1.5 text-xs text-ink-faint select-none" aria-hidden="true">
              …
            </span>
          ) : (
            <button
              key={it}
              type="button"
              data-type="page"
              aria-current={it === page ? 'page' : undefined}
              data-selected={it === page ? 'true' : undefined}
              aria-label={t('common.pageOf')(it, pages)}
              className={`min-w-7 h-7 px-2 rounded-md text-xs font-medium transition-colors cursor-pointer ${
                it === page
                  ? 'bg-accent text-on-accent shadow-2xs'
                  : 'text-ink-secondary hover:bg-surface-hover border border-line bg-surface-muted/60'
              }`}
              onClick={() => onPage(it)}
            >
              <span>{it}</span>
            </button>
          ),
        )}
        {btn(t('common.nextPage'), 'i-lucide:chevron-right', page + 1, page >= pages)}
        {btn(t('common.lastPage'), 'i-lucide:chevrons-right', pages, page >= pages)}
      </nav>
    </div>
  )
}

/** 对话框。采用 Radix UI Dialog 原语，自带 Portal、Focus Trap 与 Escape 键无障碍支持
 *
 *  `size` 决定最大宽度：确认类弹窗（md，默认）一句话就够；详情类弹窗内容长，
 *  用 lg 才不用每行都折。宽度上限用的是编译产物里已有的 `max-w-*`，别换成任意值类。 */
export function Dialog({
  open,
  title,
  onClose,
  children,
  footer,
  size = 'md',
  className,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg'
  /** 追加在内容容器上。宽高受限的对话框（diff、素材预览）用它放大，别改 `size` 的默认值 */
  className?: string
}) {
  const t = useT()
  const width = size === 'lg' ? 'max-w-2xl' : size === 'sm' ? 'max-w-md' : 'max-w-lg'
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="motion-overlay fixed inset-0 z-50 bg-black/35 backdrop-blur-[3px]" />
        {/* ⚠️ 居中由 Tailwind v4 的 `translate` **属性**负责，不是 `transform`。
            所以关键帧里只写 scale / translateY，不能再写 `translate(-50%,-50%)`——
            v4 把位移拆成了独立属性，两处同时写会叠加成双倍平移。
            原来的 `scale-in` 则相反：只写 `transform: scale()`，把位移覆盖掉了。
            见 console-extra.css 的 `dialog-in` / `dialog-out`。 */}
        <DialogPrimitive.Content
          className={`motion-dialog fixed z-50 bg-surface border border-line flex flex-col focus:outline-none top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[calc(100vw-2rem)] ${width} max-h-[calc(100dvh-2rem)] sm:max-h-[calc(100dvh-4rem)] shadow-2xl overflow-hidden rounded-2xl ${className ?? ''}`}
        >
          <div className="flex items-center justify-between px-6 py-4.5 min-h-16 border-b border-line">
            <DialogPrimitive.Title className="text-base font-semibold text-ink leading-6">{title}</DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <button
                type="button"
                aria-label={t('common.close')}
                className="p-1 rounded-lg text-ink-faint hover:text-ink-secondary hover:bg-surface-hover transition-colors cursor-pointer"
              >
                <Icon name="i-lucide:x" className="shrink-0 size-4.5" />
              </button>
            </DialogPrimitive.Close>
          </div>
          <div className="flex-1 overflow-y-auto px-6 py-5 text-sm text-ink-secondary">{children}</div>
          {footer && (
            <div className="flex items-center justify-end px-6 py-3.5 gap-2.5 border-t border-line bg-surface-muted/60">{footer}</div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

export function PrimaryButton({
  children,
  onClick,
  disabled,
  icon,
}: {
  children: ReactNode
  onClick?: () => void
  disabled?: boolean
  icon?: string
}) {
  return (
    <button
      type="button"
      className="inline-flex items-center justify-center gap-1.5 h-8.5 px-3.5 rounded-lg text-xs font-medium bg-accent text-on-accent shadow-xs hover:bg-accent-hover disabled:opacity-50 disabled:pointer-events-none transition-all cursor-pointer active:scale-[0.98]"
      onClick={onClick}
      disabled={disabled}
    >
      {icon && <Icon name={icon} className="shrink-0 size-4" />}
      {children}
    </button>
  )
}

export function TextLinkButton({ children, onClick, disabled }: { children: ReactNode; onClick?: () => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      className="text-xs font-medium text-accent hover:text-accent-hover disabled:text-ink-faint disabled:pointer-events-none transition-colors cursor-pointer"
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  )
}

export function SearchBox({
  value,
  onChange,
  onSubmit,
  onFilter,
  onFilterClose,
  filterOpen = false,
  filter,
  placeholder,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
  onFilter?: () => void
  /** 点浮层外面或按 Esc 时收起。
   *
   *  原来只能再点一次漏斗按钮：浮层挡住半张表，用户点旁边的行想关掉它，
   *  结果点了两次才明白要先点那个小图标。 */
  onFilterClose?: () => void
  filterOpen?: boolean
  filter?: ReactNode
  /** 各页的提示语不一样，由调用方传入 */
  placeholder?: string
}) {
  const t = useT()
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!filterOpen || !onFilterClose) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) onFilterClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onFilterClose()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [filterOpen, onFilterClose])

  return (
    <div ref={wrapRef} className="console-search flex items-center gap-2">
      <div className="console-search-input relative inline-flex items-center w-64">
        <Icon name="i-lucide:search" className="absolute left-3 size-4 text-ink-faint pointer-events-none" />
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && onSubmit()}
          placeholder={placeholder ?? t('common.searchPlaceholder')}
          className="w-full rounded-lg border border-line bg-surface-muted hover:bg-surface-muted focus:bg-surface text-ink placeholder:text-ink-muted text-xs pl-9 pr-3 py-1.5 transition-all outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 shadow-2xs"
        />
      </div>
      {onFilter && (
        <div className="relative">
          <button
            type="button"
            aria-label={t('filter.apply')}
            title={t('filter.apply')}
            aria-expanded={filterOpen}
            className={`p-1.5 rounded-lg border border-line bg-surface-muted hover:bg-surface-hover text-ink-secondary transition-colors cursor-pointer shadow-2xs ${
              filterOpen ? 'bg-accent-soft border-accent/30 text-accent' : ''
            }`}
            onClick={onFilter}
          >
            <Icon name="i-db-filter-line" className="shrink-0 size-4" />
          </button>
          {filterOpen && filter}
        </div>
      )}
    </div>
  )
}

export function Tag({ text, tone = 'default' }: { text: string; tone?: Tone }) {
  const cls =
    tone === 'ok'
      ? 'bg-success-soft text-success ring-1 ring-inset ring-success/25'
      : tone === 'warn'
        ? 'bg-warning-soft text-warning ring-1 ring-inset ring-warning/25'
        : 'bg-surface-hover text-ink-secondary ring-1 ring-inset ring-line-strong/40'
  return <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium tracking-tight ${cls}`}>{text}</span>
}
