import type { Memory } from '../../lib/api'
/** 记忆工具结果的解析与汇总。
 *
 *  MCP 工具把业务 JSON 塞在 `content[0].text` 里（text 是字符串，不是对象），
 *  这里统一拆成记忆条目；结构不认识时返回 `parsed: false`，由调用方回退到原始数据。
 */

import {
  asNumber,
  asRecord,
  asString,
  shortToolName,
  toolQuery,
  toolResultCount,
  type ChatTimelineItem,
  type ChatToolItem,
} from './timeline'

export interface MemoryEntry {
  memoryId: string
  value: string
  key?: string
  /** 0–1 的相关性；上游也可能给 0–100 */
  relativity?: number
  createTime?: string
  updateTime?: string
  memoryType?: string
  detailType?: string
  status?: string
  tags?: string[]
  source: 'memory' | 'dream'
}

export interface ParsedMemoryResult {
  parsed: boolean
  entries: MemoryEntry[]
}

export interface ChatMemoryItem extends MemoryEntry {
  toolId: string
  toolCallId: string
  toolName: string
  /** 这次工具调用带的查询词 */
  query: string
  /** 同一记忆在更早一次检索中返回的不同内容；不推断未返回的变更。 */
  previousValue?: string
}

/** 判断一个工具是否是记忆类工具；MCP 名字带 `mcp__server__` 前缀 */
export function isMemoryTool(toolName: string): boolean {
  return toolName.endsWith('search_memory') || toolName.endsWith('get_user_profile')
}

function parseJson(value: unknown): unknown {
  if (typeof value !== 'string') return value
  const text = value.trim()
  if (!text) return value
  try {
    return JSON.parse(text)
  } catch {
    return value
  }
}

function textBlocksOf(value: unknown): string[] {
  if (typeof value === 'string') return [value]
  const rec = asRecord(value)
  if (!rec || !Array.isArray(rec.content)) return []
  const out: string[] = []
  for (const block of rec.content) {
    const item = asRecord(block)
    if (!item) continue
    const type = (asString(item.type) ?? '').toLowerCase()
    if (type && type !== 'text') continue
    const text = asString(item.text) ?? asString(item.content)
    if (text) out.push(text)
  }
  return out
}

interface RawMemory {
  raw: Record<string, unknown>
  source: MemoryEntry['source']
}

function memoryListsOf(value: unknown): RawMemory[] | null {
  const rec = asRecord(value)
  if (!rec) return null
  let found = false
  const out: RawMemory[] = []
  for (const [field, source] of [
    ['memory_detail_list', 'memory'],
    ['preference_detail_list', 'memory'],
    ['tool_memory_detail_list', 'memory'],
    ['dream_memory_detail_list', 'dream'],
  ] as const) {
    const list = rec[field]
    if (!Array.isArray(list)) continue
    found = true
    for (const item of list) {
      const raw = asRecord(item)
      if (raw) out.push({ raw, source })
    }
  }
  return found ? out : null
}

function entryOf({ raw, source }: RawMemory): MemoryEntry | null {
  const metadata = asRecord(parseJson(raw.metadata)) ?? {}
  const tags = raw.tags ?? metadata.tags
  const memoryId = asString(raw.memory_id) ?? asString(raw.memoryId) ?? asString(raw.id) ?? ''
  const value =
    asString(raw.memory_value) ??
    asString(raw.memoryValue) ??
    asString(raw.content) ??
    asString(raw.preference) ??
    asString(raw.tool_value) ??
    asString(raw.value) ??
    ''
  if (!memoryId && !value.trim()) return null
  return {
    memoryId,
    value,
    key: asString(raw.memory_key) ?? asString(raw.memoryKey) ?? asString(raw.key),
    relativity: asNumber(raw.relativity) ?? asNumber(raw.score),
    createTime: asString(raw.create_time) ?? asString(raw.createTime) ?? asString(raw.created_at),
    updateTime: asString(raw.updated_at) ?? asString(raw.update_time) ?? asString(metadata.updated_at),
    memoryType: asString(raw.memory_type) ?? asString(metadata.memory_type),
    status: asString(raw.status) ?? asString(metadata.status),
    tags: Array.isArray(tags) ? tags.filter((tag): tag is string => typeof tag === 'string') : undefined,
    source,
  }
}

export function parseMemoryResult(result: unknown): ParsedMemoryResult {
  if (result === undefined || result === null) return { parsed: false, entries: [] }

  // 三条常见路径：历史里的纯文本、工具事件里的 {content:[{type:'text'}]}、
  // 以及偶尔被上游直接展开的对象。逐个尝试，任何一步不认识就回退。
  const candidates: unknown[] = []
  const blocks = textBlocksOf(result)
  if (blocks.length) candidates.push(...blocks.map(parseJson))
  else candidates.push(parseJson(result))
  if (typeof result === 'object') candidates.push(result)

  for (const candidate of candidates) {
    let lists = memoryListsOf(candidate)
    if (!lists) {
      const rec = asRecord(candidate)
      if (rec) {
        for (const field of ['data', 'result']) {
          lists = memoryListsOf(parseJson(rec[field]))
          if (lists) break
        }
      }
    }
    if (lists) {
      return {
        parsed: true,
        entries: lists.map(entryOf).filter((entry): entry is MemoryEntry => entry !== null),
      }
    }
  }
  return { parsed: false, entries: [] }
}

/** 摘要行里的命中条数：优先解析真实结构，旧格式再退回通用计数 */
export function memoryHitCount(item: ChatToolItem): number | undefined {
  if (item.running) return undefined
  const parsed = parseMemoryResult(item.result)
  if (parsed.parsed) return parsed.entries.length
  return toolResultCount(item.result)
}

/** 输入按最新在前排列；按 memory_id 去重，并保留更早检索中的不同内容。 */
export function collectMemories(items: ChatTimelineItem[]): ChatMemoryItem[] {
  const out: ChatMemoryItem[] = []
  const seen = new Map<string, ChatMemoryItem>()
  for (const item of items) {
    if (item.kind !== 'tool' || item.running || item.isError || !isMemoryTool(item.toolName)) continue
    const parsed = parseMemoryResult(item.result)
    if (!parsed.parsed) continue
    for (const entry of parsed.entries) {
      const dedupeKey = entry.memoryId || entry.value
      if (!dedupeKey) continue
      const current = seen.get(dedupeKey)
      if (current) {
        if (current.value !== entry.value && current.previousValue === undefined) current.previousValue = entry.value
        continue
      }
      const memory = {
        ...entry,
        toolId: item.id,
        toolCallId: item.toolCallId,
        toolName: shortToolName(item.toolName),
        query: toolQuery(item.args),
      }
      seen.set(dedupeKey, memory)
      out.push(memory)
    }
  }
  return out
}

/** 相关性按 0–1 处理；上游给百分数时自动换算 */
export function relativityPercent(value: number | undefined): number | undefined {
  if (value === undefined || !Number.isFinite(value) || value < 0) return undefined
  const ratio = value > 1 ? value / 100 : value
  return Math.max(0, Math.min(100, Math.round(ratio * 100)))
}

export function entryFromMemory(memory: Memory): MemoryEntry {
  return {
    memoryId: memory.memoryId,
    relativity: memory.relativity,
    key: memory.memoryKey,
    value: memory.memoryContent,
    memoryType: memory.memoryType,
    status: memory.status,
    tags: memory.tags,
    updateTime: memory.updateTime,
    createTime: memory.createTime,
    source: 'memory',
  }
}
