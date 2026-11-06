import type { Memory, MemoryLogActivity } from '../../lib/api'
import { entryFromMemory, isMemoryTool, parseMemoryResult, type MemoryEntry } from './memory'
import { memoryCallId, memoryActivityForCall, memoryLogPresentation } from './memoryLog'
import { toolQuery, type ChatTimelineItem } from './timeline'

export interface TurnSnapshot {
  entries: {
    entry: MemoryEntry
    record?: Memory
    query: string
    origins?: { kind: 'search' | 'profile'; callId?: string; entry?: MemoryEntry; record?: Memory }[]
  }[]
  status: 'available' | 'pending' | 'unavailable'
  partial: boolean
}

/** 只用时间线中的实际工具调用归属轮次，不按时间或文字猜记忆关联。 */
export function turnSnapshots(timeline: ChatTimelineItem[], activities: Record<string, MemoryLogActivity>) {
  const result = new Map<string, TurnSnapshot>()
  const bySubmission = new Map<string, TurnSnapshot>()
  for (const item of timeline) {
    if (item.kind !== 'message' || item.role !== 'user') continue
    const snapshot: TurnSnapshot = { entries: [], status: 'unavailable', partial: false }
    result.set(item.id, snapshot)
    if (item.submissionId) bySubmission.set(item.submissionId, snapshot)
  }
  let historySnapshot: TurnSnapshot | undefined
  for (const item of timeline) {
    if (item.kind === 'message' && item.role === 'user') {
      if (item.origin === 'history') historySnapshot = result.get(item.id)
      continue
    }
    const snapshot =
      item.submissionId && bySubmission.has(item.submissionId)
        ? bySubmission.get(item.submissionId)
        : item.origin === 'history'
          ? historySnapshot
          : undefined
    if (!snapshot || item.kind !== 'tool' || !isMemoryTool(item.toolName)) continue
    if (item.running) {
      snapshot.status = 'pending'
      continue
    }
    if (memoryLogPresentation(item).status === 'failed') {
      snapshot.partial = true
      continue
    }
    const event = memoryActivityForCall(activities, memoryCallId(item))
    const query = toolQuery(item.args)
    const ranked = item.toolName.endsWith('search_memory')
    const origins = [{ kind: ranked ? ('search' as const) : ('profile' as const), callId: memoryCallId(item) }]
    if (event && 'eventId' in event && 'kind' in event && event.kind === 'recall' && event.status === 'succeeded') {
      snapshot.status = snapshot.status === 'pending' ? 'pending' : 'available'
      snapshot.partial ||= event.truncated || event.total > event.memories.length
      const returned = parseMemoryResult(item.result).entries
      for (const record of event.memories) {
        const entry = entryFromMemory(record)
        // 旧事件投影可能未携带分数，只允许从该次同ID同正文的工具结果恢复。
        if (ranked) entry.relativity ??= returned.find((row) => row.memoryId === entry.memoryId && row.value === entry.value)?.relativity
        if (!ranked) entry.relativity = undefined
        snapshot.entries.push({ entry, record, query, origins: origins.map((origin) => ({ ...origin, entry: { ...entry }, record })) })
      }
    } else {
      const parsed = parseMemoryResult(item.result)
      if (!parsed.parsed) {
        snapshot.partial = true
        continue
      }
      snapshot.status = snapshot.status === 'pending' ? 'pending' : 'available'
      for (const entry of parsed.entries) {
        if (!ranked) entry.relativity = undefined
        snapshot.entries.push({ entry, query, origins: origins.map((origin) => ({ ...origin, entry: { ...entry } })) })
      }
    }
  }
  for (const snapshot of result.values()) {
    const unique = new Map<string, TurnSnapshot['entries'][number]>()
    for (const row of snapshot.entries) {
      const key = JSON.stringify([row.entry.memoryId, row.entry.value])
      const previous = unique.get(key)
      if (!previous) unique.set(key, row)
      else {
        const selected = row.record ? row : previous
        selected.entry.relativity = previous.entry.relativity ?? row.entry.relativity
        selected.query ||= previous.query
        selected.origins = [
          ...new Map(
            [...(previous.origins ?? []), ...(row.origins ?? [])].map((origin) => [JSON.stringify([origin.kind, origin.callId]), origin]),
          ).values(),
        ]
        unique.set(key, selected)
      }
    }
    snapshot.entries = [...unique.values()].sort((a, b) => Number(isSearchSnapshotEntry(b)) - Number(isSearchSnapshotEntry(a)))
  }
  return result
}

/** 画像没有查询评分；只有真实搜索来源才显示相关度。 */
export function isSearchSnapshotEntry(row: TurnSnapshot['entries'][number]) {
  return row.origins ? row.origins.some((origin) => origin.kind === 'search') : row.entry.relativity !== undefined
}
