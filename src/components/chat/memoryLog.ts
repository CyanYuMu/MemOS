import type { MemoryLogActivity } from '../../lib/api'
import { isMemoryTool, parseMemoryResult, type MemoryEntry } from './memory'
import { asRecord, asString, shortToolName, toolQuery, type ChatToolItem } from './timeline'

export interface SubmittedMessage {
  role: string
  content: string
}

export interface MemoryLogRow {
  text: string
  entry?: MemoryEntry
  message?: SubmittedMessage
}

interface MemoryLogPresentation {
  titleKey: 'playground.logs.writeCall' | 'playground.logs.deleteCall' | 'playground.logs.updateCall' | 'playground.logs.memoryCall'
  status: 'running' | 'failed' | 'accepted' | 'done'
  rows: MemoryLogRow[]
  count: number | undefined
}

export function isMemoryOperation(name: string): boolean {
  return isMemoryTool(name) || ['add_message', 'delete_memory', 'update_memory', 'add_feedback'].includes(shortToolName(name))
}

function payloadOf(result: unknown): Record<string, unknown> | null {
  if (typeof result === 'string') {
    try {
      return payloadOf(JSON.parse(result))
    } catch {
      return null
    }
  }
  const rec = asRecord(result)
  if (!rec) return null
  if (Array.isArray(rec.content)) {
    for (const block of rec.content) {
      const text = asString(asRecord(block)?.text)
      if (text) {
        const parsed = payloadOf(text)
        if (parsed) return parsed
      }
    }
  }
  return asRecord(rec.data) ?? rec
}

export function memoryCallId(item: ChatToolItem): string | undefined {
  return asString(payloadOf(item.result)?.call_id)
}

export function memoryWriteCallId(item: ChatToolItem): string | undefined {
  return shortToolName(item.toolName) === 'add_message' ? asString(payloadOf(item.result)?.call_id) : undefined
}

export function memoryOperationTitle(item: ChatToolItem) {
  switch (shortToolName(item.toolName)) {
    case 'add_message':
      return 'playground.logs.writeDetail' as const
    case 'delete_memory':
      return 'playground.logs.deleteDetail' as const
    case 'update_memory':
    case 'add_feedback':
      return 'playground.logs.updateDetail' as const
    default:
      return 'playground.logs.searchDetail' as const
  }
}

/** 写入回执不是提取结果；摘要展示实际提交的对话，不把 call_id 或 deferred 当记忆。 */
export function submittedMessages(args: unknown): SubmittedMessage[] {
  const rec = asRecord(args)
  if (!rec) return []
  const messages = rec.messages
  if (!Array.isArray(messages)) return []
  return messages.flatMap((value) => {
    const message = asRecord(value)
    if (!message) return []
    const content =
      asString(message.content) ??
      (Array.isArray(message.content)
        ? message.content
            .map((block) => asString(asRecord(block)?.text) ?? '')
            .filter(Boolean)
            .join('\n')
        : '')
    return content.trim() ? [{ role: asString(message.role) ?? '', content }] : []
  })
}

export function memoryLogPresentation(item: ChatToolItem): MemoryLogPresentation {
  const name = shortToolName(item.toolName)
  const payload = payloadOf(item.result)
  const parsed = !item.running && !item.isError ? parseMemoryResult(item.result) : { parsed: false, entries: [] }
  const failed = item.isError || ['error', 'failed'].includes(asString(payload?.status)?.toLowerCase() ?? '')
  const status = item.running ? 'running' : failed ? 'failed' : payload?.deferred === true ? 'accepted' : 'done'
  const titleKey =
    name === 'add_message'
      ? 'playground.logs.writeCall'
      : name === 'delete_memory'
        ? 'playground.logs.deleteCall'
        : ['update_memory', 'add_feedback'].includes(name)
          ? 'playground.logs.updateCall'
          : 'playground.logs.memoryCall'
  let rows: MemoryLogRow[] = parsed.entries.map((entry) => ({
    text: entry.key ? `${entry.key}: ${entry.value}` : entry.value,
    entry,
  }))
  if (!rows.length && name === 'add_message') {
    rows = submittedMessages(item.args).map((message) => ({ text: message.content, message }))
  }
  if (!rows.length && isMemoryTool(item.toolName)) {
    const query = toolQuery(item.args)
    if (query) rows = [{ text: query }]
  }
  if (!rows.length && name === 'delete_memory' && Array.isArray(payload?.deleted_ids)) {
    rows = payload.deleted_ids.filter((id): id is string => typeof id === 'string').map((id) => ({ text: id }))
  }
  if (!rows.length && name === 'add_feedback') {
    const content = asString(asRecord(item.args)?.feedback_content)
    if (content) rows = [{ text: content }]
  }
  return {
    titleKey,
    status,
    rows,
    count: failed
      ? undefined
      : name === 'update_memory' && payload?.changed === false
        ? 0
        : name === 'delete_memory' && Array.isArray(payload?.deleted_ids)
          ? payload.deleted_ids.length
          : parsed.parsed
            ? parsed.entries.length
            : undefined,
  }
}

/** 多个事件保留独立身份；操作详情优先使用最新的真实事件，不被审计覆盖。 */
export function memoryActivityForCall(records: Record<string, MemoryLogActivity>, callId: string | undefined) {
  if (!callId) return undefined
  const rows = Object.values(records).filter((row) => row.callId === callId)
  return rows.reduce<MemoryLogActivity | undefined>((selected, row) => {
    if (!selected) return row
    if ('eventId' in row && (!('eventId' in selected) || BigInt(row.sequence) > BigInt(selected.sequence))) return row
    return selected
  }, undefined)
}
