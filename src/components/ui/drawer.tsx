import type { ReactNode } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { Icon } from '../Icon'

/** 右侧详情抽屉。
 *
 *  复用 Radix Dialog 原语（与 `Dialog` 同一个依赖）：遮罩、焦点陷阱、Escape 关闭
 *  都由它提供，视觉语言和弹窗保持一致。宽度用标准类，不引入新的 UI 库。
 *
 *  ⚠️ 进场关键帧写在 `console-extra.css`（`drawer-in` / `drawer-out`）。 */
export function Drawer({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/35 backdrop-blur-[3px] data-[state=open]:animate-[dialog-overlay-in_180ms_ease-out] data-[state=closed]:animate-[dialog-overlay-out_140ms_ease-in]" />
        <DialogPrimitive.Content className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col overflow-hidden border-l border-line bg-surface shadow-2xl focus:outline-none data-[state=open]:animate-[drawer-in_200ms_cubic-bezier(0.16,1,0.3,1)] data-[state=closed]:animate-[drawer-out_150ms_ease-in]">
          <div className="flex min-h-16 shrink-0 items-center justify-between border-b border-line px-6 py-4.5">
            <DialogPrimitive.Title className="text-base font-semibold leading-6 text-ink">{title}</DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <button
                type="button"
                aria-label="Close"
                className="cursor-pointer rounded-lg p-1 text-ink-faint transition-colors hover:bg-surface-hover hover:text-ink-secondary"
              >
                <Icon name="i-lucide:x" className="size-4.5 shrink-0" />
              </button>
            </DialogPrimitive.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5 text-sm text-ink-secondary">{children}</div>
          {footer && (
            <div className="flex shrink-0 items-center justify-end gap-2.5 border-t border-line bg-surface-muted/60 px-6 py-3.5">
              {footer}
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
