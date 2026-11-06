import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type MemoryLogActivity } from '../../lib/api'
import { memoryCallId, memoryWriteCallId } from './memoryLog'
import type { ChatToolItem } from './timeline'

/** 业务序号优先，时间相同的晚到事件也不能被旧80条挤掉。 */
function newestFirst(a: MemoryLogActivity, b: MemoryLogActivity) {
  if ('eventId' in a && 'eventId' in b) {
    const left = BigInt(a.sequence)
    const right = BigInt(b.sequence)
    if (left !== right) return left > right ? -1 : 1
  }
  return Date.parse(b.completedAt) - Date.parse(a.completedAt) || 0
}

export function mergeMemoryActivities(previous: Record<string, MemoryLogActivity>, rows: MemoryLogActivity[], pinnedCalls: string[] = []) {
  const merged = { ...previous }
  for (const row of rows) {
    if ('eventId' in row) {
      for (const [key, old] of Object.entries(merged)) {
        if (!('eventId' in old) && old.callId === row.callId) delete merged[key]
      }
      merged[row.eventId] = row
    } else if (!Object.values(merged).some((old) => 'eventId' in old && old.callId === row.callId)) {
      merged[`legacy:${row.callId}`] = row
    }
  }
  const pinned = new Set(pinnedCalls)
  return Object.fromEntries(
    Object.values(merged)
      .sort(newestFirst)
      .filter((row, i) => i < 80 || pinned.has(row.callId))
      .map((row) => ['eventId' in row ? row.eventId : `legacy:${row.callId}`, row]),
  )
}

/** 游标只跟随项目；新工具调用只更新待查ID，不重置增量读取。 */
export function useMemoryActivity(projectId: string, items: ChatToolItem[], enabled = true) {
  const [snapshot, setSnapshot] = useState<{ projectId: string; records: Record<string, MemoryLogActivity> }>({ projectId, records: {} })
  const requests = useRef({ projectId, ids: [] as string[], pinned: [] as string[] })
  const historyKey = useMemo(() => JSON.stringify([...new Set(items.map(memoryCallId).filter((id): id is string => !!id))]), [items])
  const key = useMemo(
    () => JSON.stringify([...new Set(items.map(memoryWriteCallId).filter((id): id is string => !!id))].slice(-20)),
    [items],
  )
  useEffect(() => {
    requests.current = { projectId, ids: JSON.parse(key) as string[], pinned: JSON.parse(historyKey) as string[] }
  }, [projectId, key, historyKey])
  useEffect(() => {
    if (!enabled) return
    let active = true
    let retry: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const ids = JSON.parse(historyKey) as string[]
    async function loadHistory(start = 0) {
      for (let i = start; active && i < ids.length; i += 20) {
        try {
          const result = await api.memory.events(projectId, undefined, ids.slice(i, i + 20))
          if (!active) return
          failures = 0
          setSnapshot((previous) => ({
            projectId,
            records: mergeMemoryActivities(previous.projectId === projectId ? previous.records : {}, result.records, ids),
          }))
        } catch {
          // 只重试读取失败的批次，不能靠实时游标找回旧序号。
          if (active) retry = setTimeout(() => void loadHistory(i), Math.min(10000, 1000 * 2 ** Math.min(failures++, 4)))
          return
        }
      }
    }
    void loadHistory()
    return () => {
      active = false
      clearTimeout(retry)
    }
  }, [projectId, historyKey, enabled])
  useEffect(() => {
    if (!enabled) return
    let cursor = ''
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      if (!active) return
      let more = false
      const ids = requests.current.projectId === projectId ? [...requests.current.ids] : []
      try {
        const [feed, targeted] = await Promise.all([
          api.memory.events(projectId, cursor),
          ids.length ? api.memory.activity(projectId, ids) : Promise.resolve({ records: [] as MemoryLogActivity[] }),
        ])
        if (!active) return
        cursor = feed.cursor
        more = feed.hasMore
        setSnapshot((previous) => ({
          projectId,
          records: mergeMemoryActivities(
            previous.projectId === projectId ? previous.records : {},
            [...targeted.records, ...feed.records],
            requests.current.pinned,
          ),
        }))
        const pending = new Set(
          targeted.records.filter((row) => row.status === 'pending' || row.status === 'not_found').map((row) => row.callId),
        )
        const confirmed = new Set(feed.records.map((row) => row.callId))
        if (requests.current.projectId === projectId)
          requests.current.ids = requests.current.ids.filter((id) => !ids.includes(id) || (pending.has(id) && !confirmed.has(id)))
      } catch {
        // 保留已确认快照和原游标，读取失败不触发提取或跳过未读事件。
      }
      if (active) timer = setTimeout(() => void poll(), more ? 0 : document.visibilityState === 'hidden' ? 10000 : 2000)
    }
    void poll()
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [projectId, enabled])
  return snapshot.projectId === projectId ? snapshot.records : {}
}
