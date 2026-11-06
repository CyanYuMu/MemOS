import type { TurnSnapshot } from './turnSnapshot'

export interface MemoryCitationScope {
  projectId: string
  snapshot?: TurnSnapshot
}

/** 数字来自本轮卡片顺序；跨调用、跨版本或有歧义的ID不猜关联。 */
export function resolveMemoryCitation(token: string, snapshot?: TurnSnapshot) {
  if (!snapshot) return null
  const full = /^\[memory:([^\]\n]+)\]$/.exec(token)
  const legacy = /^\[\d+:([0-9a-f]{8})\]$/i.exec(token)
  let matches: number[] = []
  let sourceCall: string | null = null
  if (full) {
    const slash = full[1].indexOf('/')
    const callId = slash >= 0 ? full[1].slice(0, slash) : null
    sourceCall = callId
    const id = slash >= 0 ? full[1].slice(slash + 1) : full[1]
    matches = snapshot.entries.flatMap((row, index) =>
      row.entry.memoryId === id && (callId === null || row.origins?.some((o) => o.callId === callId)) ? [index] : [],
    )
  } else if (legacy) {
    matches = snapshot.entries.flatMap((row, index) =>
      row.entry.memoryId.toLowerCase().startsWith(legacy[1].toLowerCase()) ? [index] : [],
    )
  }
  if (matches.length !== 1) return null
  const row = snapshot.entries[matches[0]]
  const origin = sourceCall ? row.origins?.find((o) => o.callId === sourceCall) : undefined
  // 同文不同元数据也保留原调用版本；不能借另一调用的完整实体补齐。
  return { row: origin?.entry ? { ...row, entry: origin.entry, record: origin.record } : row, index: matches[0] }
}

interface MarkdownNode {
  type: string
  value?: string
  url?: string
  children?: MarkdownNode[]
}
export const MEMORY_CITATION_PREFIX = '#memory-citation:'

/** 仅转换正文文本节点，代码块和已有链接不成为引用按钮。 */
export function remarkMemoryCitations() {
  return (tree: unknown) => {
    function visit(node: MarkdownNode) {
      if (!node.children || node.type === 'link') return
      node.children = node.children.flatMap((child) => {
        if (child.type !== 'text') {
          visit(child)
          return [child]
        }
        const value = (child.value ?? '').replace(/\[memory:[^\]\n]*$/, '')
        const result: MarkdownNode[] = []
        let from = 0
        for (const match of value.matchAll(/\[memory:[^\]\n]{1,520}\]|\[\d+:[0-9a-fA-F]{8}\]/g)) {
          if (match.index > from) result.push({ type: 'text', value: value.slice(from, match.index) })
          result.push({
            type: 'link',
            url: MEMORY_CITATION_PREFIX + encodeURIComponent(match[0]),
            children: [{ type: 'text', value: match[0] }],
          })
          from = match.index + match[0].length
        }
        if (from < value.length) result.push({ type: 'text', value: value.slice(from) })
        return result
      })
    }
    visit(tree as MarkdownNode)
  }
}

/** 复制和反馈引用保留可读编号，不把内部调用ID带入用户文字。 */
export function memoryCitationText(text: string, snapshot?: TurnSnapshot) {
  return text.replace(/^(?: {4}|\t).*|(`+)[\s\S]*?\1|\[memory:[^\]\n]{1,520}\]|\[\d+:[0-9a-fA-F]{8}\]/gm, (token) => {
    if (/^(?: {4}|\t|`)/.test(token)) return token
    const match = resolveMemoryCitation(token, snapshot)
    return match ? `[${match.index + 1}]` : ''
  })
}
