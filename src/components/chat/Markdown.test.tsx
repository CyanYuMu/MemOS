// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { LangProvider, ToastProvider } from '../../lib/store'
import { Markdown } from './Markdown'
afterEach(cleanup)
it('JSON、py、bash与无语言代码均产生安全的高亮token', () => {
  const code =
    '```json\n{"enabled":true,"name":"sample"}\n```\n```py\ndef greet(name):\n    return "hello"\n```\n```bash\necho "$HOME"\n```\n```\n{"value":true}\n```'
  const { container } = render(
    <LangProvider>
      <ToastProvider>
        <Markdown text={code} />
      </ToastProvider>
    </LangProvider>,
  )
  const blocks = container.querySelectorAll('pre code')
  expect(blocks.length).toBe(4)
  for (const block of blocks) expect(block.querySelectorAll('[class^=hljs-]').length).toBeGreaterThan(0)
  expect(container.querySelector('script')).toBeNull()
})
