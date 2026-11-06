// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
vi.mock('../lib/config', () => ({ SERVICE_BASE: '', LINKS: { mcp: '/mcp', docs: '/cn/docs' } }))
vi.mock('../lib/store', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/store')>()),
  useMeta: () => ({ tools: [] }),
}))
import { LangProvider, ToastProvider } from '../lib/store'
import { QuickStart } from './QuickStart'
afterEach(cleanup)
it('地址未配置时不给出会误连其他环境的安装命令，保留聊天入口', () => {
  const { container } = render(
    <MemoryRouter>
      <LangProvider>
        <ToastProvider>
          <QuickStart />
        </ToastProvider>
      </LangProvider>
    </MemoryRouter>,
  )
  expect(screen.getByRole('status').textContent).toContain('接入地址尚未配置')
  expect(screen.getByRole('link', { name: '体验对话' }).getAttribute('href')).toBe('/cn/chat')
  expect(container.textContent).not.toContain('curl ')
})
