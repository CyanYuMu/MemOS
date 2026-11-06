import { useEffect, useRef, useState } from 'react'
import { Icon } from '../Icon'
import { copyText } from '../../lib/clipboard'
import { useT, useToast } from '../../lib/store'
import { cn } from '../../lib/utils'

/** 复制按钮。
 *
 *  复制成功后图标变勾并保持 1.4 秒——只靠顶部 toast 的话，用户视线在代码块上，
 *  往往还没抬头提示就已经走了。失败才走 toast：那是要用户处理的事。
 *
 *  `className` 只用于定位；外观由 `.copy-btn` 统一（常显半透明，悬停变实）。
 *  不要再用「hover 才出现」的写法：代码块里唯一的操作被藏起来，第一次用的人找不到。 */
export function CopyButton({ text, label, className }: { text: string; label?: string; className?: string }) {
  const t = useT()
  const toast = useToast()
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  return (
    <button
      type="button"
      aria-label={label ?? t('common.copy')}
      title={label ?? t('common.copy')}
      className={cn('copy-btn', className)}
      onClick={async () => {
        try {
          await copyText(text)
          setCopied(true)
          if (timer.current) clearTimeout(timer.current)
          timer.current = setTimeout(() => setCopied(false), 1400)
        } catch {
          toast(t('common.copyFailed'), 'err')
        }
      }}
    >
      <Icon name={copied ? 'i-lucide:check' : 'i-lucide:copy'} className="shrink-0 size-3.5" />
    </button>
  )
}
