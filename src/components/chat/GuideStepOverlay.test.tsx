// @vitest-environment jsdom
import { act } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import { LangProvider } from '../../lib/store'
import { GuideStepOverlay } from './GuideStepOverlay'

const library = vi.hoisted(() => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  return { gate, release, highlight: vi.fn(), destroy: vi.fn(), created: vi.fn(), setConfig: vi.fn() }
})
vi.mock('driver.js', async () => {
  await library.gate
  return {
    driver: () => {
      library.created()
      return {
        highlight: library.highlight,
        destroy: library.destroy,
        getState: () => ({}),
        getConfig: () => ({ overlayOpacity: 0.5 }),
        setConfig: library.setConfig,
      }
    },
  }
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})
it('driver加载期间详情已打开，不叠加遮罩或抢Escape；关闭详情后才高亮', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  const skip = vi.fn()
  const view = render(
    <LangProvider>
      <div id="chat-thread" />
      <GuideStepOverlay stage="first" title="guide" onSkip={skip}>
        <button>next</button>
      </GuideStepOverlay>
    </LangProvider>,
  )
  const detail = document.createElement('div')
  detail.className = 'memos-detail-dialog'
  detail.dataset.state = 'open'
  await act(async () => {
    document.body.append(detail)
  })
  library.release()
  await waitFor(() => expect(library.created).toHaveBeenCalledTimes(1))
  expect(library.highlight).not.toHaveBeenCalled()
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  expect(skip).not.toHaveBeenCalled()
  await act(async () => {
    detail.remove()
  })
  await waitFor(() => expect(library.highlight).toHaveBeenCalledTimes(1))
  view.unmount()
  expect(library.destroy).toHaveBeenCalled()
})

it('高亮框贴着面板且圆角取页面上真实的圆角，不盖住侧栏和相邻面板', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  library.release()
  library.highlight.mockClear()
  library.setConfig.mockClear()
  render(
    <LangProvider>
      <div style={{ borderTopLeftRadius: 22 }}>
        <section>
          <div id="chat-thread" />
        </section>
      </div>
      <GuideStepOverlay stage="first" title="guide" onSkip={vi.fn()}>
        <button>next</button>
      </GuideStepOverlay>
    </LangProvider>,
  )
  await waitFor(() => expect(library.highlight).toHaveBeenCalledTimes(1))
  expect(library.setConfig).toHaveBeenCalledWith({ overlayOpacity: 0.5, stagePadding: 0, stageRadius: 22 })
})
