// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../../lib/api'
import { LangProvider } from '../../lib/store'
import { ChatConversations } from './ChatConversations'

vi.mock('./ChatSession', () => ({
  ChatSession: ({ projectId, conversationId }: { projectId: string; conversationId: string }) => (
    <div data-testid="room">
      {projectId}:{conversationId || 'legacy'}
    </div>
  ),
}))
vi.mock('./ChatOnboarding', () => ({ ChatOnboarding: () => null }))
function page(url = '/cn/chat') {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[url]}>
        <LangProvider>
          <ChatConversations projectId="p1" guideRequest={0} />
        </LangProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
beforeEach(() => {
  localStorage.clear()
  vi.spyOn(api.chat, 'conversations').mockResolvedValue({ records: [], total: 0 })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('新建请求在途不重复提交，成功切到真实ID且保留旧会话入口', async () => {
  let resolve!: (room: { id: string; title: string; createdAt: string }) => void
  const create = vi.spyOn(api.chat, 'newConversation').mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done
      }),
  )
  page()
  const button = screen.getByRole('button', { name: '新建对话' })
  fireEvent.click(button)
  fireEvent.click(button)
  expect(create).toHaveBeenCalledTimes(1)
  resolve({ id: 'created-by-server', title: '', createdAt: '2026-10-31T12:00:00Z' })
  await waitFor(() => expect(screen.getByTestId('room').textContent).toBe('p1:created-by-server'))
  expect(api.chat.conversations).toHaveBeenCalledWith('p1', 0)
})

it('新建失败保留当前对话并显示错误，可重试', async () => {
  vi.spyOn(api.chat, 'newConversation').mockRejectedValue(new Error('offline'))
  page('/cn/chat?chatProject=p1&conversationId=kept')
  fireEvent.click(screen.getByRole('button', { name: '新建对话' }))
  await screen.findByRole('alert')
  expect(screen.getByTestId('room').textContent).toBe('p1:kept')
  expect((screen.getByRole('button', { name: '新建对话' }) as HTMLButtonElement).disabled).toBe(false)
})

it('URL中其他项目的会话不会被发送给当前项目', () => {
  page('/cn/chat?chatProject=p2&conversationId=foreign')
  expect(screen.getByTestId('room').textContent).toBe('p1:legacy')
})

it('失败的下一页重试会再次读取缺失页，不用首页刷新冒充重试', async () => {
  const first = Array.from({ length: 20 }, (_, i) => ({ id: 'id-' + i, title: '历史' + i, createdAt: '2026-10-31T12:00:00Z' }))
  vi.mocked(api.chat.conversations)
    .mockResolvedValueOnce({ records: first, total: 21 })
    .mockRejectedValueOnce(new Error('page offline'))
    .mockResolvedValueOnce({ records: [{ id: 'last', title: '最后一条', createdAt: '2026-10-31T12:00:00Z' }], total: 21 })
  page()
  fireEvent.click(screen.getByRole('button', { name: '历史对话' }))
  fireEvent.click(await screen.findByRole('button', { name: '加载更多' }))
  fireEvent.click(await screen.findByRole('button', { name: '重试' }))
  await screen.findByRole('button', { name: /最后一条/ })
  expect(vi.mocked(api.chat.conversations).mock.calls.map((call) => call[1])).toEqual([0, 20, 20])
})
