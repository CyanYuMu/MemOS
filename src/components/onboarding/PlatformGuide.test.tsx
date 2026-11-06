// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { api, type OnboardingProgress } from '../../lib/api'
import { makeT } from '../../lib/i18n'
import { PlatformGuideProvider } from '../../lib/onboarding'
import { PlatformGuide, PlatformGuideChecklist, PlatformGuideEntry } from './PlatformGuide'

const identity = vi.hoisted(() => ({ accountId: 'new-user', projectId: 'personal', preview: false }))
vi.mock('../../lib/store', () => ({
  useToast: () => vi.fn(),
  useT: () => makeT('zh'),
  useLang: () => ({ lang: 'zh' }),
  useMeta: () => ({
    account: { accountId: identity.accountId, projects: [{ id: identity.projectId, kind: 'personal', role: 'owner', member: true }] },
    previewUser: identity.preview,
  }),
  useProject: () => ({ currentProjectId: identity.projectId, currentProjectName: '我的空间', ready: true }),
}))
vi.mock('driver.js', () => ({ driver: () => ({ highlight: vi.fn(), destroy: vi.fn() }) }))
function Location() {
  return <output data-testid="route">{useLocation().pathname}</output>
}
function mount(route = '/cn/quickstart', { checklist = false } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const tree = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <PlatformGuideProvider>
          <PlatformGuideEntry />
          <PlatformGuide />
          {checklist && <PlatformGuideChecklist />}
          <Location />
        </PlatformGuideProvider>
      </MemoryRouter>
    </QueryClientProvider>
  )
  const result = render(tree())
  return { ...result, redraw: () => result.rerender(tree()) }
}
let state: OnboardingProgress
beforeEach(() => {
  identity.accountId = 'new-user'
  identity.projectId = 'personal'
  identity.preview = false
  state = { status: 'pending', step: 0, revision: 0, version: 1 }
  vi.spyOn(api.onboarding, 'get').mockImplementation(async () => ({ ...state }))
  vi.spyOn(api.onboarding, 'update').mockImplementation(async (_project, action) => {
    state = {
      ...state,
      status: action === 'dismiss' ? 'dismissed' : 'active',
      step: action === 'advance' ? state.step + 1 : action === 'back' ? state.step - 1 : action === 'replay' ? 0 : state.step,
      revision: state.revision + 1,
    }
    return { ...state }
  })
  vi.spyOn(api.chat, 'state').mockResolvedValue({ projectId: 'personal', hasApiKey: false, canChat: false } as Awaited<
    ReturnType<typeof api.chat.state>
  >)
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  )
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('欢迎只介绍Web主线，开始后依次去密钥页，不创建凭据或发送消息', async () => {
  const create = vi.spyOn(api.keys, 'create')
  const view = mount()
  await screen.findByRole('dialog')
  expect(api.onboarding.update).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '开始了解平台' }))
  await screen.findByRole('heading', { name: '认识个人空间' })
  fireEvent.click(screen.getByRole('button', { name: /已了解，下一步/ }))
  await screen.findByRole('heading', { name: '认识快速开始' })
  fireEvent.click(screen.getByRole('button', { name: /前往接口密钥/ }))
  await waitFor(() => expect(view.getByTestId('route').textContent).toBe('/cn/apikeys'))
  await screen.findByRole('heading', { name: '创建 API Key' })
  expect((screen.getByRole('button', { name: /开始对话/ }) as HTMLButtonElement).disabled).toBe(true)
  expect(create).not.toHaveBeenCalled()
})

it('旧账号不自动弹出；可以主动恢复持久化检查点', async () => {
  state = { ...state, status: 'dismissed', step: 2, revision: 4 }
  mount()
  await waitFor(() => expect(api.onboarding.get).toHaveBeenCalled())
  expect(screen.queryByRole('dialog')).toBeNull()
  await waitFor(() => expect((screen.getByRole('button', { name: '平台指引' }) as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: '平台指引' }))
  await screen.findByRole('heading', { name: '创建 API Key' })
  expect(api.onboarding.update).toHaveBeenCalledWith('personal', 'start', 4, undefined)
})

it('保存失败保留欢迎与重试动作，不提前跳转', async () => {
  vi.mocked(api.onboarding.update).mockRejectedValue(new Error('save failed'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: '开始了解平台' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('dialog')).toBeTruthy()
  expect(screen.getByTestId('route').textContent).toBe('/cn/quickstart')
})

it('只读空间提供切换指引，不把已有密钥当作聊天权限', async () => {
  state = { ...state, status: 'active', step: 2 }
  vi.mocked(api.chat.state).mockResolvedValue({ projectId: 'personal', hasApiKey: true, canChat: false, readOnly: true } as Awaited<
    ReturnType<typeof api.chat.state>
  >)
  mount('/cn/apikeys')
  await screen.findByText('当前空间暂不能开始对话，请切换到个人空间继续。')
  expect((screen.getByRole('button', { name: /开始对话/ }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.onboarding.update).not.toHaveBeenCalled()
})

it('切换账号空间后不采用迟到的旧请求跳转', async () => {
  let resolve: (p: OnboardingProgress) => void = () => {}
  vi.mocked(api.onboarding.update).mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r
      }),
  )
  const view = mount()
  fireEvent.click(await screen.findByRole('button', { name: '开始了解平台' }))
  await waitFor(() => expect(api.onboarding.update).toHaveBeenCalledTimes(1))
  identity.accountId = 'other'
  identity.projectId = 'other-space'
  state = { ...state, status: 'dismissed' }
  view.redraw()
  await act(async () => {
    resolve({ status: 'active', step: 2, revision: 1, version: 1 })
  })
  expect(screen.getByTestId('route').textContent).toBe('/cn/quickstart')
  expect(screen.queryByRole('heading', { name: '创建 API Key' })).toBeNull()
})

