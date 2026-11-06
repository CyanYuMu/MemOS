import * as React from 'react'
import { Icon } from '../Icon'
import { cn } from '../../lib/utils'

export interface CheckboxProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  checked: boolean
  indeterminate?: boolean
  onCheckedChange?: (checked: boolean) => void
}

export function Checkbox({ checked, indeterminate, onCheckedChange, className, disabled, ...props }: CheckboxProps) {
  const isIndeterminate = indeterminate && !checked
  const isChecked = checked

  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={isIndeterminate ? 'mixed' : isChecked}
      disabled={disabled}
      onClick={() => onCheckedChange?.(!checked)}
      className={cn(
        'size-4 shrink-0 rounded border transition-colors flex items-center justify-center cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500/30 disabled:cursor-not-allowed disabled:opacity-50',
        isChecked || isIndeterminate ? 'bg-accent border-accent text-on-accent' : 'bg-surface border-line-strong hover:border-line-strong',
        className,
      )}
      {...props}
    >
      {isChecked && <Icon name="i-lucide:check" className="size-3 text-on-accent stroke-[2.5]" />}
      {isIndeterminate && <Icon name="i-lucide:minus" className="size-3 text-on-accent stroke-[2.5]" />}
    </button>
  )
}
