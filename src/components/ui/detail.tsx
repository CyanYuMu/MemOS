import type { ReactNode } from 'react'
import { Button } from './button'
import { Skeleton } from './skeleton'
import { useT } from '../../lib/store'
import { cn } from '../../lib/utils'

export interface DetailItem {
  label: string
  value: ReactNode
  /** 等宽字体。ID、traceId 这类要逐字比对的值用它 */
  mono?: boolean
}

/** 详情弹窗里的键值区。
 *
 *  记忆详情和请求详情原来各写一套 grid，左边宽度、行距、等宽规则都不一样。
 *  这里统一成「标签列按内容宽、值列吃掉剩余宽度」，长文本才会折行而不是把弹窗撑爆。 */
export function DetailFields({ items, className }: { items: DetailItem[]; className?: string }) {
  return (
    <dl className={cn('grid grid-cols-[max-content_1fr] gap-x-5 gap-y-3 text-sm', className)}>
      {items.map((it) => (
        <div key={it.label} className="contents">
          <dt className="pt-px text-ink-muted">{it.label}</dt>
          <dd className={cn('min-w-0 text-ink-secondary break-words whitespace-pre-wrap', it.mono && 'font-mono text-xs break-all')}>
            {it.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

/** 详情内容到达前的占位。
 *
 *  以前这里是一行「加载中…」：弹窗先以一行文字的高度打开，数据回来后内容把它撑大，
 *  看起来像关掉一个弹窗又开了另一个。骨架屏把高度提前占住，弹窗只出现一次。
 *
 *  行数按内容量给，不要写死——请求详情字段多，记忆详情正文长。 */
export function DetailSkeleton({ rows = 5, className }: { rows?: number; className?: string }) {
  const t = useT()
  return (
    <div className={cn('detail-skeleton space-y-3', className)} aria-busy="true" aria-label={t('common.loading')}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="grid grid-cols-[72px_1fr] items-center gap-x-5">
          <Skeleton className="h-3.5 w-12" />
          <Skeleton className="h-3.5" style={{ width: `${88 - (i % 3) * 14}%` }} />
        </div>
      ))}
    </div>
  )
}

/** 详情取数失败。必须给重试——关掉弹窗重新点一遍是最烦的操作。 */
export function DetailError({ text, onRetry }: { text: string; onRetry?: () => void }) {
  const t = useT()
  return (
    <div className="flex flex-col items-start gap-3" role="alert">
      <p className="text-sm text-warning">{text}</p>
      {onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          {t('common.retry')}
        </Button>
      )}
    </div>
  )
}
