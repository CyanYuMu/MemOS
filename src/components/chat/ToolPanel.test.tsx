// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LangProvider } from '../../lib/store'
import { api, type Memory, type MemoryActivity, type MemoryLogActivity } from '../../lib/api'
import { TooltipProvider } from '../ui/tooltip'
import { ToolPanel } from './ToolPanel'
import { MemoryDetailFields } from './MemoryDetailFields'
import { entryFromMemory } from './memory'
import type { ChatToolItem } from './timeline'
import { memoryCallId, memoryLogPresentation } from './memoryLog'

const activity = vi.hoisted(() => ({ records: {} as Record<string, MemoryLogActivity> }))
vi.mock('./useMemoryActivity', () => ({ useMemoryActivity: () => activity.records }))

const memory: Memory = {
  memoryId: 'memory-actual',
  memoryKey: '饮食偏好',
  memoryContent: '用户喜欢川菜',
  subjectType: 'user',
  externalUserId: 'test-user',
  createTime: '2026-10-30 12:00:00',
  updateTime: '2026-10-31 12:00:00',
  memoryType: 'UserMemory',
  status: 'activated',
  tags: ['川菜'],
  version: 2,
  history: [{ version: 1, content: '用户喜欢湘菜', createdAt: '2026-10-30 12:00:00' }],
  sources: [{ type: 'chat', role: 'user', content: '请记住我喜欢川菜' }],
}
const write: ChatToolItem = {
  kind: 'tool',
  id: 'write-1',
  toolCallId: 'tool-1',
  toolName: 'add_message',
  args: { messages: [{ role: 'user', content: '请记住我喜欢川菜' }] },
  result: { call_id: 'extract-1', deferred: true, status: 'success' },
  running: false,
  isError: false,
  timestamp: '2026-10-31T12:00:00Z',
}
const linked: MemoryActivity = {
  callId: 'extract-1',
  status: 'succeeded',
  completedAt: '2026-10-31T12:00:01Z',
  memories: [memory],
  total: 1,
  truncated: false,
}
function panel(items: ChatToolItem[] = [write]) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { gcTime: 0 } } })}>
      <LangProvider>
        <TooltipProvider>
          <ToolPanel projectId="test-project" items={items} onLocate={vi.fn()} open={false} onClose={vi.fn()} />
        </TooltipProvider>
      </LangProvider>
    </QueryClientProvider>,
  )
}
beforeEach(() => {
  activity.records = {}
  localStorage.clear()
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
  vi.spyOn(api.memory, 'detail').mockResolvedValue(memory)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('日志展开的对象与记忆关联', () => {
  it('同调用多事件按eventId使用独立React身份', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    const base = { ...linked, kind: 'add' as const, sessionId: 'chat-a', before: [], memoryIds: [memory.memoryId] }
    const first = { ...base, eventId: 'event-one', sequence: '1' }
    const second = { ...base, eventId: 'event-two', sequence: '2' }
    activity.records = { 'event-one': first, 'event-two': second }
    panel([])
    expect(document.querySelectorAll('.memos-log-event')).toHaveLength(2)
    expect(warn.mock.calls.some((args) => args.join(' ').includes('same key'))).toBe(false)
  })

  it('从对话写入详情进入关联记忆时仍使用当时快照', async () => {
    const event = {
      ...linked,
      eventId: 'add-event',
      sequence: '9',
      kind: 'add' as const,
      sessionId: 'chat-a',
      before: [],
      memoryIds: [memory.memoryId],
    }
    activity.records = { 'add-event': event }
    panel()
    fireEvent.click(screen.getAllByRole('button', { name: '展开' })[1])
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /饮食偏好/ }))
    expect(await within(dialog).findByText('memory-actual')).toBeTruthy()
    expect(api.memory.detail).not.toHaveBeenCalled()
  })
  it('更新事件可切换修改前后真实版本，均不读取当前值', async () => {
    const previous = { ...memory, memoryContent: '用户喜欢湘菜', version: 1 }
    const event = {
      ...linked,
      eventId: 'update-event',
      sequence: '10',
      kind: 'update' as const,
      sessionId: 'chat-a',
      before: [previous],
      memoryIds: [memory.memoryId],
    }
    activity.records = { 'update-event': event }
    panel([])
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(await screen.findByText('用户喜欢川菜')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: '修改前' }))
    expect(screen.getByText('用户喜欢湘菜')).toBeTruthy()
    expect(screen.getByText(/V1/)).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: '修改后' }))
    expect(screen.getByText('用户喜欢川菜')).toBeTruthy()
    expect(api.memory.detail).not.toHaveBeenCalled()
  })

  it('删除事件展开删除前快照，刷新详情不会读取已删除的当前实体', async () => {
    const event = {
      ...linked,
      eventId: 'delete-event',
      sequence: '8',
      kind: 'delete' as const,
      sessionId: 'chat-a',
      before: [memory],
      memories: [],
      memoryIds: [memory.memoryId],
    }
    activity.records = { 'delete-call': { ...event, callId: 'delete-call' } }
    panel([])
    expect(screen.getByText('删除了 1 条记忆')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(await screen.findByText('memory-actual')).toBeTruthy()
    expect(api.memory.detail).not.toHaveBeenCalled()
    for (const label of ['ID', 'Key', 'value', 'memory_type', 'status', 'tags', 'updated_at']) {
      expect(screen.getByRole('rowheader', { name: label })).toBeTruthy()
    }
  })

  it('提交的对话显示写入详情，不冒充两字段记忆', () => {
    panel()
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: '对话写入详情' })).toBeTruthy()
    expect(within(dialog).getByText('用户', { exact: true })).toBeTruthy()
    expect(within(dialog).getByText('请记住我喜欢川菜', { exact: true })).toBeTruthy()
    expect(within(dialog).queryByRole('heading', { name: '记忆详情' })).toBeNull()
    expect(api.memory.detail).not.toHaveBeenCalled()
  })
  it('从写入详情进入已保存七字段和版本，不回填当前实体', async () => {
    activity.records = { 'extract-1': linked }
    panel()
    fireEvent.click(screen.getAllByRole('button', { name: '展开' })[1])
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /饮食偏好/ }))
    expect(await within(dialog).findByText('memory-actual')).toBeTruthy()
    expect(api.memory.detail).not.toHaveBeenCalled()
    for (const label of ['ID', 'Key', 'value', 'memory_type', 'status', 'tags', 'updated_at']) {
      expect(within(dialog).getByRole('rowheader', { name: label })).toBeTruthy()
    }
    expect(within(dialog).getByText(/V2/)).toBeTruthy()
  })
  it('没有保存关联的历史写入给出真实状态及查看记忆库入口', () => {
    activity.records = { 'extract-1': { ...linked, status: 'unavailable', memories: [], total: 0 } }
    panel()
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(screen.getByText('此记录暂无关联记忆')).toBeTruthy()
    expect(screen.getByRole('link', { name: '查看所有记忆' }).getAttribute('href')).toBe('/cn/memoryList')
    expect(screen.queryByRole('rowheader', { name: 'ID' })).toBeNull()
  })
  it('等待提取的详情随真实活动结果更新，不把提交回执当完成', () => {
    activity.records = { 'extract-1': { ...linked, status: 'pending', memories: [], total: 0 } }
    const view = panel()
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getAllByText('记忆整理中').length).toBeGreaterThan(0)
    expect(within(dialog).queryByText('已生成 1 条记忆')).toBeNull()
    activity.records = { 'extract-1': linked }
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <LangProvider>
          <TooltipProvider>
            <ToolPanel projectId="test-project" items={[write]} onLocate={vi.fn()} open={false} onClose={vi.fn()} />
          </TooltipProvider>
        </LangProvider>
      </QueryClientProvider>,
    )
    expect(within(dialog).getByText('已生成 1 条记忆')).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: /饮食偏好/ })).toBeTruthy()
  })
  it('失败的删除显示删除详情与失败状态', () => {
    panel([{ ...write, toolName: 'delete_memory', args: { memory_ids: ['missing-id'] }, result: { status: 'failed' }, isError: true }])
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: '记忆删除详情' })).toBeTruthy()
    expect(within(dialog).getByText('操作未完成，请查看对话后重试')).toBeTruthy()
    expect(within(dialog).queryByRole('rowheader', { name: 'memory_type' })).toBeNull()
  })
  it('空检索显示检索详情，不生成记忆对象', () => {
    panel([{ ...write, toolName: 'search_memory', args: { query: '未知偏好' }, result: { memories: [] } }])
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(screen.getByRole('heading', { name: '记忆检索详情' })).toBeTruthy()
    expect(screen.queryByRole('rowheader', { name: 'memory_type' })).toBeNull()
  })
  it('当前记忆库详情读取失败时允许重试，不把摘要当完整实体', async () => {
    vi.mocked(api.memory.detail).mockRejectedValue(new Error('detail unavailable'))
    render(
      <QueryClientProvider client={new QueryClient()}>
        <LangProvider>
          <MemoryDetailFields projectId="test-project" entry={entryFromMemory(memory)} />
        </LangProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('alert', {}, { timeout: 3500 })).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
    vi.mocked(api.memory.detail).mockResolvedValue(memory)
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('memory-actual')).toBeTruthy()
  })
  it('被删除或不可访问的关联记忆不显示成零条提取', () => {
    activity.records = { 'extract-1': { ...linked, memories: [] } }
    panel()
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(screen.getByText('已生成 1 条记忆')).toBeTruthy()
    expect(screen.getByText('部分记忆已删除或不可访问')).toBeTruthy()
  })
})

