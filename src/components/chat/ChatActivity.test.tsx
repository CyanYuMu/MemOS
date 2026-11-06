// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { LangProvider, ToastProvider } from '../../lib/store'
import { ChatActivity } from './ChatActivity'
import type { ChatThinkingItem } from './timeline'

const thinking: ChatThinkingItem = { kind: 'thinking', id: 'think-1', text: '这是实际返回的过程正文。', streaming: false }
beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
function activity(item = thinking) {
  render(
    <LangProvider>
      <ToastProvider>
        <ChatActivity items={[item]} highlightedId={null} />
      </ToastProvider>
    </LangProvider>,
  )
}
it('标题为静态文本，只保留右侧展开入口', () => {
  activity()
  expect(screen.getByText('已完成深度思考').closest('button')).toBeNull()
  expect(screen.getAllByRole('button')).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '放大查看过程' }))
  expect(screen.getByText(thinking.text)).toBeTruthy()
})
it('放大按钮在原面板展开正文，再次点击收起，不创建弹窗或副本', () => {
  activity()
  const opener = screen.getByRole('button', { name: '放大查看过程' })
  const panel = opener.closest('section')!
  fireEvent.click(opener)
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(panel.contains(screen.getByText(thinking.text))).toBe(true)
  expect(opener.getAttribute('aria-expanded')).toBe('true')
  const collapse = screen.getByRole('button', { name: '收起过程' })
  fireEvent.click(collapse)
  expect(screen.queryByText(thinking.text)).toBeNull()
  expect(opener.getAttribute('aria-expanded')).toBe('false')
})
it('流式中的过程直接可读，不需要第二次展开', () => {
  activity({ ...thinking, streaming: true })
  expect(screen.getByText(thinking.text)).toBeTruthy()
})
it('原位置展开只注册一份工具定位锚点', () => {
  render(
    <LangProvider>
      <ToastProvider>
        <ChatActivity
          items={[
            thinking,
            {
              kind: 'tool',
              id: 'tool-1',
              toolCallId: 'call-1',
              toolName: 'search_memory',
              args: { query: '川菜' },
              result: { memories: [] },
              running: false,
              isError: false,
            },
          ]}
          highlightedId={null}
        />
      </ToastProvider>
    </LangProvider>,
  )
  fireEvent.click(screen.getByRole('button', { name: '放大查看过程' }))
  expect(document.querySelectorAll('[id="tool-tool-1"]')).toHaveLength(1)
})
