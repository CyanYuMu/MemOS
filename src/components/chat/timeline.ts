/** 对话时间线的解析与更新。
 *
 *  pi 的消息结构与 SSE 事件字段都按“上游可能变”处理：这里只认稳定字段，
 *  每次取值都先收窄 `unknown`。页面层面不直接碰原始消息。
 */

import type { ChatEvent, ChatMessage } from '../../../shared/contract'

interface ExecutionIdentity {
  submissionId?: string
  origin?: 'history' | 'stream'
}

export interface ChatMessageItem extends ExecutionIdentity {
  kind: 'message'
  id: string
  role: 'user' | 'assistant'
  timestamp?: string | number
  text: string
  streaming: boolean
}

export interface ChatToolItem extends ExecutionIdentity {
  kind: 'tool'
  id: string
  toolCallId: string
  toolName: string
  args: unknown
  result: unknown
  isError: boolean
  running: boolean
  timestamp?: string | number
}

/** 思考过程单独成项：不和助手正文混在同一个气泡里。 */
export interface ChatThinkingItem extends ExecutionIdentity {
  kind: 'thinking'
  id: string
  text: string
  streaming: boolean
}

export type ChatTimelineItem = ChatMessageItem | ChatToolItem | ChatThinkingItem

type StreamActionBody =
  | { kind: 'assistant_start'; id: string; text: string }
  | { kind: 'assistant_delta'; id: string; delta: string }
  | { kind: 'assistant_end'; id: string | null; fallbackId: string; text: string; timestamp?: string | number }
  | { kind: 'thinking_start'; id: string }
  | { kind: 'thinking_delta'; id: string; delta: string }
  /** id 为 null 时把所有还在流式输出的思考块一并收尾 */
  | { kind: 'thinking_end'; id: string | null }
  | { kind: 'tool_start'; seed: string; data: unknown; timestamp?: number }
  | { kind: 'tool_update'; seed: string; data: unknown; timestamp?: number }
  | { kind: 'tool_end'; seed: string; data: unknown; timestamp?: number }

export type StreamAction = StreamActionBody & { submissionId?: string }

/* ------------------------------------------------------------ 收窄工具 */

export function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

export function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

export function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

/* ------------------------------------------------------- SSE 单帧解析 */

export function parseChatEvent(raw: string): ChatEvent | null {
  try {
    const parsed: unknown = JSON.parse(raw)
    const rec = asRecord(parsed)
    if (!rec) return null
    return {
      seq: asNumber(rec.seq) ?? 0,
      submissionId: asString(rec.submissionId),
      type: asString(rec.type) ?? '',
      data: rec.data,
    }
  } catch {
    return null
  }
}

/** `message_start` / `message_end` 的正文可能直接挂在 data 上，也可能包一层 `message` */
export function messagePayload(data: unknown): unknown {
  const rec = asRecord(data)
  return rec?.message ?? rec?.assistantMessage ?? data
}

/** `message_update` 的增量在 `assistantMessageEvent` 里 */
export function messageUpdatePayload(data: unknown): unknown {
  const rec = asRecord(data)
  return rec?.assistantMessageEvent ?? rec?.event ?? data
}

export function eventTypeOf(payload: unknown): string {
  return (asString(asRecord(payload)?.type) ?? '').toLowerCase()
}

/** 文本增量：字段名在 delta / text / content 之间挑第一个字符串 */
export function extractDelta(payload: unknown): string {
  if (typeof payload === 'string') return payload
  const rec = asRecord(payload)
  if (!rec) return ''
  for (const key of ['delta', 'text', 'content']) {
    const value = rec[key]
    if (typeof value === 'string') return value
  }
  return ''
}

/** 只对真正的文本增量返回 delta。
 *
 *  pi 的 `text_end` 带 `content`（整段文本），当成增量再拼一遍会重复；
 *  `text_start` 只有 contentIndex；thinking / toolcall 的增量后端已经过滤，
 *  这里再拦一次，防止上游换版本后又冒出来。 */
export function textDeltaOf(payload: unknown): string {
  const kind = eventTypeOf(payload)
  if (kind === 'text_start' || kind === 'text_end') return ''
  if (kind.includes('thinking') || kind.includes('reasoning') || kind.includes('tool')) return ''
  return extractDelta(payload)
}

export type ThinkingPhase = 'start' | 'delta' | 'end'

