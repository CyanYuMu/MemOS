import { memo, useMemo, type ReactNode, type ComponentPropsWithoutRef } from 'react'
import { common, createLowlight } from 'lowlight'
import 'highlight.js/styles/github.css'

const highlighter = createLowlight(common)
type Token = ReturnType<typeof highlighter.highlight>['children'][number]
function renderToken(node: Token, key: string): ReactNode {
  if (node.type === 'text') return node.value
  if (node.type !== 'element') return null
  const classes = node.properties.className
  return (
    <span key={key} className={Array.isArray(classes) ? classes.join(' ') : undefined}>
      {node.children.map((child, index) => renderToken(child, `${key}-${index}`))}
    </span>
  )
}

/** 直接渲染高亮AST，保留代码原文，不插入HTML或额外复制按钮。 */
export const HighlightedCode = memo(function HighlightedCode({
  text,
  language,
  className = '',
  ...attributes
}: {
  text: string
  language?: string
  className?: string
} & Omit<ComponentPropsWithoutRef<'code'>, 'children'>) {
  const tree = useMemo(() => {
    if (language) return highlighter.registered(language) ? highlighter.highlight(language, text) : null
    return highlighter.highlightAuto(text, { subset: ['json', 'bash', 'python', 'typescript', 'javascript', 'sql'] })
  }, [text, language])
  return (
    <code {...attributes} className={`syntax-code hljs language-${language || tree?.data?.language || 'plaintext'} ${className}`}>
      {tree ? tree.children.map((node, index) => renderToken(node, String(index))) : text}
    </code>
  )
})
