// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { LangProvider } from '../../lib/store'
import { ChatOnboarding } from './ChatOnboarding'
import type { GuideProgress } from './guideProgress'

vi.mock('../../lib/store', async (original) => ({
  ...(await original<typeof import('../../lib/store')>()),
  useMeta: () => ({ account: { accountId: 'guide-account' } }),
}))
vi.mock('./GuideStepOverlay', () => ({
  GuideStepOverlay: ({ title, children }: { title: string; children: React.ReactNode }) => (
    <section>
      <h2>{title}</h2>
      {children}
    </section>
  ),
}))
const progress: GuideProgress = {
  conversationId: 'intro-1',
  ready: true,
  busy: false,
  needsRetry: false,
  hasAnswer: true,
  error: null,
  write: 'pending',
  written: 0,
  recall: 'succeeded',
  recalled: 0,
}
function setup(initial: GuideProgress | null = null, replay = true) {
  const callbacks = {
    onBegin: vi
      .fn<(text: string, title: string, purpose: 'intro' | 'recall') => Promise<string>>()
      .mockResolvedValueOnce('intro-1')
      .mockResolvedValue('recall-2'),
    onSelect: vi.fn(),
    onActive: vi.fn(),
    onLogs: vi.fn(),
    onReturn: vi.fn(),
    onRetry: vi.fn(),
    onRetryWrite: vi.fn().mockResolvedValue(undefined),
  }
  const draw = (p: GuideProgress | null) => (
    <LangProvider>
      <ChatOnboarding {...callbacks} projectId="p1" replay={replay} conversationId={p?.conversationId || ''} progress={p} />
    </LangProvider>
  )
  const result = render(draw(initial))
  return { ...callbacks, rerender: (p: GuideProgress | null) => result.rerender(draw(p)) }
}
beforeEach(() => localStorage.clear())
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('平台检查点可在没有浏览器缓存时恢复原对话，不重复发送', async () => {
  const begin = vi.fn()
  const select = vi.fn()
  render(
    <LangProvider>
      <ChatOnboarding
        projectId="p1"
        replay
        embedded
        conversationId=""
        progress={null}
        savedCheckpoint={{ stage: 'logs', first: 'server-owned-room' }}
        onBegin={begin}
        onSelect={select}
        onActive={vi.fn()}
        onLogs={vi.fn()}
        onReturn={vi.fn()}
        onRetry={vi.fn()}
      />
    </LangProvider>,
  )
  await screen.findByRole('heading', { name: '看见记忆如何参与回答' })
  expect(select).toHaveBeenCalledWith('server-owned-room')
  expect(begin).not.toHaveBeenCalled()
})

