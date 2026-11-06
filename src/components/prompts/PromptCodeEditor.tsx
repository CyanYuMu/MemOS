/** 提示词正文编辑器：等宽字体 + 占位符高亮。
 *
 *  做法是「透明文字的 textarea 叠在带高亮的 pre 上」：textarea 负责输入、光标和选择，
 *  pre 负责显示颜色。两者必须共用同一套字体、行高与内边距（见 console-extra.css 的
 *  `.studio-code`），否则光标会和文字错位。滚动位置由 textarea 同步给 pre。
 */

import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { cn } from '../../lib/utils'

/** `dollar` 认 `${name}`；`brace` 与 `format` 都认 `{name}`（format 的字面花括号同理） */
function tokenPattern(syntax: string): RegExp {
  return syntax === 'dollar' ? /\$\{[A-Za-z0-9_]*\}/g : /\{\{|\}\}|\{[^{}]*\}/g
}

function highlight(value: string, syntax: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let last = 0
  for (const m of value.matchAll(tokenPattern(syntax))) {
    const at = m.index ?? 0
    if (at > last) nodes.push(value.slice(last, at))
    nodes.push(
      <span key={`${at}-${m[0]}`} className="studio-token">
        {m[0]}
      </span>,
    )
    last = at + m[0].length
  }
  if (last < value.length) nodes.push(value.slice(last))
  return nodes
}

export function PromptCodeEditor({
  value,
  syntax,
  onChange,
  readOnly = false,
  placeholder,
  ariaLabel,
  className,
}: {
  value: string
  syntax: string
  onChange?: (v: string) => void
  readOnly?: boolean
  placeholder?: string
  ariaLabel: string
  className?: string
}) {
  const preRef = useRef<HTMLPreElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const nodes = useMemo(() => highlight(value, syntax), [value, syntax])

  // textarea 出现纵向滚动条时内容宽度会少掉滚动条那几像素；高亮层要和它等宽，
  // 否则长行在两边的换行点不同，滚动之后文字和光标就会错位。
  useEffect(() => {
    const ta = inputRef.current
    const pre = preRef.current
    if (!ta || !pre) return
    const sync = () => {
      pre.style.width = `${ta.clientWidth}px`
    }
    sync()
    const observer = new ResizeObserver(sync)
    observer.observe(ta)
    return () => observer.disconnect()
  }, [])

  return (
    <div className={cn('studio-code', readOnly && 'studio-code--readonly', className)}>
      <pre ref={preRef} className="studio-code__highlight" aria-hidden="true">
        {nodes}
        {'\n'}
      </pre>
      <textarea
        ref={inputRef}
        className="studio-code__input"
        value={value}
        readOnly={readOnly}
        onChange={(e) => onChange?.(e.target.value)}
        onScroll={(e) => {
          const pre = preRef.current
          if (!pre) return
          pre.scrollTop = e.currentTarget.scrollTop
          pre.scrollLeft = e.currentTarget.scrollLeft
        }}
        placeholder={placeholder}
        aria-label={ariaLabel}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
      />
    </div>
  )
}
