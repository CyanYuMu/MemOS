import { memo } from 'react'
import { CopyButton } from '../ui/copy-button'
import { useLang, useT } from '../../lib/store'
import { cn } from '../../lib/utils'
import { relativityPercent, type MemoryEntry } from './memory'

/** 一条检索到的记忆。正文是重点，相关性与时间、id 都用弱化的次级样式。 */
export const MemoryCard = memo(function MemoryCard({
  entry,
  onSelect,
  caption,
  className,
}: {
  entry: MemoryEntry
  /** 传入时整张卡片可点击，用于汇总面板里定位到工具调用 */
  onSelect?: () => void
  /** 汇总面板里标出来自哪次查询 */
  caption?: string
  className?: string
}) {
  const t = useT()
  const { lang } = useLang()
  const percent = relativityPercent(entry.relativity)
  const time = timeLabel(entry.createTime, lang, t)

  return (
    <div
      role={onSelect ? 'button' : undefined}
      tabIndex={onSelect ? 0 : undefined}
      onClick={onSelect}
      onKeyDown={
        onSelect
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault()
                onSelect()
              }
            }
          : undefined
      }
      className={cn(
        'chat-memory-card rounded-lg border border-line bg-surface px-2.5 py-2 text-xs',
        onSelect && 'cursor-pointer transition-colors hover:border-accent/40 hover:bg-surface-hover/60',
        className,
      )}
      data-memory-id={entry.memoryId || undefined}
    >
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 whitespace-pre-wrap break-words leading-5 text-ink-secondary">{entry.value || entry.key || '-'}</p>
        {percent !== undefined && (
          <div className="flex shrink-0 flex-col items-end gap-1 pt-0.5">
            <span className="font-mono text-[11px] text-ink-muted">{percent}%</span>
            <span className="h-1 w-12 overflow-hidden rounded-full bg-surface-hover">
              <span className="block h-full rounded-full bg-accent" style={{ width: `${percent}%` }} />
            </span>
          </div>
        )}
      </div>
      <div className="mt-1.5 flex min-w-0 items-center gap-2 text-[11px] text-ink-faint">
        {time && <span className="shrink-0">{time}</span>}
        {entry.source === 'dream' && (
          <span className="shrink-0 rounded bg-accent-soft px-1 py-0.5 text-[10px] text-accent">{t('chat.memory.dream')}</span>
        )}
        {entry.memoryId && <span className="min-w-0 flex-1 truncate font-mono">{entry.memoryId}</span>}
        {entry.memoryId && (
          <span className="shrink-0" onClick={(event) => event.stopPropagation()}>
            <CopyButton text={entry.memoryId} label={t('chat.memory.copyId')} />
          </span>
        )}
      </div>
      {caption && <div className="mt-1 truncate text-[11px] text-ink-faint">{caption}</div>}
    </div>
  )
})

/** 相对时间；超过一周就退回短日期，避免“37 天前”这种没有信息量的说法 */
function timeLabel(value: string | undefined, lang: 'zh' | 'en', t: ReturnType<typeof useT>): string {
  if (!value) return ''
  const ts = Date.parse(value)
  if (!Number.isFinite(ts)) return value
  const diff = Date.now() - ts
  if (diff < 0) return new Intl.DateTimeFormat(lang === 'zh' ? 'zh-CN' : 'en-US', { month: 'numeric', day: 'numeric' }).format(ts)
  if (diff < 60_000) return t('chat.memory.justNow')
  if (diff < 3_600_000) return t('chat.memory.minutesAgo')(Math.floor(diff / 60_000))
  if (diff < 86_400_000) return t('chat.memory.hoursAgo')(Math.floor(diff / 3_600_000))
  if (diff < 7 * 86_400_000) return t('chat.memory.daysAgo')(Math.floor(diff / 86_400_000))
  return new Intl.DateTimeFormat(lang === 'zh' ? 'zh-CN' : 'en-US', { month: 'numeric', day: 'numeric' }).format(ts)
}
