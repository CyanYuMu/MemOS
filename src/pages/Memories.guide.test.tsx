// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../lib/api'
import { makeT } from '../lib/i18n'
import { TooltipProvider } from '../components/ui/tooltip'
import { Memories } from './Memories'

const guide = vi.hoisted(() => ({ value: null as null | Record<string, unknown> }))
vi.mock('../lib/onboarding', () => ({ usePlatformGuide: () => guide.value }))
vi.mock('../lib/store', () => ({
  useT: () => makeT('zh'),
  useLang: () => ({ lang: 'zh' }),
  useToast: () => vi.fn(),
  useProject: () => ({ currentProjectId: 'p1', currentProjectName: '默认', ready: true }),
}))
const totals: Record<string, number> = { text_mem: 0, pref_mem: 3, event_mem: 0, profile_mem: 4, tool_mem: 0 }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>
          <Memories />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
beforeEach(() => {
  guide.value = null
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
  vi.spyOn(api.memory, 'list').mockImplementation(async (query) => {
    const total = totals[query.memoryDetailType] ?? 0
    return {
      total,
      memListItemResponseList: Array.from({ length: Math.min(total, query.pageSize ?? 10) }, (_, i) => ({
        memoryId: `${query.memoryDetailType}-${i}`,
        createTime: '2026-11-05 10:00:00',
        memoryContent: `内容 ${query.memoryDetailType}`,
        subjectType: 'user',
        externalUserId: 'u1',
      })),
    } as Awaited<ReturnType<typeof api.memory.list>>
  })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('平台指引第 5 步：默认页签没有记忆而别的页签有时，自动切到有数据的页签', async () => {
  guide.value = { available: true, state: { status: 'active', step: 4 }, detailsSeen: false, markDetailsSeen: vi.fn() }
  mount()
  await waitFor(() => expect(document.querySelector('[data-guide="memory-row"]')).not.toBeNull())
  expect(document.body.textContent).toContain('内容 pref_mem')
})

it('用户自己点过页签后不再替他切；没有进行中的指引时也保持默认页签', async () => {
  mount()
  await waitFor(() => expect(screen.getByRole('tab', { name: /偏好记忆/ })).toBeTruthy())
  await waitFor(() => expect(screen.getByRole('tab', { name: /偏好记忆/ }).textContent).toContain('3'))
  expect(document.querySelector('[data-guide="memory-row"]')).toBeNull()
  cleanup()
  guide.value = { available: true, state: { status: 'active', step: 4 }, detailsSeen: false, markDetailsSeen: vi.fn() }
  mount()
  await waitFor(() => expect(document.body.textContent).toContain('内容 pref_mem'))
  fireEvent.click(screen.getByRole('tab', { name: /事实记忆/ }))
  await waitFor(() => expect(document.querySelector('[data-guide="memory-row"]')).toBeNull())
  await new Promise((r) => setTimeout(r, 50))
  expect(document.querySelector('[data-guide="memory-row"]')).toBeNull()
})
