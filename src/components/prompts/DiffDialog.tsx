/** 差异对话框：任选两侧内容比较，可切换并排 / 上下。
 *
 *  内容来源由调用方给（草稿、当前生效、内置默认、历史版本），这里只负责切换与渲染。
 */

import { useState } from 'react'
import { Dialog } from '../ui'
import { Tabs } from '../ui/tabs'
import { useT } from '../../lib/store'
import { DiffView } from './DiffView'

export interface DiffSource {
  id: string
  label: string
  text: string
}

export function DiffDialog({
  open,
  title,
  sources,
  leftId,
  rightId,
  initialMode = 'split',
  onClose,
}: {
  open: boolean
  title: string
  sources: DiffSource[]
  leftId: string
  rightId: string
  initialMode?: 'split' | 'unified'
  onClose: () => void
}) {
  const t = useT()
  // 每次打开都会重新挂载（父组件条件渲染），这里直接用调用方指定的两侧
  const [left, setLeft] = useState(leftId)
  const [right, setRight] = useState(rightId)
  const [mode, setMode] = useState<'split' | 'unified'>(initialMode)

  const leftSource = sources.find((s) => s.id === left) ?? sources[0]
  const rightSource = sources.find((s) => s.id === right) ?? sources[1] ?? sources[0]

  const picker = (value: string, onChange: (v: string) => void, label: string) => (
    <label className="flex items-center gap-2 text-xs text-ink-muted">
      <span className="shrink-0">{label}</span>
      <select className="studio-select" value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        {sources.map((s) => (
          <option key={s.id} value={s.id}>
            {s.label}
          </option>
        ))}
      </select>
    </label>
  )

  return (
    <Dialog open={open} title={title} onClose={onClose} size="lg" className="studio-modal-diff">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-3">
            {picker(left, setLeft, t('prompts.diff.left'))}
            {picker(right, setRight, t('prompts.diff.right'))}
          </div>
          <Tabs
            items={[
              { key: 'split', label: t('prompts.diff.mode.split') },
              { key: 'unified', label: t('prompts.diff.mode.unified') },
            ]}
            value={mode}
            onChange={setMode}
            label={t('prompts.diff.title')}
          />
        </div>
        <p className="text-[11px] leading-5 text-ink-faint">{t('prompts.diff.dir')(leftSource?.label ?? '', rightSource?.label ?? '')}</p>
        <DiffView
          leftText={leftSource?.text ?? ''}
          rightText={rightSource?.text ?? ''}
          leftLabel={leftSource?.label ?? ''}
          rightLabel={rightSource?.label ?? ''}
          mode={mode}
          className="max-h-[62vh]"
        />
      </div>
    </Dialog>
  )
}
