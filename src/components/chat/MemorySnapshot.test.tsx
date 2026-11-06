// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LangProvider } from '../../lib/store'
import { api, type Memory } from '../../lib/api'
import { MemorySnapshot } from './MemorySnapshot'
import { entryFromMemory } from './memory'
import type { TurnSnapshot } from './turnSnapshot'

const record: Memory = {
  memoryId: 'historical-id',
  memoryKey: '饮食偏好',
  memoryContent: '过去喜欢茶',
  createTime: '2026-10-31 12:00:00',
  updateTime: '2026-10-31 12:00:00',
  subjectType: 'user',
  externalUserId: 'u',
  memoryType: 'UserMemory',
  status: 'activated',
  tags: ['茶'],
  version: 1,
  relativity: 0.673,
}
function page(snapshot: TurnSnapshot) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <LangProvider>
        <MemorySnapshot projectId="p" snapshot={snapshot} />
      </LangProvider>
    </QueryClientProvider>,
  )
}
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
it('快照在气泡下方内联展开，卡片详情保留七字段，不请求当前版本', async () => {
  const detail = vi.spyOn(api.memory, 'detail')
  page({ status: 'available', partial: false, entries: [{ entry: entryFromMemory(record), record, query: '茶' }] })
  const trigger = screen.getByRole('button', { name: /记忆快照/ })
  fireEvent.click(trigger)
  expect(trigger.getAttribute('aria-expanded')).toBe('true')
  expect(screen.queryByRole('dialog')).toBeNull()
  const card = within(screen.getByRole('article'))
  for (const value of ['historical-id', '饮食偏好', '过去喜欢茶', 'UserMemory', 'activated', '茶', '0.67', '1'])
    expect(card.getByText(value)).toBeTruthy()
  expect(card.getAllByText('2026-10-31')).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: '展开' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText('historical-id')).toBeTruthy()
  for (const field of ['ID', 'Key', 'value', 'memory_type', 'status', 'tags', 'updated_at'])
    expect(screen.getByRole('rowheader', { name: field })).toBeTruthy()
  expect(within(dialog).getByText('过去喜欢茶')).toBeTruthy()
  expect(detail).not.toHaveBeenCalled()
  fireEvent.click(screen.getAllByRole('button', { name: '关闭' })[0])
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByRole('button', { name: '展开' })).toBeTruthy()
  fireEvent.click(trigger)
  expect(trigger.getAttribute('aria-expanded')).toBe('false')
  expect(screen.queryByRole('article')).toBeNull()
})
it('旧摘要不冒充完整实体，明确缺失且不读取当前记忆', () => {
  const detail = vi.spyOn(api.memory, 'detail')
  page({ status: 'available', partial: false, entries: [{ entry: entryFromMemory(record), query: '茶' }] })
  fireEvent.click(screen.getByRole('button', { name: /记忆快照/ }))
  expect(screen.getByText('完整字段未保留')).toBeTruthy()
  expect(screen.queryByRole('button', { name: '展开' })).toBeNull()
  expect(detail).not.toHaveBeenCalled()
})

it('按6张递增展示，真实总数不变，收起后重开恢复首批', () => {
  const detail = vi.spyOn(api.memory, 'detail')
  const entries = Array.from({ length: 13 }, (_, i) => {
    const row = { ...record, memoryId: `m-${i}` }
    return { entry: entryFromMemory(row), record: row, query: '' }
  })
  page({ status: 'available', partial: false, entries })
  const trigger = screen.getByRole('button', { name: /记忆快照/ })
  expect(trigger.textContent).toContain('共13个')
  fireEvent.click(trigger)
  expect(screen.getAllByRole('article')).toHaveLength(6)
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  expect(screen.getAllByRole('article')).toHaveLength(12)
  fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
  expect(screen.getAllByRole('article')).toHaveLength(13)
  expect(screen.queryByRole('button', { name: '加载更多' })).toBeNull()
  fireEvent.click(trigger)
  fireEvent.click(trigger)
  expect(screen.getAllByRole('article')).toHaveLength(6)
  expect(detail).not.toHaveBeenCalled()
})
it('画像不显示虚构的relativity，快照版本纳入表格说明', async () => {
  page({
    status: 'available',
    partial: false,
    entries: [
      { entry: { ...entryFromMemory(record), relativity: undefined }, record, query: '', origins: [{ kind: 'profile', callId: 'p' }] },
    ],
  })
  fireEvent.click(screen.getByRole('button', { name: /记忆快照/ }))
  expect(screen.queryByText(/relativity/)).toBeNull()
  expect(screen.getAllByText(/画像上下文/).length).toBeGreaterThan(0)
  fireEvent.click(screen.getByRole('button', { name: '展开' }))
  const dialog = await screen.findByRole('dialog')
  const version = within(dialog).getByText('快照版本 V1')
  expect(version.closest('caption')).toBeTruthy()
  expect(within(dialog).queryByText(/当前版本/)).toBeNull()
})
