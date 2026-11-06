import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import type { ReactNode } from 'react'

/** Radix Tooltip 需要祖先 Provider。放在 AppShell 根部，一处即可。 */
export const TooltipProvider = TooltipPrimitive.Provider

/** 悬停说明。用于只有图标的按钮——图标必须能说清自己是什么。 */
export function Tooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <TooltipPrimitive.Root delayDuration={320}>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={6}
          className="motion-pop z-[60] rounded-md bg-ink px-2 py-1 text-[11px] font-medium leading-4 text-canvas shadow-md"
        >
          {label}
          <TooltipPrimitive.Arrow className="fill-ink" width={9} height={5} />
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  )
}
