import { expect, it } from 'vitest'
import { turnSnapshots } from './turnSnapshot'
import { guideMemoryProgress } from './guideProgress'
import { applyStreamAction, executionTimeline, parseChatEvent } from './timeline'
import type { ChatTimelineItem, ChatToolItem } from './timeline'
import type { MemoryEvent } from '../../lib/api'

const recall: ChatToolItem = {
  kind: 'tool',
  origin: 'history',
  id: 'search-1',
  toolCallId: 'tool-1',
  toolName: 'search_memory',
  args: { query: 'tea' },
  result: { call_id: 'call-1', memory_detail_list: [{ memory_id: 'm1', memory_key: 'drink', memory_value: 'old tea' }] },
  running: false,
  isError: false,
  timestamp: 9999,
}
const event: MemoryEvent = {
  eventId: 'event-1',
  sequence: '1',
  kind: 'recall',
  callId: 'call-1',
  sessionId: 'session-1',
  status: 'succeeded',
  completedAt: '2026-10-31T12:00:00Z',
  total: 1,
  truncated: false,
  memoryIds: ['m1'],
  before: [],
  memories: [
    {
      memoryId: 'm1',
      memoryKey: 'drink',
      memoryContent: 'old tea',
      createTime: '2026-10-31 12:00:00',
      subjectType: 'user',
      externalUserId: 'user-1',
      memoryType: 'UserMemory',
      status: 'activated',
      tags: ['tea'],
      version: 1,
    },
  ],
}
const user = (id: string): ChatTimelineItem => ({
  kind: 'message',
  id,
  role: 'user',
  text: 'question',
  streaming: false,
  origin: 'history',
})

it('按实际轮次与call_id关联，不采用相同文本或相近时间的其他事件', () => {
  const timeline = [
    user('first'),
    recall,
    user('second'),
    { ...recall, id: 'search-2', toolCallId: 'tool-2', result: { call_id: 'call-2', memory_detail_list: [] }, timestamp: 1 },
  ]
  const snapshots = turnSnapshots(timeline, {
    'event-1': event,
    foreign: { ...event, eventId: 'foreign', callId: 'unrelated', memories: [{ ...event.memories[0], memoryContent: 'future coffee' }] },
  })
  expect(snapshots.get('first')?.entries[0].record?.memoryContent).toBe('old tea')
  expect(snapshots.get('second')).toMatchObject({ status: 'available', entries: [], partial: false })
  expect(turnSnapshots([recall, user('after')], { 'event-1': event }).get('after')?.status).toBe('unavailable')
})

it('同轮重复命中去重，缺失实体不显示完整零条，失败不当成功', () => {
  const both = turnSnapshots([user('u'), recall, { ...recall, id: 'again' }], { 'event-1': event }).get('u')!
  expect(both.entries).toHaveLength(1)
  const partial = turnSnapshots([user('u'), recall], { 'event-1': { ...event, memories: [], truncated: true } }).get('u')!
  expect(partial).toMatchObject({ status: 'available', entries: [], partial: true })
  expect(turnSnapshots([user('u'), { ...recall, isError: true }], {}).get('u')).toMatchObject({ status: 'unavailable', entries: [] })
})

it('历史原始结果只保留实际摘要，不用当前实体填补完整字段', () => {
  const snapshot = turnSnapshots([user('u'), recall], {}).get('u')!
  expect(snapshot.entries[0].entry.value).toBe('old tea')
  expect(snapshot.entries[0].record).toBeUndefined()
})

it('相关度仅取本次同ID同正文结果，画像去重不覆盖实际搜索分数', () => {
  const ranked = {
    ...recall,
    result: { call_id: 'call-1', memory_detail_list: [{ memory_id: 'm1', memory_value: 'old tea', relativity: 0.673 }] },
  }
  const profile = { ...recall, id: 'profile', toolName: 'get_user_profile' }
  const actual = turnSnapshots([user('u'), ranked, profile], { 'event-1': event }).get('u')!
  expect(actual.entries[0].entry.relativity).toBe(0.673)
  const changed = {
    ...ranked,
    result: { ...ranked.result, memory_detail_list: [{ memory_id: 'm1', memory_value: 'future coffee', relativity: 0.99 }] },
  }
  expect(turnSnapshots([user('u'), changed], { 'event-1': event }).get('u')!.entries[0].entry.relativity).toBeUndefined()
})

it('引导等待真实提取终态，不把受理或别的调用的完成当成记住', () => {
  const write: ChatToolItem = {
    ...recall,
    id: 'write',
    toolName: 'add_message',
    result: { call_id: 'write-1', deferred: true, status: 'success' },
  }
  expect(guideMemoryProgress([write], { 'event-1': event }).write).toBe('pending')
  expect(guideMemoryProgress([write], { written: { ...event, kind: 'add', callId: 'write-1', total: 3 } })).toMatchObject({
    write: 'succeeded',
    written: 3,
  })
  expect(
    guideMemoryProgress([write], { failed: { ...event, kind: 'add', callId: 'write-1', status: 'failed', total: 0, memories: [] } }).write,
  ).toBe('failed')
})

