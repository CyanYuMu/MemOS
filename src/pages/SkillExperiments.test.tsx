// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api } from '../lib/api'
import { SkillExperiments } from './SkillExperiments'

const item = {
  experimentId: 'experiment-1',
  skillId: 'fixture-repair',
  revision: 'a'.repeat(64),
  manifestHash: 'b'.repeat(64),
  status: 'frozen' as const,
  planned: 21,
  completed: 0,
}
const detail = {
  ...item,
  groups: ['none', 'raw_memory', 'skill'] as const,
  budget: { timeoutSeconds: 240, maxTurns: 12, maxBudgetUsd: 3, contextBytes: 32768 },
  rows: [],
  summary: { groups: [], completePairs: 0 },
  publication: 'not_approved' as const,
  limitations: ['仅七个独立场景'],
}
beforeEach(() => {
  vi.spyOn(api.skills, 'experiments').mockResolvedValue({ items: [item], total: 1 })
  vi.spyOn(api.skills, 'experiment').mockResolvedValue({ ...detail, groups: [...detail.groups] })
  vi.spyOn(api.skills, 'startExperiment').mockResolvedValue({ ...item, status: 'running' })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('展示固定三组和身份，viewer 不能开始', async () => {
  render(<SkillExperiments projectId="project-a" canWrite={false} />)
  await screen.findByText(item.manifestHash)
  expect(screen.getByText(/原始 Memory/)).toBeTruthy()
  expect(screen.getByText(/21 次执行/)).toBeTruthy()
  expect((screen.getByRole('button', { name: '开始固定实验' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.skills.startExperiment).not.toHaveBeenCalled()
})
it('开始只提交登记实验身份，后续只查询、不重复开始', async () => {
  render(<SkillExperiments projectId="project-a" canWrite />)
  await screen.findByText(item.manifestHash)
  vi.mocked(api.skills.experiment).mockResolvedValue({ ...detail, groups: [...detail.groups], status: 'completed', completed: 21 })
  fireEvent.click(screen.getByRole('button', { name: '开始固定实验' }))
  await screen.findByText('执行结束，仍需审阅')
  expect(api.skills.startExperiment).toHaveBeenCalledTimes(1)
  expect(api.skills.startExperiment).toHaveBeenCalledWith({
    projectId: 'project-a',
    experimentId: 'experiment-1',
    operationId: expect.any(String),
  })
})
it('未知与人工待审独立显示，不把完成或行为通过当作发布', async () => {
  vi.mocked(api.skills.experiment).mockResolvedValue({
    ...detail,
    groups: [...detail.groups],
    status: 'completed',
    completed: 21,
    rows: [
      {
        cellId: 'cell-1',
        caseId: 'holdout-one',
        group: 'skill',
        status: 'unknown',
        assessmentKind: 'manual_boundary',
        repairPass: null,
        boundaryPass: null,
        branchSupported: null,
        criticalViolation: null,
        manualReview: 'pending',
        taskAccepted: false,
        receiptVerified: true,
        loading: 'verified',
        capture: 'complete',
        elapsedSeconds: 12,
        reportedCostUsd: null,
        toolCalls: 3,
        failureReasons: ['manual_review_required'],
      },
    ],
    summary: {
      groups: [
        {
          group: 'skill',
          primaryRepair: { passed: 2, denominator: 3, planned: 3 },
          boundary: { taskSolved: 2, planned: 4, pendingReview: 4 },
          unknown: 1,
          infraInvalid: 0,
          elapsedSeconds: 12,
          reportedCostUsd: null,
          costUnknown: 1,
        },
      ],
      completePairs: 7,
    },
  })
  render(<SkillExperiments projectId="project-a" canWrite />)
  await screen.findByText('holdout-one')
  expect(screen.getByText('结果未知')).toBeTruthy()
  expect(screen.getByText('2 / 3')).toBeTruthy()
  expect(screen.getAllByText('待人工审阅').length).toBeGreaterThan(0)
  expect(screen.getByText(/实验完成不代表发布批准/)).toBeTruthy()
  expect(screen.queryByText('已发布')).toBeNull()
})
it('拒绝混入另一个实验的详情', async () => {
  vi.mocked(api.skills.experiment).mockResolvedValue({ ...detail, groups: [...detail.groups], experimentId: 'foreign' })
  render(<SkillExperiments projectId="project-a" canWrite />)
  await screen.findByText(/实验身份与当前选择不匹配/)
  expect(screen.queryByText(item.manifestHash)).toBeNull()
})
it('查询失败只允许刷新，不自动重试模型执行', async () => {
  vi.mocked(api.skills.experiment).mockRejectedValue(new Error('查询暂时不可用'))
  render(<SkillExperiments projectId="project-a" canWrite />)
  await screen.findByText('查询暂时不可用')
  fireEvent.click(screen.getByRole('button', { name: '刷新实验' }))
  await waitFor(() => expect(api.skills.experiment).toHaveBeenCalledTimes(2))
  expect(api.skills.startExperiment).not.toHaveBeenCalled()
})
it('中断实验不提供隐式重跑入口', async () => {
  vi.mocked(api.skills.experiment).mockResolvedValue({ ...detail, groups: [...detail.groups], status: 'interrupted', completed: 1 })
  render(<SkillExperiments projectId="project-a" canWrite />)
  await screen.findByText('执行中断，保留未知记录')
  expect((screen.getByRole('button', { name: '开始固定实验' }) as HTMLButtonElement).disabled).toBe(true)
})