it('平台指引召回为空时不能完成，也不会回报任务成功', async () => {
  const complete = vi.fn()
  render(
    <LangProvider>
      <ChatOnboarding
        projectId="p1"
        replay
        embedded
        conversationId="recall-2"
        progress={{ ...progress, conversationId: 'recall-2', recall: 'succeeded', recalled: 0 }}
        savedCheckpoint={{ stage: 'finish', first: 'intro-1', second: 'recall-2' }}
        onComplete={complete}
        onBegin={vi.fn()}
        onSelect={vi.fn()}
        onActive={vi.fn()}
        onLogs={vi.fn()}
        onReturn={vi.fn()}
        onRetry={vi.fn()}
      />
    </LangProvider>,
  )
  const button = await screen.findByRole('button', { name: '完成指引' })
  expect((button as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(button)
  expect(complete).not.toHaveBeenCalled()
})

it('六步使用真实新对话，回答和提取未完成前不能越过等待', async () => {
  const view = setup()
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  fireEvent.change(screen.getByLabelText('希望记住的偏好'), { target: { value: '喜欢茶，回答先说结论' } })
  fireEvent.click(screen.getByRole('checkbox', { name: '川菜' }))
  fireEvent.click(screen.getByRole('button', { name: '发送介绍，开始体验' }))
  await screen.findByRole('heading', { name: '第一段对话：让助手认识你' })
  expect(view.onBegin).toHaveBeenCalledTimes(1)
  expect(view.onBegin.mock.calls[0][0]).toContain('川菜')
  expect(view.onBegin.mock.calls[0][0]).toContain('喜欢茶，回答先说结论')
  expect((screen.getByRole('button', { name: '请等待完成' }) as HTMLButtonElement).disabled).toBe(true)
  view.rerender(progress)
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  await screen.findByRole('heading', { name: '看见记忆如何参与回答' })
  expect((screen.getByRole('button', { name: '请等待完成' }) as HTMLButtonElement).disabled).toBe(true)
  expect(view.onBegin).toHaveBeenCalledTimes(1)
  view.rerender({ ...progress, write: 'succeeded', written: 2 })
  expect(screen.getByRole('status').textContent).toContain('2')
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  await screen.findByRole('heading', { name: '换一段对话，看看记忆的作用' })
  expect(view.onBegin.mock.calls[1][2]).toBe('recall')
  expect(view.onBegin.mock.calls[1][0]).not.toContain('喜欢茶')
  expect((screen.getByRole('button', { name: '下一步（5秒）' }) as HTMLButtonElement).disabled).toBe(true)
})

it('暂停后保留项目步骤，重新打开只恢复对话，不自动再发送', async () => {
  localStorage.setItem('console.chat.guide.v2:guide-account:p1:progress', JSON.stringify({ stage: 'logs', first: 'intro-1' }))
  const view = setup(progress)
  await screen.findByRole('heading', { name: '看见记忆如何参与回答' })
  fireEvent.click(screen.getByRole('button', { name: '回到对话，稍后继续' }))
  expect(view.onBegin).not.toHaveBeenCalled()
  expect(JSON.parse(localStorage.getItem('console.chat.guide.v2:guide-account:p1:progress')!).stage).toBe('logs')
  cleanup()
  const resumed = setup(progress)
  await screen.findByRole('heading', { name: '看见记忆如何参与回答' })
  expect(resumed.onBegin).not.toHaveBeenCalled()
})

it('失败保留表单可重试，未点提交不创建对话；完成过的账号不自动弹出', async () => {
  const view = setup()
  view.onBegin.mockReset().mockRejectedValue(new Error('暂时不可用'))
  expect(view.onBegin).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  fireEvent.change(screen.getByLabelText('希望记住的偏好'), { target: { value: '喜欢清淡' } })
  fireEvent.click(screen.getByRole('button', { name: '发送介绍，开始体验' }))
  await screen.findByRole('alert')
  expect((screen.getByLabelText('希望记住的偏好') as HTMLTextAreaElement).value).toBe('喜欢清淡')
  expect((screen.getByRole('button', { name: '发送介绍，开始体验' }) as HTMLButtonElement).disabled).toBe(false)
  cleanup()
  localStorage.setItem('console.chat.guide.v2:guide-account', '1')
  setup(null, false)
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
})

it('恢复的半截回答有needsRetry时不解锁，并提供实际重试入口', async () => {
  localStorage.setItem('console.chat.guide.v2:guide-account:p1:progress', JSON.stringify({ stage: 'first', first: 'intro-1' }))
  const view = setup({ ...progress, needsRetry: true, busy: false })
  await screen.findByRole('heading', { name: '第一段对话：让助手认识你' })
  expect((screen.getByRole('button', { name: '请等待完成' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(view.onRetry).toHaveBeenCalledTimes(1)
  expect(view.onBegin).not.toHaveBeenCalled()
})

it('创建召回会话等待就绪时，不把父层新选择拉回第一会话', async () => {
  localStorage.setItem('console.chat.guide.v2:guide-account:p1:progress', JSON.stringify({ stage: 'logs', first: 'intro-1' }))
  const view = setup({ ...progress, write: 'succeeded', written: 7 })
  let resolve!: (id: string) => void
  view.onBegin.mockReset().mockImplementation(
    () =>
      new Promise<string>((done) => {
        resolve = done
      }),
  )
  fireEvent.click(screen.getByRole('button', { name: '下一步' }))
  // 父层先切新会话，历史和SSE准备好后onBegin才返回ID。
  view.rerender({ ...progress, conversationId: 'recall-2', ready: false, hasAnswer: false })
  expect(view.onSelect).not.toHaveBeenCalledWith('intro-1')
  await act(async () => {
    resolve('recall-2')
  })
  expect(screen.getByRole('heading', { name: '换一段对话，看看记忆的作用' })).toBeTruthy()
  expect(view.onSelect).not.toHaveBeenCalledWith('intro-1')
})

it('回答结束但没有写入调用时可以显式重新保存，不会假装已完成', async () => {
  localStorage.setItem('console.chat.guide.v2:guide-account:p1:progress', JSON.stringify({ stage: 'logs', first: 'intro-1' }))
  const view = setup({ ...progress, write: 'none' })
  const retry = await screen.findByRole('button', { name: '重新尝试保存' })
  expect(view.onRetryWrite).not.toHaveBeenCalled()
  expect((screen.getByRole('button', { name: '请等待完成' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(retry)
  await waitFor(() => expect(view.onRetryWrite).toHaveBeenCalledTimes(1))
  expect(view.onBegin).not.toHaveBeenCalled()
})
