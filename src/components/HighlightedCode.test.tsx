// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { HighlightedCode } from './HighlightedCode'
afterEach(cleanup)
it('Bash、JSON、py、TypeScript保留原文且生成token，HTML样例不执行', () => {
  for (const [language, text] of [
    ['bash', "curl -fsSL 'https://example.com/install.sh' | bash"],
    ['json', '{"name":"<script>alert(1)</script>","enabled":true}'],
    ['py', 'def greet(name):\n    return "hello"'],
    ['typescript', 'const value: number = 1;'],
  ]) {
    const { container, unmount } = render(<HighlightedCode text={text} language={language} />)
    expect(container.textContent).toBe(text)
    expect(container.querySelectorAll('[class^=hljs-]').length).toBeGreaterThan(0)
    expect(container.querySelector('script')).toBeNull()
    unmount()
  }
})
