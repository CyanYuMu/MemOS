// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { makeT } from '../lib/i18n'
import { api } from '../lib/api'
import { QuickStart } from './QuickStart'

vi.mock('../lib/store', () => ({
  useT: () => makeT('zh'),
  useLang: () => ({ lang: 'zh' }),
  useToast: () => vi.fn(),
  useMeta: () => ({ tools: [] }),
  useProject: () => ({ currentProjectId: 'test-project', ready: true }),
}))
beforeEach(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
  vi.spyOn(api.keys, 'list').mockResolvedValue({ records: [], total: 0 })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
function page() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <QuickStart />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
it('接入卡片有专用图标，代码语言使用各自标识', () => {
  page()
  const choices = screen.getByRole('group', { name: '选择接入方式' })
  expect(choices.querySelector('img')?.getAttribute('src')).toBe('/logo.svg?v=hugegraph')
  fireEvent.click(within(choices).getByRole('button', { name: /代码集成/ }))
  for (const [name, asset] of [
    ['Python (HTTP)', 'python'],
    ['TypeScript (HTTP)', 'typescript'],
    ['cURL', 'bash'],
  ]) {
    const tab = screen.getByRole('tab', { name })
    expect(tab.querySelector('img')?.getAttribute('src')).toBe(`/images/access/${asset}.svg`)
  }
})
it('切换接入分组后保留分别选中的方式，选中方式与代码同步切换', () => {
  page()
  fireEvent.click(screen.getByRole('tab', { name: 'Hermes（MCP）' }))
  fireEvent.click(screen.getByRole('button', { name: /代码集成/ }))
  fireEvent.click(screen.getByRole('tab', { name: 'cURL' }))
  expect(screen.getByRole('tab', { name: 'cURL' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: /Agent 接入/ }))
  expect(screen.getByRole('tab', { name: 'Hermes（MCP）' }).getAttribute('aria-selected')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: /代码集成/ }))
  expect(screen.getByRole('tab', { name: 'cURL' }).getAttribute('aria-selected')).toBe('true')
  expect(screen.getAllByText(/curl --fail-with-body/).length).toBeGreaterThan(0)
})

it('代码集成提示词默认收起，跟随语言切换并只复制当前可见示例', async () => {
  const copy = vi.fn().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: copy } })
  page()
  fireEvent.click(screen.getByRole('button', { name: /代码集成/ }))
  expect(screen.queryByText(/请在当前项目中使用/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看提示词' }))
  const preview = document.getElementById('code-integration-prompt')!
  expect(preview.textContent).toContain('Python (HTTP)')
  expect(preview.textContent).toContain('/memory/add')
  expect(preview.textContent).toContain('/memory/search')
  expect(preview.textContent).toContain('YOUR_API_KEY')
  fireEvent.click(screen.getByRole('tab', { name: 'cURL' }))
  expect(preview.textContent).toContain('curl --fail-with-body')
  expect(preview.textContent).not.toContain('from urllib.request')
  fireEvent.click(screen.getByRole('button', { name: '复制配置提示词' }))
  expect(copy).toHaveBeenCalledWith(preview.textContent)
  expect(await screen.findByRole('button', { name: '已复制' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '收起提示词' }))
  expect(document.getElementById('code-integration-prompt')).toBeNull()
})