it('只更新共享activities也必须刷新异步提取日志', () => {
  const props = {
    projectId: 'test-project',
    items: [write],
    libraryOpen: false,
    onLibraryOpen: vi.fn(),
    onLibraryClose: vi.fn(),
    onLocate: vi.fn(),
    open: false,
    onClose: vi.fn(),
  }
  const wrap = (records: Record<string, MemoryLogActivity>) => (
    <QueryClientProvider client={new QueryClient()}>
      <LangProvider>
        <TooltipProvider>
          <ToolPanel {...props} activities={records} />
        </TooltipProvider>
      </LangProvider>
    </QueryClientProvider>
  )
  const view = render(wrap({}))
  expect(screen.queryByText('用户MemCube新增了1条记忆')).toBeNull()
  view.rerender(wrap({ 'extract-1': linked }))
  expect(screen.getByText('用户MemCube新增了1条记忆')).toBeTruthy()
})

it('相同内容的更新不把返回的现有实体计成新增版本', () => {
  const item: ChatToolItem = {
    ...write,
    toolName: 'update_memory',
    result: { changed: false, memory: { memory_id: 'memory-actual', memory_value: '用户喜欢川菜' } },
  }
  expect(memoryLogPresentation(item).count).toBe(0)
  panel([item])
  expect(screen.getByText('更新了 0 条记忆')).toBeTruthy()
  expect(screen.queryByText('更新了 1 条记忆')).toBeNull()
})

it('失败的工具关联实际失败事件，只显示一条记录', () => {
  const failed: ChatToolItem = {
    ...write,
    toolName: 'search_memory',
    isError: true,
    result: {
      content: [
        { type: 'text', text: '操作未完成' },
        { type: 'text', text: JSON.stringify({ status: 'failed', call_id: 'failed-search' }) },
      ],
    },
  }
  expect(memoryCallId(failed)).toBe('failed-search')
  activity.records = {
    failure: {
      ...linked,
      callId: 'failed-search',
      eventId: 'failure',
      sequence: '12',
      kind: 'recall',
      sessionId: 'chat-a',
      before: [],
      memoryIds: [],
      status: 'failed',
      memories: [],
      total: 0,
    },
  }
  panel([failed])
  expect(document.querySelectorAll('.memos-log-event')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '展开' }))
  expect(screen.getByRole('heading', { name: '记忆检索详情' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '定位对话' })).toBeTruthy()
})

it('日志记忆库入口跳转列表，不打开弹窗', () => {
  panel([])
  expect(screen.getByRole('link', { name: '所有记忆' }).getAttribute('href')).toBe('/cn/memoryList')
  expect(screen.queryByRole('dialog')).toBeNull()
})
