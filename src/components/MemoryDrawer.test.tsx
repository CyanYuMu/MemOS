// @vitest-environment jsdom
import { useState } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, type Memory } from '../lib/api'
import { makeT } from '../lib/i18n'
import { MemoryDrawer } from './MemoryDrawer'

const guide = vi.hoisted(() => ({ value: null as null | Record<string, unknown> }))
vi.mock('../lib/onboarding', () => ({ usePlatformGuide: () => guide.value }))
vi.mock('../lib/store', () => ({ useT: () => makeT('zh'), useLang: () => ({ lang: 'zh' }), useToast: () => vi.fn() }))
const memory: Memory = {
  memoryId: 'actual-id',
  memoryKey: '饮食偏好',
  memoryContent: '我喜欢川菜。',
  subjectType: 'user',
  externalUserId: 'test-user',
  createTime: '2026-10-31 12:00:00',
  updateTime: '2026-10-31 13:00:00',
  memoryType: 'UserMemory',
  status: 'activated',
  tags: ['川菜'],
  version: 2,
  sources: [{ role: 'user', type: 'chat', content: '请改为川菜。' }],
  history: [{ version: 1, content: '我喜欢湘菜。', createdAt: '2026-10-31 12:00:00' }],
}
function TestPage() {
  const [open, setOpen] = useState(false)
  const [opener, setOpener] = useState<HTMLElement | null>(null)
  return (
    <>
      <button
        onClick={(event) => {
          setOpener(event.currentTarget)
          setOpen(true)
        }}
      >
        查看详情
      </button>
      {open && (
        <MemoryDrawer
          projectId="test-project"
          memoryId="actual-id"
          memType="text_mem"
          returnFocus={opener}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  )
}
beforeEach(() => {
  guide.value = null
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
it('复制实际正文、读取真实版本，Escape关闭后返回原列表按钮', async () => {
  const copy = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: copy } })
  render(
    <QueryClientProvider client={new QueryClient()}>
      <TestPage />
    </QueryClientProvider>,
  )
  const opener = screen.getByRole('button', { name: '查看详情' })
  opener.focus()
  fireEvent.click(opener)
  const dialog = screen.getByRole('dialog', { name: '记忆详情' })
  expect(await within(dialog).findByText('我喜欢川菜。')).toBeTruthy()
  fireEvent.click(within(dialog.querySelector('.memory-drawer-content') as HTMLElement).getByRole('button', { name: '复制' }))
  await waitFor(() => expect(copy).toHaveBeenCalledWith('我喜欢川菜。'))
  fireEvent.click(within(dialog).getByRole('tab', { name: '记忆版本' }))
  expect(within(dialog).getByText('V2')).toBeTruthy()
  expect(within(dialog).getByText('V1')).toBeTruthy()
  expect(within(dialog).getByText('我喜欢湘菜。')).toBeTruthy()
  expect(api.memory.detail).toHaveBeenCalledTimes(1)
  fireEvent.keyDown(within(dialog).getByRole('button', { name: '关闭' }), { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await waitFor(() => expect(document.activeElement).toBe(opener))
})

it('平台指引第 5 步：详情里提示去点“记忆版本”，点开后记为已查看', async () => {
  const markDetailsSeen = vi.fn()
  guide.value = { available: true, state: { status: 'active', step: 4 }, detailsSeen: false, markDetailsSeen }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TestPage />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole('button', { name: '查看详情' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByRole('status').textContent).toContain('点击上方的“记忆版本”页签')
  expect(dialog.querySelector('.memory-drawer-tabs')?.getAttribute('data-guide')).toBe('versions')
  expect(markDetailsSeen).not.toHaveBeenCalled()
  fireEvent.click(within(dialog).getByRole('tab', { name: '记忆版本' }))
  await waitFor(() => expect(markDetailsSeen).toHaveBeenCalled())
})

it('没有进行中的指引时，详情里不出现任何提示', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TestPage />
    </QueryClientProvider>,
  )
  fireEvent.click(screen.getByRole('button', { name: '查看详情' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).queryByRole('status')).toBeNull()
  expect(dialog.querySelector('.memory-drawer-tabs')?.getAttribute('data-guide')).toBeNull()
})
