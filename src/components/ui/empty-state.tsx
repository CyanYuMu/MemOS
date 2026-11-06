import type { ReactNode } from 'react'
import { Icon } from '../Icon'
import { cn } from '../../lib/utils'

/** 空态。
 *
 *  原来两套写法：表格里是一行灰字「暂无数据」，用量监控是「这段时间没有调用记录」+
 *  一大片空白。现在统一成「图标 + 一句主文案 + 可选说明/操作」，
 *  `compact` 用于嵌在表格单元格里，否则撑满所在面板。
 */
export function EmptyState({
  icon = 'i-lucide:inbox',
  title,
  desc,
  action,
  compact = false,
  className,
}: {
  icon?: string
  title: string
  desc?: string
  action?: ReactNode
  /** 表格里用：垂直留白收窄，图标小一号 */
  compact?: boolean
  className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center text-center', compact ? 'gap-2 py-10' : 'gap-3 px-6 py-16', className)}>
      <div
        className={cn(
          'flex items-center justify-center rounded-2xl border border-line bg-surface-muted text-ink-faint',
          compact ? 'size-10' : 'size-12',
        )}
      >
        <Icon name={icon} className={compact ? 'size-4.5' : 'size-5'} />
      </div>
      <div className={cn('font-medium text-ink', compact ? 'text-xs' : 'text-sm')}>{title}</div>
      {desc && <p className="max-w-[440px] text-xs leading-5 text-ink-muted">{desc}</p>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}
