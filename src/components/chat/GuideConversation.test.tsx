// @vitest-environment jsdom
import { useEffect, useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { LangProvider } from '../../lib/store'
import { api } from '../../lib/api'
import { ChatConversations } from './ChatConversations'
import type { ChatControls } from './ChatSession'

const calls = vi.hoisted(() => ({ send: vi.fn<() => Promise<boolean>>(), done: vi.fn(), failed: vi.fn() }))
vi.mock('./ChatSession', () => ({
  ChatSession: ({
    conversationId,
    onControls,
  }: {
    conversationId: string
    onControls: (id: string, controls: ChatControls | null) => void
  }) => {
    const [ready, setReady] = useState(false)
    useEffect(() => {
      onControls(conversationId, { ready, send: calls.send, retry: async () => {}, focus: () => {}, showLogs: () => {} })
      return () => onControls(conversationId, null)
    }, [conversationId, onControls, ready])
    return (
      <div data-testid="room">
        {conversationId}
        <button onClick={() => setReady(true)}>connect</button>
      </div>
    )
  },
}))
vi.mock('./ChatOnboarding', () => ({
  ChatOnboarding: ({
    onBegin,
    onActive,
  }: {
    onBegin: (text: string, title: string, purpose: 'intro') => Promise<string>
    onActive: (active: boolean) => void
  }) => (
    <>
      <button
        onClick={() => {
          onActive(true)
          void onBegin('explicit introduction', 'title', 'intro').then(calls.done).catch(calls.failed)
        }}
      >
        begin guide
      </button>
      <button onClick={() => onActive(false)}>leave guide</button>
    </>
  ),
}))
function page() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <LangProvider>
          <ChatConversations projectId="p1" guideRequest={0} />
        </LangProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
beforeEach(() => {
  localStorage.clear()
  calls.send.mockReset().mockResolvedValue(true)
  calls.done.mockReset()
  calls.failed.mockReset()
  vi.spyOn(api.chat, 'newConversation').mockResolvedValue({ id: 'server-room', title: 'title', createdAt: '2026-10-31T12:00:00Z' })
  vi.spyOn(api.chat, 'conversations').mockResolvedValue({ records: [], total: 0 })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('新会话历史和流就绪后才发送，返回上一步不重复提交已受理介绍', async () => {
  page()
  fireEvent.click(screen.getByRole('button', { name: 'begin guide' }))
  await waitFor(() => expect(screen.getByTestId('room').textContent).toContain('server-room'))
  expect(calls.send).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'connect' }))
  await waitFor(() => expect(calls.done).toHaveBeenCalledWith('server-room'))
  expect(calls.send).toHaveBeenCalledTimes(1)
  expect(calls.send).toHaveBeenCalledWith('explicit introduction')
  fireEvent.click(screen.getByRole('button', { name: 'begin guide' }))
  await waitFor(() => expect(calls.done).toHaveBeenCalledTimes(2))
  expect(calls.send).toHaveBeenCalledTimes(1)
  expect(api.chat.newConversation).toHaveBeenCalledTimes(1)
})

it('等待中退出不会在旧准备回调到达时偷偷发送介绍', async () => {
  page()
  fireEvent.click(screen.getByRole('button', { name: 'begin guide' }))
  await waitFor(() => expect(screen.getByTestId('room').textContent).toContain('server-room'))
  fireEvent.click(screen.getByRole('button', { name: 'leave guide' }))
  await waitFor(() => expect(calls.failed).toHaveBeenCalledTimes(1))
  fireEvent.click(screen.getByRole('button', { name: 'connect' }))
  expect(calls.send).not.toHaveBeenCalled()
  expect(calls.done).not.toHaveBeenCalled()
})
