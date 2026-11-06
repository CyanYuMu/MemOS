import * as Popover from '@radix-ui/react-popover'
import { useState, useRef } from 'react'
import { useT } from '../lib/store'

/** 样式都挂在作用域属性下，类名不带它就不生效 */
const Q = { 'data-v-de9e37d2': '' } as const

/** 表格的三种单元格，同一套边框 + 内边距，只有首列和表头加粗、不换行 */
const CELL = 'border border-line px-2.5 py-1.5 align-top'
const CELL_HEAD = 'border border-line bg-surface-muted px-2.5 py-1.5 font-medium text-ink whitespace-nowrap'
const CELL_NAME = `${CELL} font-medium text-ink whitespace-nowrap`

const CLOSE_DELAY = 240

export type HintRow = [name: string, scene: string, feature: string]

export function AccessMethodHint({ text, rows }: { text: string; rows: HintRow[] }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  const show = () => {
    if (timer.current) window.clearTimeout(timer.current)
    setOpen(true)
  }
  const hide = () => {
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setOpen(false), CLOSE_DELAY)
  }

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <span className="relative inline-flex" onMouseEnter={show} onMouseLeave={hide}>
        <Popover.Trigger asChild>
          <button
            type="button"
            {...Q}
            className="access-method-hint cursor-help outline-none bg-transparent border-0 p-0 text-xs text-ink-muted hover:text-ink transition-colors"
            aria-expanded={open}
            data-state={open ? 'open' : 'closed'}
          >
            {text}
          </button>
        </Popover.Trigger>
      </span>

      <Popover.Portal>
        <Popover.Content
          side="bottom"
          align="end"
          sideOffset={4}
          collisionPadding={16}
          onMouseEnter={show}
          onMouseLeave={hide}
          onOpenAutoFocus={(e) => e.preventDefault()}
          onCloseAutoFocus={(e) => e.preventDefault()}
          className="z-50 w-[min(560px,calc(100vw-2rem))] max-w-[560px] p-3.5 bg-surface text-ink-secondary rounded-xl border border-line shadow-xl focus:outline-none pointer-events-auto"
        >
          <div className="text-[12px] leading-[1.6]">
            <table className="access-method-table w-full border-collapse text-left">
              <thead>
                <tr>
                  <th className={CELL_HEAD}>{t('quickstart.hint.col.method')}</th>
                  <th className={CELL_HEAD}>{t('quickstart.hint.col.scene')}</th>
                  <th className={CELL_HEAD}>{t('quickstart.hint.col.feature')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([name, scene, feature]) => (
                  <tr key={name}>
                    <td className={CELL_NAME}>{name}</td>
                    <td className={CELL}>{scene}</td>
                    <td className={CELL}>{feature}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {/* 这里原来有一行「若你正在使用 OpenClaw，推荐通过 Plugin + CLI 的形式以获取最佳体验」。
                2026-10-19 删掉：它推荐的那一半（Plugin）我们没提供，而删掉 Plugin 之后
                剩下的那半（OpenClaw + CLI）我也无法证实——`AGENTS.md` 第八节：
                写一句做不到的承诺比不写更糟。真要恢复，先确认 OpenClaw 走 CLI 确实能通。 */}
          </div>
          <Popover.Arrow className="fill-surface" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