/** 思考事件：后端透传 thinking_*（部分版本叫 reasoning_*） */
export function thinkingPhaseOf(payload: unknown): ThinkingPhase | null {
  const kind = eventTypeOf(payload)
  if (kind === 'thinking_start' || kind === 'reasoning_start') return 'start'
  if (kind === 'thinking_delta' || kind === 'reasoning_delta') return 'delta'
  if (kind === 'thinking_end' || kind === 'reasoning_end') return 'end'
  return null
}

export function thinkingDeltaOf(payload: unknown): string {
  return thinkingPhaseOf(payload) === 'delta' ? extractDelta(payload) : ''
}

export function roleOfMessage(payload: unknown): string {
  return (asString(asRecord(payload)?.role) ?? '').toLowerCase()
}

export function errorMessageOf(data: unknown): string {
  if (typeof data === 'string') return data
  const rec = asRecord(data)
  if (!rec) return ''
  for (const key of ['message', 'error', 'reason', 'detail']) {
    const value = rec[key]
    if (typeof value === 'string' && value.trim()) return value
    const nested = asRecord(value)
    if (nested) {
      const text = asString(nested.message) ?? asString(nested.error)
      if (text?.trim()) return text
    }
  }
  return ''
}

/* --------------------------------------------------------- 消息正文 */

function collectText(content: unknown): string[] {
  if (typeof content === 'string') return [content]
  if (!Array.isArray(content)) {
    const rec = asRecord(content)
    if (!rec) return []
    const text = asString(rec.text) ?? asString(rec.content)
    return text === undefined ? [] : [text]
  }
  const out: string[] = []
  for (const block of content) {
    if (typeof block === 'string') {
      out.push(block)
      continue
    }
    const rec = asRecord(block)
    if (!rec) continue
    const type = (asString(rec.type) ?? '').toLowerCase()
    // 思考过程不进气泡；工具调用由 tool 卡片展示
    if (type === 'thinking' || type === 'reasoning' || type === 'toolcall' || type === 'tool_call' || type === 'tool_use') continue
    const text = asString(rec.text) ?? asString(rec.content)
    if (text !== undefined) out.push(text)
  }
  return out
}

export function textOfMessage(payload: unknown): string {
  const rec = asRecord(payload)
  const content = rec && 'content' in rec ? rec.content : payload
  return collectText(content).join('\n').trim()
}

/** 历史消息里的思考块；pi 存盘时字段名可能是 thinking / text / content */
function thinkingTextsOf(content: unknown): string[] {
  if (!Array.isArray(content)) return []
  const out: string[] = []
  for (const block of content) {
    const rec = asRecord(block)
    if (!rec) continue
    const type = (asString(rec.type) ?? '').toLowerCase()
    if (type !== 'thinking' && type !== 'reasoning') continue
    const text = asString(rec.thinking) ?? asString(rec.text) ?? asString(rec.content)
    if (text?.trim()) out.push(text)
  }
  return out
}

interface ParsedToolCall {
  toolCallId: string
  toolName: string
  args: unknown
}

function toolCallsOf(content: unknown, base = 0): ParsedToolCall[] {
  if (!Array.isArray(content)) return []
  const out: ParsedToolCall[] = []
  content.forEach((block, index) => {
    const rec = asRecord(block)
    if (!rec) return
    const type = (asString(rec.type) ?? '').toLowerCase()
    if (type !== 'toolcall' && type !== 'tool_call' && type !== 'tool_use') return
    const toolName = asString(rec.name) ?? asString(rec.toolName) ?? ''
    out.push({
      toolCallId: asString(rec.id) ?? asString(rec.toolCallId) ?? `${toolName || 'tool'}_${base + index}`,
      toolName,
      args: rec.arguments ?? rec.args ?? rec.input,
    })
  })
  return out
}

/* --------------------------------------------------------- 历史消息 */

/** `offset` 是这一页在原消息数组里的绝对起始下标。
 *
 *  跨页拼接时只靠页内下标会生成重复 id，所以兜底 id 一律用绝对下标；
 *  工具卡片用 toolCallId 作为身份，方便后一页的 result 合并到前一页的空卡片上。 */
