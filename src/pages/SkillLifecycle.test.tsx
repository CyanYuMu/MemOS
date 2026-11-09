// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError } from '../lib/api'
import { skillLifecycle, skillLifecyclePackage } from '../lib/skill-lifecycle.contract'
import { lifecycleFixture, lifecyclePackageFixture } from '../lib/skill-lifecycle.test-fixture'
import { SkillLifecyclePanel } from './SkillLifecycle'

const refresh = vi.fn()
const hash = (value: string) => value.repeat(64)
const props = () => ({
  reference: { projectId: 'project-a', skillId: 'fixture-repair', revision: hash('a') },
  data: skillLifecycle.parse(lifecycleFixture()),
  sequence: 7,
  contentHash: hash('b'),
  exportHash: hash('c'),
  canManage: true,
  validations: [{ validationId: hash('d'), caseId: 'new-independent', status: 'passed' }],
  onRefresh: refresh,
})
beforeEach(() => {
  refresh.mockReset()
  vi.spyOn(api.skills, 'prepareImpact').mockResolvedValue(skillLifecyclePackage.parse(lifecyclePackageFixture()))
  vi.spyOn(api.skills, 'reviewImpact').mockResolvedValue({
    state: 'paused',
    sequence: 8,
    lifecycle: skillLifecycle.parse(lifecycleFixture()),
  })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('同时展示仍在影响的多个根因及固定路径，恢复观察不冒充准入通过', () => {
  const input = props()
  input.data.roots.push({ ...input.data.roots[0], rootId: hash('1'), status: 'restored_pending', reason: 'tool_restored' })
  render(<SkillLifecyclePanel {...input} />)
  expect(screen.getByText(/2 个未解除根因/)).toBeTruthy()
  expect(screen.getByText('已恢复观察，待审阅解除')).toBeTruthy()
  expect(screen.getAllByText(/fixture-repair@/).length).toBe(2)
  expect(screen.getByText(/恢复一个根因不会解除其他根因/)).toBeTruthy()
})
it('viewer 可以读观察但不能准备包或主动扫描', () => {
  render(<SkillLifecyclePanel {...props()} canManage={false} />)
  expect((screen.getByRole('button', { name: '检查当前来源与工具' }) as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: '准备固定影响包' }) as HTMLButtonElement).disabled).toBe(true)
})
it('陈旧水位包被拒绝，不给确认入口', async () => {
  vi.mocked(api.skills.prepareImpact).mockResolvedValue({
    ...skillLifecyclePackage.parse(lifecyclePackageFixture()),
    lifecycleWatermark: hash('0'),
  })
  render(<SkillLifecyclePanel {...props()} />)
  fireEvent.click(screen.getByRole('button', { name: '准备固定影响包' }))
  expect((await screen.findByRole('alert')).textContent).toMatch(/不匹配/)
  expect(screen.queryByLabelText('确认此固定影响包')).toBeNull()
})
it('保留决定绑定固定包与主体，冲突后清除确认且不重试', async () => {
  vi.mocked(api.skills.reviewImpact).mockRejectedValue(new ApiError('状态已变化', 409))
  render(<SkillLifecyclePanel {...props()} />)
  fireEvent.click(screen.getByRole('button', { name: '准备固定影响包' }))
  await screen.findByText('account:reviewer')
  fireEvent.change(screen.getByLabelText('处置决定'), { target: { value: 'retain' } })
  fireEvent.change(screen.getByLabelText('生命周期审阅理由'), { target: { value: '用户授权的助手代办；证据不足，保留根因。' } })
  fireEvent.click(screen.getByLabelText('确认此固定影响包'))
  fireEvent.click(screen.getByRole('button', { name: '提交固定包处置' }))
  await waitFor(() => expect(api.skills.reviewImpact).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.skills.reviewImpact).mock.calls[0][0]).toMatchObject({
    expectedSequence: 7,
    packageId: hash('e'),
    packageHash: hash('f'),
    decision: 'retain',
    replacementRevision: null,
  })
  expect((await screen.findByRole('alert')).textContent).toMatch(/不会自动重试/)
  expect(screen.queryByLabelText('确认此固定影响包')).toBeNull()
})
