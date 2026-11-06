import * as React from 'react'
import { cn } from '../../lib/utils'

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'relative overflow-hidden rounded-md bg-line/60 after:absolute after:inset-0 after:-translate-x-full after:animate-[shimmer_1.6s_infinite] after:bg-gradient-to-r after:from-transparent after:via-ink-faint/20 after:to-transparent',
        className,
      )}
      {...props}
    />
  )
}

/** 鱼骨图卡片骨架屏 (Fishbone Card Skeleton) */
export function CardSkeleton({ rows = 3, className = '' }: { rows?: number; className?: string }) {
  return (
    <div className={cn('p-5 rounded-xl border border-line bg-surface space-y-4', className)}>
      <div className="flex items-center gap-3">
        <Skeleton className="size-10 rounded-lg shrink-0" />
        <div className="space-y-1.5 flex-1 min-w-0">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-3 w-1/2" />
        </div>
      </div>
      <div className="space-y-2 pt-2 border-t border-line">
        {Array.from({ length: rows }).map((_, i) => (
          <Skeleton key={i} className="h-3 w-full" style={{ width: `${85 - i * 15}%` }} />
        ))}
      </div>
    </div>
  )
}

/** KPI 指标卡片骨架屏 (Fishbone KPI Skeleton) */
export function KpiSkeleton({ className = '' }: { className?: string }) {
  return (
    <div className={cn('min-w-[180px] flex-1 rounded-xl border border-line bg-surface p-5 shadow-xs space-y-3', className)}>
      <Skeleton className="h-3.5 w-24" />
      <Skeleton className="h-7 w-20" />
      <Skeleton className="h-3 w-16" />
    </div>
  )
}

/** 鱼骨图表格骨架屏 (Fishbone Table Skeleton) */
export function TableSkeleton({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="w-full divide-y divide-line">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-4 px-4 py-3.5">
          <Skeleton className="h-4 w-4 shrink-0 rounded" />
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton
              key={c}
              className="h-4"
              style={{
                width: c === 0 ? '20%' : c === 1 ? '30%' : c === cols - 1 ? '15%' : '25%',
              }}
            />
          ))}
        </div>
      ))}
    </div>
  )
}
