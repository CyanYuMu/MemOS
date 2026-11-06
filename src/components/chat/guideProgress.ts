import type { MemoryLogActivity } from '../../lib/api'
import { isMemoryTool } from './memory'
import { memoryActivityForCall, memoryCallId, memoryLogPresentation } from './memoryLog'
import { shortToolName, type ChatTimelineItem } from './timeline'

export interface GuideProgress {
  conversationId: string
  ready: boolean
  busy: boolean
  needsRetry: boolean
  hasAnswer: boolean
  error: string | null
  write: 'none' | 'pending' | 'succeeded' | 'failed' | 'unavailable'
  written: number
  recall: 'none' | 'pending' | 'succeeded' | 'failed'
  recalled: number
}

export function guideMemoryProgress(timeline: ChatTimelineItem[], activities: Record<string, MemoryLogActivity>) {
  let write: GuideProgress['write'] = 'none',
    recall: GuideProgress['recall'] = 'none',
    written = 0
  const recalledEntries = new Set<string>()
  let unidentified = 0
  for (const item of timeline) {
    if (item.kind !== 'tool') continue
    const name = shortToolName(item.toolName),
      view = memoryLogPresentation(item)
    if (name === 'add_message') {
      const event = memoryActivityForCall(activities, memoryCallId(item))
      if (event?.status === 'succeeded') {
        write = 'succeeded'
        written += event.total
      } else if (event?.status === 'failed' || view.status === 'failed') write = 'failed'
      else if (event?.status === 'unavailable') write = 'unavailable'
      else write = 'pending'
    }
    if (isMemoryTool(item.toolName)) {
      if (view.status === 'failed') recall = 'failed'
      else if (view.status === 'running') recall = 'pending'
      else if (view.count !== undefined) {
        recall = 'succeeded'
        const entries = view.rows.flatMap((row) => (row.entry ? [row.entry] : []))
        if (!entries.length) unidentified += view.count
        for (const entry of entries) {
          if (entry.memoryId) recalledEntries.add(JSON.stringify([entry.memoryId, entry.value]))
          else unidentified += 1
        }
      }
    }
  }
  const recalled = recalledEntries.size + unidentified
  return { write, written, recall, recalled }
}