export function parseHistoryMessages(messages: ChatMessage[], offset = 0): ChatTimelineItem[] {
  const items: ChatTimelineItem[] = []
  const toolIndex = new Map<string, number>()

  messages.forEach((raw, index) => {
    const absolute = offset + index
    const role = (raw.role ?? '').toLowerCase()
    const seedId = raw.id || `history_${absolute}`

    if (role === 'user') {
      const text = textOfMessage(raw)
      if (text) items.push({ kind: 'message', id: seedId, role: 'user', text, streaming: false })
      return
    }

    if (role === 'assistant') {
      thinkingTextsOf(raw.content).forEach((text, blockIndex) => {
        items.push({
          kind: 'thinking',
          id: `thinking_${raw.id || absolute}_${blockIndex}`,
          text,
          streaming: false,
        })
      })
      const text = textOfMessage(raw)
      if (text)
        items.push({
          kind: 'message',
          id: seedId,
          role: 'assistant',
          text,
          streaming: false,
          timestamp: asString(raw.timestamp) ?? asNumber(raw.timestamp),
        })
      for (const call of toolCallsOf(raw.content, absolute)) {
        toolIndex.set(call.toolCallId, items.length)
        items.push({
          kind: 'tool',
          id: `tool_${call.toolCallId}`,
          toolCallId: call.toolCallId,
          toolName: call.toolName,
          args: call.args,
          result: undefined,
          isError: false,
          running: false,
          timestamp: asString(raw.timestamp) ?? asNumber(raw.timestamp),
        })
      }
      return
    }

    if (role === 'toolresult' || role === 'tool_result' || role === 'tool') {
      const toolCallId = asString(raw.toolCallId) ?? asString(raw.tool_call_id) ?? asString(raw.id) ?? ''
      const toolName = asString(raw.toolName) ?? asString(raw.tool_name) ?? asString(raw.name) ?? ''
      const isError = raw.isError === true || raw.is_error === true
      const text = textOfMessage(raw)
      const result = text || raw.content
      const at = toolCallId ? toolIndex.get(toolCallId) : undefined
      const existing = at === undefined ? undefined : items[at]
      if (existing?.kind === 'tool') {
        existing.result = result
        existing.isError = isError
        existing.running = false
        existing.timestamp = asString(raw.timestamp) ?? asNumber(raw.timestamp) ?? existing.timestamp
      } else {
        const key = toolCallId || `result_${absolute}`
        items.push({
          kind: 'tool',
          id: `tool_${key}`,
          toolCallId: key,
          toolName,
          args: undefined,
          result,
          isError,
          running: false,
          timestamp: asString(raw.timestamp) ?? asNumber(raw.timestamp),
        })
      }
    }
    // system 与其他角色不展示
  })

  return items.map((item) => ({ ...item, origin: 'history' as const }))
}

/** 把更早的一页前插到当前时间线。
 *
 *  相邻页会在工具调用边界轻微重叠：消息按 id 去重，工具卡片按 toolCallId 合并，
 *  后一页（更新）的 result 覆盖前一页的空结果。 */
export function mergeTimelinePages(older: ChatTimelineItem[], current: ChatTimelineItem[]): ChatTimelineItem[] {
  const merged: ChatTimelineItem[] = []
  const idIndex = new Map<string, number>()
  const toolIndex = new Map<string, number>()

  const add = (item: ChatTimelineItem) => {
    if (item.kind === 'tool') {
      const at = toolIndex.get(item.toolCallId)
      if (at === undefined) {
        toolIndex.set(item.toolCallId, merged.length)
        merged.push(item)
        return
      }
      const prev = merged[at]
      if (prev.kind !== 'tool') return
      merged[at] = {
        ...prev,
        toolName: prev.toolName || item.toolName,
        args: prev.args !== undefined ? prev.args : item.args,
        result: item.result !== undefined ? item.result : prev.result,
        isError: item.result !== undefined ? item.isError : prev.isError,
        running: prev.running && item.running,
        timestamp: item.timestamp ?? prev.timestamp,
      }
      return
    }

    const at = idIndex.get(item.id)
    if (at === undefined) {
      idIndex.set(item.id, merged.length)
      merged.push(item)
      return
    }
    const prev = merged[at]
    if (prev.kind === 'message' && item.kind === 'message' && !prev.text.trim() && item.text.trim()) {
      merged[at] = item
    }
  }

  for (const item of older) add(item)
  for (const item of current) add(item)
  return merged
}

/* --------------------------------------------------------- 流式更新 */

interface ToolEventData {
  toolCallId: string
  toolName: string
  args: unknown
  result: unknown
  hasResult: boolean
  isError: boolean
}

function readToolEvent(data: unknown, seed: string): ToolEventData {
  const rec = asRecord(data)
  return {
    toolCallId: asString(rec?.toolCallId) ?? asString(rec?.id) ?? `tool_${seed}`,
    toolName: asString(rec?.toolName) ?? asString(rec?.name) ?? '',
    args: rec?.args ?? rec?.arguments,
    result: rec?.partialResult ?? rec?.result,
    hasResult: rec !== null && ('result' in rec || 'partialResult' in rec),
    isError: rec?.isError === true || rec?.is_error === true,
  }
}

