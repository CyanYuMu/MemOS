/** 图谱探索用的纯逻辑：边索引、跳数、搜索匹配。
 *
 *  单独一层的理由和 `constellation.ts` 一样——不碰 DOM、不碰 React，页面和画布
 *  共用同一套判定（抽屉里的「N 跳内 M 条」与画布上的高亮必须一致）。
 */

import type { GraphLink } from './api'

/** 索引化之后的边。两端都是 `nodes` 数组的下标。 */
export interface IndexedEdge {
  a: number
  b: number
}

/** 把 id 形式的边映射成下标边。端点不存在的边直接丢弃（类型过滤后会大量出现）。 */
export function indexEdges(ids: Array<{ id: string }>, links: Array<Pick<GraphLink, 'source' | 'target'>>): IndexedEdge[] {
  const index = new Map<string, number>()
  ids.forEach((n, i) => index.set(n.id, i))
  const out: IndexedEdge[] = []
  for (const l of links) {
    const a = index.get(l.source)
    const b = index.get(l.target)
    if (a === undefined || b === undefined || a === b) continue
    out.push({ a, b })
  }
  return out
}

/** 无向邻接表。图谱的边在业务上是双向可达的（同一会话 / PARENT / 归纳来源）。 */
export function adjacencyOf(nodeCount: number, edges: IndexedEdge[]): number[][] {
  const adj: number[][] = Array.from({ length: nodeCount }, () => [])
  for (const e of edges) {
    adj[e.a].push(e.b)
    adj[e.b].push(e.a)
  }
  return adj
}

/**
 * 从 `start` 出发的 BFS 跳数，最多 `maxDepth` 跳。
 *
 * 返回长度 = 节点数；`0` 是起点，`1..maxDepth` 是跳数，`-1` 表示不可达
 * 或超出 `maxDepth`——调用方用 `>= 0 && <= depth` 判断是否在聚焦范围内。
 * 不无限 BFS 是刻意的：层级过滤的语义就是「最多 N 跳」，再远的点本来也不画。
 */
export function hopDepths(nodeCount: number, edges: IndexedEdge[], start: number, maxDepth = 3): Int16Array {
  const depths = new Int16Array(nodeCount).fill(-1)
  if (start < 0 || start >= nodeCount || maxDepth < 0) return depths
  const adj = adjacencyOf(nodeCount, edges)
  depths[start] = 0
  let frontier = [start]
  for (let d = 1; d <= maxDepth && frontier.length > 0; d++) {
    const next: number[] = []
    for (const u of frontier) {
      for (const v of adj[u]) {
        if (depths[v] !== -1) continue
        depths[v] = d
        next.push(v)
      }
    }
    frontier = next
  }
  return depths
}

/** 聚焦范围内（0..depth 跳）的节点数。 */
export function countWithin(depths: Int16Array, depth: number): number {
  let n = 0
  for (let i = 0; i < depths.length; i++) if (depths[i] >= 0 && depths[i] <= depth) n++
  return n
}

export interface MatchOptions {
  /** 类型的本地化名字。搜「洞察」时也应该能命中 InsightMemory。 */
  typeLabel?: (memoryType: string) => string
  limit?: number
}

/** 一个节点的可搜索文本。顺序即优先级：正文 → 会话 → id → 类型。 */
function haystack(node: { id: string; label: string; sessionId: string; memoryType: string }, typeLabel?: (t: string) => string): string[] {
  return [node.label, node.sessionId, node.id, typeLabel?.(node.memoryType) ?? node.memoryType]
}

/**
 * 搜索记忆。返回**按相关度排序的下标**，直接用于画布高亮和结果列表。
 *
 * 排序规则（数字越小越靠前）：
 *   0 正文完全相等 · 1 正文开头 · 2 会话开头 · 3 id 开头 · 4 类型开头
 *   5 正文包含   · 6 会话包含 · 7 id 包含 · 8 类型包含
 * 同分按数组顺序（BFF 已按时间升序给好），保证同一份数据每次结果一致。
 */
export function matchNodes(
  nodes: Array<{ id: string; label: string; sessionId: string; memoryType: string }>,
  query: string,
  { typeLabel, limit = 50 }: MatchOptions = {},
): number[] {
  const q = query.trim().toLowerCase()
  if (!q) return []
  const hits: Array<{ index: number; score: number }> = []
  for (let i = 0; i < nodes.length; i++) {
    const fields = haystack(nodes[i], typeLabel).map((s) => s.toLowerCase())
    let score = -1
    const consider = (v: number) => {
      if (score < 0 || v < score) score = v
    }
    for (let f = 0; f < fields.length; f++) {
      const field = fields[f]
      if (!field) continue
      if (field === q) consider(f === 0 ? 0 : f + 2)
      else if (field.startsWith(q)) consider(f + 1)
      else if (field.includes(q)) consider(f + 5)
    }
    if (score >= 0) hits.push({ index: i, score })
  }
  hits.sort((a, b) => a.score - b.score || a.index - b.index)
  return hits.slice(0, limit).map((h) => h.index)
}
