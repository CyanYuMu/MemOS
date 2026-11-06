/** 列表页筛选浮层。这一块用的是 Tailwind 原生色系，
 *  跟页面主体的语义 token 不是一套，别顺手统一。 */

import type { ReactNode } from 'react'
import { Icon } from './Icon'
import { useT } from '../lib/store'

/** 浮层本体。定位由外层的 relative 容器负责 */
export function FilterPopover({
  open,
  onFilter,
  onReset,
  children,
}: {
  open: boolean
  onFilter: () => void
  onReset: () => void
  children: ReactNode
}) {
  const t = useT()
  if (!open) return null
  return (
    <div className="absolute top-full right-0 mt-2 w-[380px] bg-surface rounded-xl shadow-xl border border-line p-4.5 z-50 popover-panel">
      {children}
      <div className="flex items-center justify-between pt-3 border-t border-line">
        <button
          type="button"
          className="text-xs font-medium text-ink-muted hover:text-ink transition-colors cursor-pointer px-1 py-1"
          onClick={onReset}
        >
          {t('filter.reset')}
        </button>
        <button
          type="button"
          className="rounded-lg bg-ink hover:bg-ink text-canvas text-xs font-medium px-3.5 py-1.5 transition-colors cursor-pointer shadow-2xs"
          onClick={onFilter}
        >
          {t('filter.apply')}
        </button>
      </div>
    </div>
  )
}

/** 一个字段：标签 + 控件 */
export function FilterField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mb-3.5">
      <label className="block text-xs font-medium text-ink-secondary mb-1.5">{label}</label>
      {children}
    </div>
  )
}

/** 两列排布的字段行 */
export function FilterFieldRow({ children }: { children: ReactNode }) {
  return <div className="mb-3.5 grid grid-cols-2 gap-3">{children}</div>
}

/** 下拉选择。交互用一层透明的原生 select 顶在触发器上 */
export function FilterSelect({
  value,
  onChange,
  options,
  placeholder,
}: {
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
  placeholder: string
}) {
  const current = options.find((o) => o.value === value)
  return (
    <div className="relative group rounded-lg border border-line bg-surface-muted hover:bg-surface-muted focus-within:bg-white focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100 h-8.5 transition-all">
      <div className="flex items-center justify-between h-full px-2.5 text-xs">
        <span className={`truncate pointer-events-none ${current ? 'text-ink font-medium' : 'text-ink-muted'}`}>
          {current ? current.label : placeholder}
        </span>
        <Icon name="i-lucide:chevron-down" className="shrink-0 text-ink-faint size-3.5" />
      </div>
      <select
        className="absolute inset-0 w-full h-full cursor-pointer opacity-0"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={placeholder}
      >
        <option value="">{placeholder}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  )
}

/** 文本输入 */
export function FilterInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <div className="relative inline-flex items-center w-full">
      <input
        className="w-full rounded-lg border border-line bg-surface-muted hover:bg-surface-muted focus:bg-surface text-ink placeholder:text-ink-muted text-xs px-2.5 py-1.5 transition-all outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 shadow-2xs h-8.5"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
      />
    </div>
  )
}

/** 日期区间。原生 datetime-local 不吃 placeholder，
 *  所以让它透明铺满，显示的文案由上面那层 span 负责。 */
export function FilterDateRange({ start, end, onChange }: { start: string; end: string; onChange: (start: string, end: string) => void }) {
  const t = useT()
  const fmt = (v: string) => v.replace('T', ' ')
  const slot = (value: string, placeholder: string, onPick: (v: string) => void) => (
    <div className="flex-1 min-w-0 text-center relative py-1">
      <span className={`text-xs leading-none truncate block ${value ? 'text-ink font-medium' : 'text-ink-muted'}`}>
        {value ? fmt(value) : placeholder}
      </span>
      <input
        type="datetime-local"
        value={value}
        onChange={(e) => onPick(e.target.value)}
        className="absolute inset-0 w-full h-full cursor-pointer opacity-0"
        aria-label={placeholder}
      />
    </div>
  )
  return (
    <div className="w-full rounded-lg border border-line bg-surface-muted hover:bg-surface-muted focus-within:bg-white focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100 h-8.5 flex items-center px-2.5 gap-2 transition-all shadow-2xs">
      <Icon name="i-lucide:calendar" className="size-3.5 text-ink-faint shrink-0" />
      {slot(start, t('filter.startTime'), (v) => onChange(v, end))}
      <span className="text-ink-muted text-xs leading-none shrink-0 font-mono">{t('filter.to')}</span>
      {slot(end, t('filter.endTime'), (v) => onChange(start, v))}
    </div>
  )
}