it('A执行时排队B，后来的A检索仍归A；没有执行ID不猜B', () => {
  const a = { ...user('u-a'), origin: 'stream' as const, submissionId: 'accepted-a' }
  const b = { ...user('u-b'), origin: 'stream' as const, submissionId: 'accepted-b' }
  const queued = [a, b]
  const updated = applyStreamAction(queued, {
    kind: 'tool_end',
    seed: '1',
    submissionId: 'accepted-a',
    data: { toolCallId: 'tool-1', toolName: 'search_memory', result: recall.result },
  })
  expect(updated[2]).toMatchObject({ origin: 'stream', submissionId: 'accepted-a' })
  expect(executionTimeline(updated).map((item) => item.id)).toEqual(['u-a', 'tool_tool-1', 'u-b'])
  const snapshots = turnSnapshots(updated, { 'event-1': event })
  expect(snapshots.get('u-a')?.entries[0].record?.memoryId).toBe('m1')
  expect(snapshots.get('u-b')?.status).toBe('unavailable')
  const noOwner = updated.map((item) => (item.kind === 'tool' ? { ...item, submissionId: undefined } : item))
  expect(turnSnapshots(noOwner, { 'event-1': event }).get('u-b')?.entries).toEqual([])
  expect(parseChatEvent(JSON.stringify({ seq: 1, type: 'tool_execution_end', submissionId: 'accepted-a', data: {} }))?.submissionId).toBe(
    'accepted-a',
  )
})

it('画像读取也属于本轮快照，和搜索重复的实体不重复计数', () => {
  const profile: ChatToolItem = {
    ...recall,
    id: 'profile',
    toolCallId: 'profile-tool',
    toolName: 'get_user_profile',
    result: {
      call_id: 'profile-call',
      memory_detail_list: [
        { memory_id: 'm1', memory_value: 'old tea' },
        { memory_id: 'm2', memory_value: '河南' },
      ],
    },
  }
  const profileEvent: MemoryEvent = {
    ...event,
    eventId: 'profile-event',
    callId: 'profile-call',
    total: 2,
    memories: [event.memories[0], { ...event.memories[0], memoryId: 'm2', memoryContent: '河南' }],
    memoryIds: ['m1', 'm2'],
  }
  const records = { search: event, profile: profileEvent }
  const snapshot = turnSnapshots([user('u'), recall, profile], records).get('u')!
  expect(snapshot.entries).toHaveLength(2)
  expect(snapshot.entries.find((row) => row.entry.memoryId === 'm2')?.record?.memoryContent).toBe('河南')
  expect(guideMemoryProgress([recall, profile], records).recalled).toBe(2)
})

it('无评分画像的原始零值不冒充搜索相关度', () => {
  const profile = {
    ...recall,
    toolName: 'get_user_profile',
    result: { call_id: 'call-1', memory_detail_list: [{ memory_id: 'm1', memory_value: 'old tea', relativity: 0 }] },
  }
  expect(turnSnapshots([user('u'), profile], {}).get('u')!.entries[0].entry.relativity).toBeUndefined()
  expect(turnSnapshots([user('u'), profile], { 'event-1': event }).get('u')!.entries[0].entry.relativity).toBeUndefined()
})

it('保留10条检索加27条画像去重后的31个实体及每个真实调用归属', () => {
  const searchRows = Array.from({ length: 10 }, (_, i) => ({
    ...event.memories[0],
    memoryId: `s-${i}`,
    memoryContent: `value-${i}`,
    relativity: 0.9 - i * 0.01,
  }))
  const profileRows = [
    ...searchRows.slice(0, 6).map((m) => ({ ...m, relativity: undefined })),
    ...Array.from({ length: 21 }, (_, i) => ({ ...event.memories[0], memoryId: `p-${i}`, memoryContent: `profile-${i}` })),
  ]
  const profile = {
    ...recall,
    id: 'profile',
    toolCallId: 'profile-tool',
    toolName: 'get_user_profile',
    result: { call_id: 'profile-call', memory_detail_list: [] },
  }
  const snap = turnSnapshots([user('u'), recall, profile], {
    search: { ...event, memories: searchRows, total: 10 },
    profile: { ...event, eventId: 'profile-event', callId: 'profile-call', memories: profileRows, total: 27 },
  }).get('u')!
  expect(snap.entries).toHaveLength(31)
  expect(snap.entries.filter((e) => e.origins?.some((o) => o.kind === 'search'))).toHaveLength(10)
  expect(snap.entries[0].origins?.map(({ kind, callId }) => ({ kind, callId }))).toEqual([
    { kind: 'search', callId: 'call-1' },
    { kind: 'profile', callId: 'profile-call' },
  ])
  expect(snap.entries[0].entry.relativity).toBe(0.9)
  expect(snap.entries.at(-1)?.entry.relativity).toBeUndefined()
})