it('切换普通用户预览时保留指引入口和原有进度', async () => {
  state = { ...state, status: 'dismissed', step: 2, revision: 4 }
  const view = mount()
  await waitFor(() => expect((screen.getByRole('button', { name: '平台指引' }) as HTMLButtonElement).disabled).toBe(false))
  identity.preview = true
  view.redraw()
  expect(screen.queryByRole('button', { name: '平台指引' })).not.toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '平台指引' }))
  await screen.findByRole('heading', { name: '创建 API Key' })
  expect(api.onboarding.update).toHaveBeenCalledWith('personal', 'start', 4, undefined)
  identity.preview = false
  view.redraw()
  expect(screen.getByRole('heading', { name: '创建 API Key' })).toBeTruthy()
  expect(api.onboarding.update).toHaveBeenCalledTimes(1)
})

it('平台指引可退回上一页并保存检查点，不创建密钥', async () => {
  state = { ...state, status: 'active', step: 2, revision: 4 }
  const create = vi.spyOn(api.keys, 'create')
  const view = mount('/cn/apikeys')
  fireEvent.click(await screen.findByRole('button', { name: '上一步' }))
  await screen.findByRole('heading', { name: '认识快速开始' })
  await waitFor(() => expect(view.getByTestId('route').textContent).toBe('/cn/quickstart'))
  expect(api.onboarding.update).toHaveBeenCalledWith('personal', 'back', 4, undefined)
  expect(create).not.toHaveBeenCalled()
})

it('快速开始页的清单与引导卡片使用同一个步数', async () => {
  state = { ...state, status: 'active', step: 1, revision: 2 }
  mount('/cn/quickstart', { checklist: true })
  const card = (await screen.findByRole('heading', { name: '认识快速开始' })).closest('section') as HTMLElement
  const list = document.querySelector('.platform-guide-checklist') as HTMLElement
  expect(card.querySelector('.platform-guide-count')?.textContent).toBe('2 / 6')
  expect(list.querySelector('header span')?.textContent).toBe('2 / 6')
})

it('清单步数：未开始为 0，已中断按下一步计，完成为 6', async () => {
  state = { ...state, status: 'dismissed', step: 3, revision: 5 }
  const view = mount('/cn/quickstart', { checklist: true })
  await waitFor(() => expect(document.querySelector('.platform-guide-checklist header span')?.textContent).toBe('4 / 6'))
  view.unmount()
  state = { status: 'pending', step: 0, revision: 0, version: 1 }
  mount('/cn/quickstart', { checklist: true })
  await waitFor(() => expect(document.querySelector('.platform-guide-checklist header span')?.textContent).toBe('0 / 6'))
  cleanup()
  state = { status: 'completed', step: 6, revision: 9, version: 1 }
  mount('/cn/quickstart', { checklist: true })
  await waitFor(() => expect(document.querySelector('.platform-guide-checklist header span')?.textContent).toBe('6 / 6'))
})

it('对话步骤的指引收成一行，其他步骤保持原样', async () => {
  state = { ...state, status: 'active', step: 3, revision: 4 }
  mount('/cn/chat')
  await screen.findByRole('heading', { name: '通过对话体验记忆' })
  expect(document.querySelector('.platform-guide--bar')).not.toBeNull()
  cleanup()
  state = { ...state, step: 1 }
  mount('/cn/quickstart')
  await screen.findByRole('heading', { name: '认识快速开始' })
  expect(document.querySelector('.platform-guide--bar')).toBeNull()
})

it('弹窗盖住目标时指引卡片先收起，不掉回页面顶部；弹窗关闭后回来', async () => {
  state = { ...state, status: 'active', step: 4, revision: 4 }
  const host = document.createElement('div')
  host.innerHTML = '<table><tbody><tr data-guide="memory-row"><td><button>显示</button></td></tr></tbody></table>'
  document.body.append(host)
  const button = host.querySelector('button') as HTMLButtonElement
  button.getBoundingClientRect = () => ({ width: 20, height: 20, x: 0, y: 0, top: 0, left: 0, right: 20, bottom: 20, toJSON: () => ({}) })
  mount('/cn/memoryList')
  await screen.findByRole('heading', { name: '查看记忆与版本' })
  const dialog = document.createElement('div')
  dialog.setAttribute('role', 'dialog')
  dialog.dataset.state = 'open'
  await act(async () => {
    document.body.append(dialog)
  })
  await waitFor(() => expect(screen.queryByRole('heading', { name: '查看记忆与版本' })).toBeNull())
  await act(async () => {
    dialog.remove()
  })
  await waitFor(() => expect(screen.queryByRole('heading', { name: '查看记忆与版本' })).not.toBeNull())
  host.remove()
})

it('第 5 步没看过记忆版本时，提示和主按钮都在告诉用户去点哪里', async () => {
  state = { ...state, status: 'active', step: 4, revision: 4 }
  mount('/cn/memoryList')
  await screen.findByRole('heading', { name: '查看记忆与版本' })
  expect(screen.getByText(/点击一条记忆右侧的“眼睛”图标（查看详情）打开详情，再切换到“记忆版本”页签/)).toBeTruthy()
  expect(screen.getByText(/还差一步：在详情里切换到“记忆版本”页签/)).toBeTruthy()
  expect((screen.getByRole('button', { name: /已了解，下一步/ }) as HTMLButtonElement).disabled).toBe(true)
})
