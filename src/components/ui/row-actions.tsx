import { Icon } from '../Icon'
import { Tooltip } from './tooltip'
import { cn } from '../../lib/utils'

export interface RowAction {
  /** tooltip 与无障碍名称都用它 */
  label: string
  icon: string
  onClick: () => void
  tone?: 'default' | 'danger'
  disabled?: boolean
}

/** 表格行内的操作按钮。
 *
 *  原来各页写各的：接口密钥是蓝字「复制密钥」+ 红字「删除」，模型列表是蓝字
 *  「复制模型名称」，记忆列表又直接写了两个裸 `<button>`。三处字号、配色、间距都不同。
 *
 *  现在统一成 **ghost 图标按钮**：默认低对比度，悬停才出现底色；删除类只在悬停时变红，
 *  避免每一行都挂一个红字把表格拉花。图标必须有 tooltip——只给图标不给说明，
 *  等于让业务猜。 */
export function RowActions({ actions, className }: { actions: RowAction[]; className?: string }) {
  return (
    <div className={cn('flex items-center gap-0.5', className)}>
      {actions.map((a) => (
        <Tooltip key={a.label} label={a.label}>
          <button
            type="button"
            aria-label={a.label}
            disabled={a.disabled}
            onClick={a.onClick}
            className={cn(
              'inline-flex size-7 cursor-pointer items-center justify-center rounded-md text-ink-muted transition-all outline-none active:scale-95',
              'hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-accent/40 disabled:pointer-events-none disabled:opacity-40',
              a.tone === 'danger' ? 'hover:text-danger' : 'hover:text-ink',
            )}
          >
            <Icon name={a.icon} className="size-4" />
          </button>
        </Tooltip>
      ))}
    </div>
  )
}
