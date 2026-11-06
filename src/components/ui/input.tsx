import * as React from 'react'
import { cn } from '../../lib/utils'

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  error?: boolean
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(({ className, type, error, ...props }, ref) => {
  return (
    <input
      type={type}
      className={cn(
        'flex h-9 w-full rounded-lg border bg-surface px-3 py-1.5 text-sm text-ink shadow-xs transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-ink-muted focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50',
        error
          ? 'border-danger/50 focus-visible:border-rose-500 focus-visible:ring-2 focus-visible:ring-rose-500/20'
          : 'border-line focus-visible:border-indigo-500 focus-visible:ring-2 focus-visible:ring-indigo-500/20 hover:border-line-strong',
        className,
      )}
      ref={ref}
      {...props}
    />
  )
})
Input.displayName = 'Input'
