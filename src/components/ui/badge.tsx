import * as React from 'react'
import { cn } from '../../lib/utils'

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline'
}

export function Badge({ className, variant = 'default', ...props }: BadgeProps) {
  const variants = {
    default: 'bg-accent-soft text-accent ring-1 ring-inset ring-accent/25',
    secondary: 'bg-surface-hover text-ink-secondary ring-1 ring-inset ring-line-strong/40',
    success: 'bg-success-soft text-success ring-1 ring-inset ring-success/25',
    warning: 'bg-warning-soft text-warning ring-1 ring-inset ring-warning/25',
    destructive: 'bg-danger-soft text-danger ring-1 ring-inset ring-danger/25',
    outline: 'border border-line text-ink-secondary',
  }

  return (
    <span
      className={cn(
        'inline-flex items-center gap-x-1.5 rounded-md px-2 py-0.5 text-xs font-medium tracking-tight',
        variants[variant],
        className,
      )}
      {...props}
    />
  )
}
