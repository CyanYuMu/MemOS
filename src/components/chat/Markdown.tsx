/** 助手消息的 Markdown 渲染。
 *
 *  只给 assistant 用；用户输入保持纯文本，避免把 `$` 当成公式。
 *  raw HTML 不开启（react-markdown 默认不渲染），链接一律新窗口打开。
 */

import { isValidElement, memo, useEffect, useState, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import rehypeHighlight from 'rehype-highlight'
import 'katex/dist/katex.min.css'
import 'highlight.js/styles/github.css'
import { Icon } from '../Icon'
import { useT } from '../../lib/store'
import { copyText } from '../../lib/clipboard'
import { MemoryCitation } from './MemoryCitation'
import { MemoryCitationContext } from './memoryCitationContext'
import { MEMORY_CITATION_PREFIX, remarkMemoryCitations, type MemoryCitationScope } from './memoryCitations'

function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children)
  return ''
}

function languageOf(node: ReactNode): string {
  if (!isValidElement<{ className?: string }>(node)) return ''
  const match = /language-([\w-]+)/.exec(node.props.className ?? '')
  return match?.[1] ?? ''
}

/** 代码块：顶部一条细栏放语言与复制按钮，正文横向滚动，不撑破气泡。 */
function CodeBlock({ children }: { children?: ReactNode }) {
  const t = useT()
  const [copied, setCopied] = useState(false)
  const text = textOf(children)
  const language = languageOf(children)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
    try {
      await copyText(text)
      setCopied(true)
    } catch {
      // 复制失败不打断阅读，按钮保持原状
    }
  }

  return (
    <div className="chat-code">
      <div className="chat-code__bar">
        <span className="chat-code__lang">{language || 'code'}</span>
        <button
          type="button"
          className="chat-code__copy"
          onClick={() => void copy()}
          aria-label={copied ? t('common.copied') : t('common.copy')}
          title={copied ? t('common.copied') : t('common.copy')}
        >
          <Icon name={copied ? 'i-lucide:check' : 'i-lucide:copy'} className="size-3.5" />
          <span>{copied ? t('common.copied') : t('common.copy')}</span>
        </button>
      </div>
      <pre className="chat-code__pre">{children}</pre>
    </div>
  )
}

const COMPONENTS: Components = {
  a: ({ node: _node, ...props }) => {
    if (props.href?.startsWith(MEMORY_CITATION_PREFIX)) {
      try {
        return <MemoryCitation token={decodeURIComponent(props.href.slice(MEMORY_CITATION_PREFIX.length))} />
      } catch {
        return null
      }
    }
    return <a {...props} target="_blank" rel="noreferrer noopener" />
  },
  table: ({ node: _node, children, ...props }) => (
    <div className="chat-table-wrap">
      <table {...props}>{children}</table>
    </div>
  ),
  pre: ({ node: _node, children }) => <CodeBlock>{children}</CodeBlock>,
}

/** `text` 由调用方做 `useDeferredValue`，这里 memo 掉无关重渲染。 */
export const Markdown = memo(function Markdown({ text, memory }: { text: string; memory?: MemoryCitationScope }) {
  return (
    <MemoryCitationContext.Provider value={memory}>
      <div className="chat-markdown">
        <ReactMarkdown
          remarkPlugins={memory ? [remarkGfm, remarkMath, remarkMemoryCitations] : [remarkGfm, remarkMath]}
          rehypePlugins={[
            [rehypeKatex, { throwOnError: false, errorColor: 'var(--app-danger)' }],
            [rehypeHighlight, { detect: true, subset: ['json', 'python', 'bash', 'javascript', 'typescript', 'sql'] }],
          ]}
          components={COMPONENTS}
        >
          {text}
        </ReactMarkdown>
      </div>
    </MemoryCitationContext.Provider>
  )
})
