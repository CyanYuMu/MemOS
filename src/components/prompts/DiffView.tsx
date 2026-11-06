/** 文本差异视图：并排 / 上下两种排布，未改动的大段可以折叠。
 *
 *  差异本身来自 `src/lib/diff.ts`（行级 LCS + 行内字符级高亮），这里只负责渲染。
 *  颜色用 `.studio-diff-*` 系列类，写在 console-extra.css 里——暗色主题下靠语义色变量翻转。
 */

import { Fragment, useState } from 'react'
import { diffLines, groupBlocks, type DiffRow, type DiffSegment } from '../../lib/diff'
import { useT } from '../../lib/store'
import { cn } from '../../lib/utils'

function Segments({ segments, side }: { segments?: DiffSegment[]; side: 'left' | 'right' }) {
  if (!segments) return null
  const own = side === 'left' ? 'del' : 'add'
  return (
    <>
      {segments.map((s, i) => {
        if (s.kind === 'equal') return <Fragment key={i}>{s.text}</Fragment>
        if (s.kind !== own) return null
        return (
          <span key={i} className="studio-diff-mark">
            {s.text}
          </span>
        )
      })}
    </>
  )
}

function LineNo({ n }: { n: number | null }) {
  return <span className="studio-diff-ln">{n ?? ''}</span>
}

function SplitCell({ row, side }: { row: DiffRow; side: 'left' | 'right' }) {
  const isChange = side === 'left' ? row.kind === 'del' : row.kind === 'add'
  // 一侧是变化行时，另一侧留空，避免同一行内容在两边重复显示
  const empty = side === 'left' ? row.kind === 'add' : row.kind === 'del'
  return (
    <div className={cn('studio-diff-cell', isChange && (side === 'left' ? 'studio-diff-del' : 'studio-diff-add'))}>
      <LineNo n={side === 'left' ? row.aLine : row.bLine} />
      {empty ? null : (
        <span className="studio-diff-text">
          {isChange && row.segments ? <Segments segments={row.segments} side={side} /> : row.text || ' '}
        </span>
      )}
    </div>
  )
}

function UnifiedRow({ row }: { row: DiffRow }) {
  const sign = row.kind === 'add' ? '+' : row.kind === 'del' ? '−' : ' '
  return (
    <div className={cn('studio-diff-row', row.kind === 'add' && 'studio-diff-add', row.kind === 'del' && 'studio-diff-del')}>
      <LineNo n={row.aLine} />
      <LineNo n={row.bLine} />
      <span className="studio-diff-sign">{sign}</span>
      <span className="studio-diff-text">
        {row.kind !== 'equal' && row.segments ? (
          <Segments segments={row.segments} side={row.kind === 'del' ? 'left' : 'right'} />
        ) : (
          row.text || ' '
        )}
      </span>
    </div>
  )
}

export function DiffView({
  leftText,
  rightText,
  leftLabel,
  rightLabel,
  mode,
  className,
}: {
  leftText: string
  rightText: string
  leftLabel: string
  rightLabel: string
  mode: 'split' | 'unified'
  className?: string
}) {
  const t = useT()
  // 折叠状态绑在这份 diff 的内容上：换了一组文本，旧的「展开」不会作用到新的块号上
  const signature = `${leftText.length}:${rightText.length}:${leftText.slice(0, 32)}`
  const [expandedState, setExpandedState] = useState<{ signature: string; blocks: number[] }>({ signature, blocks: [] })
  const expanded = expandedState.signature === signature ? expandedState.blocks : []
  const expand = (bi: number) => setExpandedState({ signature, blocks: [...expanded, bi] })

  const rows = diffLines(leftText, rightText)
  const blocks = groupBlocks(rows, 3)
  const same = rows.every((r) => r.kind === 'equal')

  if (same) {
    return (
      <div className={cn('studio-diff studio-diff--same', className)}>
        <p className="text-xs text-ink-muted">{t('prompts.diff.same')}</p>
      </div>
    )
  }

  return (
    <div className={cn('studio-diff', mode === 'split' ? 'studio-diff--split' : 'studio-diff--unified', className)}>
      {mode === 'split' && (
        <>
          <div className="studio-diff-head">{leftLabel}</div>
          <div className="studio-diff-head">{rightLabel}</div>
        </>
      )}
      {blocks.map((block, bi) => {
        if (block.kind === 'fold' && !expanded.includes(bi)) {
          return (
            <button key={`fold-${bi}`} type="button" className="studio-diff-fold" onClick={() => expand(bi)}>
              {t('prompts.diff.fold')(block.count)}
            </button>
          )
        }
        const runRows = block.rows
        if (mode === 'split') {
          return (
            <Fragment key={`block-${bi}`}>
              {runRows.map((row) => (
                <Fragment key={`${row.aLine}-${row.bLine}-${row.kind}`}>
                  <SplitCell row={row} side="left" />
                  <SplitCell row={row} side="right" />
                </Fragment>
              ))}
            </Fragment>
          )
        }
        return (
          <Fragment key={`block-${bi}`}>
            {runRows.map((row) => (
              <UnifiedRow key={`${row.aLine}-${row.bLine}-${row.kind}`} row={row} />
            ))}
          </Fragment>
        )
      })}
    </div>
  )
}
