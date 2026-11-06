// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { api, type MemoryActivity, type MemoryEvent } from '../../lib/api'
import { mergeMemoryActivities, useMemoryActivity } from './useMemoryActivity'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it('切换项目时立即隐藏前一项目的记忆活动', async () => {
  const row: MemoryActivity = {
    callId: 'project-a-write',
    status: 'succeeded',
    completedAt: '2026-10-31T12:00:00Z',
    memories: [],
    total: 0,
    truncated: false,
  }
  vi.spyOn(api.memory, 'events').mockImplementation((projectId) =>
    projectId === 'project-a'
      ? Promise.resolve({
          records: [{ ...row, eventId: 'event-a', sequence: '1', kind: 'add' as const, sessionId: '', before: [], memoryIds: [] }],
          total: 1,
          cursor: '',
          hasMore: false,
        })
      : new Promise(() => {}),
  )
  const hook = renderHook(({ projectId }) => useMemoryActivity(projectId, []), { initialProps: { projectId: 'project-a' } })
  await waitFor(() => expect(hook.result.current['event-a']).toMatchObject(row))
  hook.rerender({ projectId: 'project-b' })
  expect(hook.result.current).toEqual({})
})

function event(sequence: number, callId = 'same-call'): MemoryEvent {
  return {
    eventId: `event-${sequence}`,
    sequence: String(sequence),
    kind: 'add',
    callId,
    sessionId: 'chat-a',
    status: 'succeeded',
    completedAt: '2026-10-31T12:00:00Z',
    memories: [],
    before: [],
    memoryIds: [],
    total: 0,
    truncated: false,
  }
}

it('晚到审计不能覆盖历史事件，同一调用的多个事件保留独立身份', () => {
  const first = event(1)
  const second = event(2)
  const late: MemoryActivity = {
    callId: first.callId,
    status: 'succeeded',
    completedAt: first.completedAt,
    memories: [],
    total: 0,
    truncated: false,
  }
  const merged = mergeMemoryActivities({ [first.eventId]: first }, [late, second])
  expect(Object.keys(merged)).toHaveLength(2)
  expect(merged[first.eventId]).toEqual(first)
  expect(merged[second.eventId]).toEqual(second)
})

it('同时间戳第81条按序号保留，不因80条窗口丢失', () => {
  const old = Object.fromEntries(
    Array.from({ length: 80 }, (_, i) => {
      const row = event(i + 1)
      return [row.eventId, row]
    }),
  )
  const newest = event(81)
  const merged = mergeMemoryActivities(old, [newest])
  expect(Object.values(merged)).toHaveLength(80)
  expect(merged[newest.eventId]).toEqual(newest)
  expect(merged['event-1']).toBeUndefined()
})

it('已加载历史调用的快照不被最近80条窗口挤掉', () => {
  const rows = Array.from({ length: 90 }, (_, i) => event(i + 1, `call-${i + 1}`))
  const merged = mergeMemoryActivities({}, rows, ['call-1'])
  expect(merged['event-1']).toEqual(rows[0])
  expect(merged['event-2']).toBeUndefined()
  expect(Object.values(merged)).toHaveLength(81)
})

it('刷新旧Search和Profile按真实调用读取持久化七字段，不重置feed游标', async () => {
  const old = { ...event(1, 'profile-old'), kind: 'recall' as const }
  const feed = vi.spyOn(api.memory, 'events').mockImplementation((_p, _after, ids) =>
    Promise.resolve({
      records: ids?.includes('profile-old') ? [old] : [],
      total: ids ? 1 : 0,
      cursor: ids ? '' : 'live-cursor',
      hasMore: false,
    }),
  )
  const tool = {
    kind: 'tool' as const,
    id: 'p',
    toolCallId: 'p',
    toolName: 'get_user_profile',
    args: {},
    result: { call_id: 'profile-old' },
    running: false,
    isError: false,
  }
  const hook = renderHook(() => useMemoryActivity('project-a', [tool]))
  await waitFor(() => expect(hook.result.current['event-1']).toEqual(old))
  expect(feed).toHaveBeenCalledWith('project-a', undefined, ['profile-old'])
})

it('新的待查调用不会把已追踪的feed游标重置到首屏', async () => {
  vi.useFakeTimers()
  const feed = vi
    .spyOn(api.memory, 'events')
    .mockResolvedValueOnce({ records: [], total: 0, cursor: 'cursor-1', hasMore: false })
    .mockResolvedValue({ records: [], total: 0, cursor: 'cursor-2', hasMore: false })
  vi.spyOn(api.memory, 'activity').mockResolvedValue({ records: [], total: 0, cursor: '' })
  const tool = {
    kind: 'tool' as const,
    id: 'tool',
    toolCallId: 'tool-call',
    toolName: 'add_message',
    args: {},
    result: { call_id: 'new-call', deferred: true },
    running: false,
    isError: false,
  }
  const hook = renderHook(({ items }) => useMemoryActivity('project-a', items), { initialProps: { items: [] as (typeof tool)[] } })
  await act(async () => {})
  hook.rerender({ items: [tool] })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000)
  })
  expect(feed).toHaveBeenCalledWith('project-a', 'cursor-1')
  expect(feed.mock.calls.filter((args) => !args[2])).toHaveLength(2)
})

it('旧快照请求失败后不改变items也会重试，且不回退feed游标', async () => {
  vi.useFakeTimers()
  const old = { ...event(1, 'search-old'), kind: 'recall' as const }
  let lookups = 0
  const feed = vi.spyOn(api.memory, 'events').mockImplementation((_p, _after, ids) => {
    if (ids && ++lookups === 1) return Promise.reject(new Error('temporary 502'))
    return Promise.resolve({ records: ids ? [old] : [], total: ids ? 1 : 0, cursor: ids ? '' : 'live-cursor', hasMore: false })
  })
  const tool = {
    kind: 'tool' as const,
    id: 's',
    toolCallId: 's',
    toolName: 'search_memory',
    args: {},
    result: { call_id: 'search-old' },
    running: false,
    isError: false,
  }
  const hook = renderHook(() => useMemoryActivity('project-a', [tool]))
  await act(async () => {})
  expect(hook.result.current['event-1']).toBeUndefined()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2100)
  })
  expect(hook.result.current['event-1']).toEqual(old)
  expect(lookups).toBe(2)
  expect(feed).toHaveBeenCalledWith('project-a', 'live-cursor')
})