function applyToolAction(items: ChatTimelineItem[], action: Extract<StreamAction, { kind: 'tool_start' | 'tool_update' | 'tool_end' }>) {
  const data = readToolEvent(action.data, action.seed)
  const index = items.findIndex((item) => item.kind === 'tool' && item.toolCallId === data.toolCallId)
  const running = action.kind !== 'tool_end'

  if (index < 0) {
    return [
      ...items,
      {
        kind: 'tool' as const,
        id: `tool_${data.toolCallId}`,
        toolCallId: data.toolCallId,
        toolName: data.toolName,
        args: data.args,
        result: data.hasResult ? data.result : undefined,
        isError: data.isError,
        running,
        timestamp: action.timestamp,
      },
    ]
  }

  const next = items.slice()
  const current = next[index]
  if (current.kind === 'tool') {
    next[index] = {
      ...current,
      toolName: data.toolName || current.toolName,
      args: data.args !== undefined ? data.args : current.args,
      result: data.hasResult ? data.result : current.result,
      isError: action.kind === 'tool_end' ? data.isError : current.isError,
      running,
      timestamp: action.timestamp ?? current.timestamp,
    }
  }
  return next
}

function applyStreamActionBody(items: ChatTimelineItem[], action: StreamAction): ChatTimelineItem[] {
  switch (action.kind) {
    case 'assistant_start': {
      const index = items.findIndex((item) => item.kind === 'message' && item.id === action.id)
      if (index < 0) {
        return [...items, { kind: 'message', id: action.id, role: 'assistant', text: action.text, streaming: true }]
      }
      const next = items.slice()
      const current = next[index]
      if (current.kind === 'message') next[index] = { ...current, text: current.text || action.text, streaming: true }
      return next
    }

    case 'assistant_delta': {
      const index = items.findIndex((item) => item.kind === 'message' && item.id === action.id)
      if (index < 0) {
        return [...items, { kind: 'message', id: action.id, role: 'assistant', text: action.delta, streaming: true }]
      }
      const next = items.slice()
      const current = next[index]
      if (current.kind === 'message') next[index] = { ...current, text: current.text + action.delta, streaming: true }
      return next
    }

    case 'assistant_end': {
      const index = action.id ? items.findIndex((item) => item.kind === 'message' && item.id === action.id) : -1
      if (index >= 0) {
        const current = items[index]
        const text = action.text.trim() ? action.text : current.kind === 'message' ? current.text : ''
        if (!text.trim()) return items.filter((_, i) => i !== index)
        const next = items.slice()
        next[index] = { ...items[index], text, streaming: false, timestamp: action.timestamp } as ChatMessageItem
        return next
      }
      if (!action.text.trim()) return items
      return [
        ...items,
        { kind: 'message', id: action.fallbackId, role: 'assistant', text: action.text, streaming: false, timestamp: action.timestamp },
      ]
    }

    case 'thinking_start': {
      const index = items.findIndex((item) => item.kind === 'thinking' && item.id === action.id)
      if (index >= 0) {
        const next = items.slice()
        const current = next[index]
        if (current.kind === 'thinking') next[index] = { ...current, streaming: true }
        return next
      }
      return [...items, { kind: 'thinking', id: action.id, text: '', streaming: true }]
    }

    case 'thinking_delta': {
      const index = items.findIndex((item) => item.kind === 'thinking' && item.id === action.id)
      if (index < 0) {
        return [...items, { kind: 'thinking', id: action.id, text: action.delta, streaming: true }]
      }
      const next = items.slice()
      const current = next[index]
      if (current.kind === 'thinking') next[index] = { ...current, text: current.text + action.delta, streaming: true }
      return next
    }

    case 'thinking_end':
      return closeThinking(items, action.id)

    case 'tool_start':
    case 'tool_update':
    case 'tool_end':
      return applyToolAction(items, action)
  }
}

/** 执行身份由服务器当前head提供，排队消息不改变已开始事件的归属。 */
export function applyStreamAction(items: ChatTimelineItem[], action: StreamAction): ChatTimelineItem[] {
  const previous = new Map(items.map((item) => [item.id, item]))
  return applyStreamActionBody(items, action).map((item) =>
    item === previous.get(item.id)
      ? item
      : { ...item, origin: item.origin ?? 'stream', submissionId: action.submissionId ?? item.submissionId },
  )
}

/** 生成中受理的排队用户消息可以先显示，前一轮后来到达的活动仍显示在前一轮下方。 */
export function executionTimeline(items: ChatTimelineItem[]): ChatTimelineItem[] {
  const owners = new Set(
    items.filter((item) => item.kind === 'message' && item.role === 'user' && item.submissionId).map((item) => item.submissionId!),
  )
  const buckets = new Map<string, ChatTimelineItem[]>()
  const plain: ChatTimelineItem[] = []
  for (const item of items) {
    if (
      !(item.kind === 'message' && item.role === 'user') &&
      item.origin === 'stream' &&
      item.submissionId &&
      owners.has(item.submissionId)
    ) {
      const bucket = buckets.get(item.submissionId) ?? []
      bucket.push(item)
      buckets.set(item.submissionId, bucket)
    } else plain.push(item)
  }
  return plain.flatMap((item) =>
    item.kind === 'message' && item.role === 'user' && item.submissionId ? [item, ...(buckets.get(item.submissionId) ?? [])] : [item],
  )
}

function closeThinking(items: ChatTimelineItem[], id: string | null): ChatTimelineItem[] {
  const active = items.some((item) => item.kind === 'thinking' && item.streaming && (id === null || item.id === id))
  if (!active) return items
  return items
    .map((item) => (item.kind === 'thinking' && item.streaming && (id === null || item.id === id) ? { ...item, streaming: false } : item))
    .filter((item) => !(item.kind === 'thinking' && !item.text.trim()))
}

/** 结束态：所有流式气泡与思考块落定、空白的助手气泡直接去掉；工具卡片保持原状态。 */
export function finishMessageStreams(items: ChatTimelineItem[]): ChatTimelineItem[] {
  return items
    .map((item) => {
      if (item.kind === 'message' && item.streaming) return { ...item, streaming: false }
      if (item.kind === 'thinking' && item.streaming) return { ...item, streaming: false }
      return item
    })
    .filter((item) => !(item.kind === 'message' && item.role === 'assistant' && !item.text.trim()))
    .filter((item) => !(item.kind === 'thinking' && !item.text.trim()))
}

/** 整轮结束态：在消息落定的基础上，把还在转圈的工具卡片也停掉。 */
export function finishTimeline(items: ChatTimelineItem[]): ChatTimelineItem[] {
  return finishMessageStreams(items).map((item) => (item.kind === 'tool' && item.running ? { ...item, running: false } : item))
}

/* ----------------------------------------------------------- 展示辅助 */

/** MCP 工具名带 `mcp__server__` 前缀，摘要行只显示最后一段，完整名放 title */
export function shortToolName(name: string): string {
  const at = name.lastIndexOf('__')
  return at >= 0 && at + 2 < name.length ? name.slice(at + 2) : name
}

export function toolQuery(args: unknown): string {
  const rec = asRecord(args)
  if (!rec) return ''
  for (const key of ['query', 'memSearchContent', 'q', 'text', 'keyword', 'user_id', 'userId', 'external_user_id', 'externalUserId']) {
    const value = asString(rec[key])
    if (value?.trim()) return value.trim()
  }
  for (const key of ['userIds', 'user_ids']) {
    const value = rec[key]
    if (Array.isArray(value)) {
      const first = asString(value[0])
      if (first?.trim()) return first.trim()
    }
  }
  return ''
}

export function toolResultCount(result: unknown): number | undefined {
  if (Array.isArray(result)) return result.length
  const rec = asRecord(result)
  if (!rec) return undefined
  const total = asNumber(rec.total)
  if (total !== undefined) return total
  for (const key of ['records', 'memories', 'results', 'items', 'data']) {
    const value = rec[key]
    if (Array.isArray(value)) return value.length
    const nested = asRecord(value)
    if (!nested) continue
    const nestedTotal = asNumber(nested.total)
    if (nestedTotal !== undefined) return nestedTotal
    for (const nestedKey of ['records', 'memories', 'results', 'items']) {
      if (Array.isArray(nested[nestedKey])) return nested[nestedKey].length
    }
  }
  return undefined
}

export function previewValue(value: unknown, max = 2000): string {
  if (value === undefined || value === null) return ''
  if (typeof value === 'string') return value.length > max ? `${value.slice(0, max)}…` : value
  try {
    const text = JSON.stringify(value, null, 2)
    if (!text) return String(value)
    return text.length > max ? `${text.slice(0, max)}…` : text
  } catch {
    return String(value)
  }
}
