// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { SkillAssessment, SkillAdjudicationPackage, SkillGovernanceState } from '../../shared/contract'
import { api, ApiError } from '../lib/api'
import { SkillGovernance } from './SkillGovernance'

const hash = (c: string) => c.repeat(64)
const reference = { projectId: 'project-a', skillId: 'fixed-skill', revision: hash('a') }
const assessment: SkillAssessment = {
  assessmentId: hash('b'),
  validationId: hash('c'),
  caseId: 'holdout-fixture',
  contentHash: hash('d'),
  exportHash: hash('e'),
  candidateSnapshot: hash('f'),
  reportSha256: hash('1'),
  eventsSha256: hash('2'),
  captureComplete: true,
  loadVerified: true,
  behaviorChanged: false,
  originalStatus: 'needs_review',
  verification: {
    status: 'needs_review',
    receiptSha256: hash('3'),
    protocol: 'p41.1',
    matches: true,
    sameFinalPatch: true,
    posthoc: false,
  },
  checks: [{ name: 'diagnosis', status: 'passed' }],
  events: [{ id: 'event-1', kind: 'tool_completed', value: { display: '<script>bad()</script>' } }],
  branchCandidates: [],
  coveragePlanHash: hash('4'),
}
const data: SkillGovernanceState = {
  assessments: [assessment],
  currentAssessments: { [assessment.validationId]: assessment.assessmentId },
  adjudications: [],
  coverage: [{ branchId: 'confirm_contract#0', status: 'missing', adjudicationIds: [] }],
  qualification: {},
}
const packet: SkillAdjudicationPackage = {
  ...reference,
  kind: 'adjudication',
  sequence: 8,
  contentHash: assessment.contentHash,
  exportHash: assessment.exportHash,
  sourceWatermark: [hash('5')],
  assessmentId: assessment.assessmentId,
  assessment,
  validation: { status: 'unknown' },
  activeAdjudicationIds: [],
  actor: 'account:owner',
  preparedAt: '2026-11-09T00:00:00Z',
  packageId: hash('6'),
  packageHash: hash('7'),
}

function mount(canManage = true, onRefresh = vi.fn(), governance = data) {
  return {
    ...render(
      <SkillGovernance
        reference={reference}
        data={governance}
        sequence={8}
        contentHash={assessment.contentHash}
        exportHash={assessment.exportHash}
        canManage={canManage}
        onRefresh={onRefresh}
      />,
    ),
    onRefresh,
  }
}

beforeEach(() => {
  vi.spyOn(api.skills, 'prepareAdjudication').mockResolvedValue(packet)
  vi.spyOn(api.skills, 'adjudicate').mockResolvedValue({
    adjudicationId: hash('8'),
    sequence: 9,
    state: 'candidate',
    contribution: { taskResolved: true, repairConfirmed: false },
  })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('shows missing coverage and prohibits non-managers from preparing a judgment', () => {
  mount(false)
  expect(screen.getByText('已覆盖 0 / 1 个分支')).toBeTruthy()
  expect((screen.getByRole('button', { name: '准备人工裁决包' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.skills.prepareAdjudication).not.toHaveBeenCalled()
})

it('requires an explicit decision and submits the exact package without publishing', async () => {
  mount()
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  await screen.findByText('固定裁决包')
  const submit = screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement
  expect(submit.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('任务判断'), { target: { value: 'stop_confirmed' } })
  fireEvent.change(screen.getByLabelText('退出类型'), { target: { value: 'unresolved' } })
  fireEvent.change(screen.getByLabelText('违规判断'), { target: { value: 'none' } })
  fireEvent.click(screen.getByLabelText('引用事件 event-1'))
  fireEvent.change(screen.getByLabelText('裁决理由'), { target: { value: 'Synthetic review of a reasonable stop' } })
  fireEvent.click(screen.getByLabelText('我已审阅证据，确认提交我的判断'))
  fireEvent.click(submit)
  await waitFor(() => expect(api.skills.adjudicate).toHaveBeenCalledTimes(1))
  const value = vi.mocked(api.skills.adjudicate).mock.calls[0][0]
  expect(value).toMatchObject({
    ...reference,
    packageId: packet.packageId,
    packageHash: packet.packageHash,
    expectedSequence: 8,
    supersedes: null,
    judgment: { taskDecision: 'stop_confirmed', boundary: 'unresolved', eventRefs: ['event-1'], branches: [] },
  })
  expect(document.querySelector('script')).toBeNull()
})

it('drops a conflicting package and never retries the mutation automatically', async () => {
  vi.mocked(api.skills.adjudicate).mockRejectedValue(new ApiError('状态变化', 409))
  mount()
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  await screen.findByText('固定裁决包')
  fireEvent.change(screen.getByLabelText('裁决理由'), { target: { value: 'Need additional evidence' } })
  fireEvent.click(screen.getByLabelText('我已审阅证据，确认提交我的判断'))
  fireEvent.click(screen.getByRole('button', { name: '提交我的裁决' }))
  await screen.findByText(/状态已变化，请重新读取/)
  expect(screen.queryByText('固定裁决包')).toBeNull()
  expect(api.skills.adjudicate).toHaveBeenCalledTimes(1)
})

it('rejects a package from another project before presenting an approval form', async () => {
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, projectId: 'project-b' })
  mount()
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  await screen.findByText(/裁决包与当前版本不匹配/)
  expect(screen.queryByText('固定裁决包')).toBeNull()
  expect(api.skills.adjudicate).not.toHaveBeenCalled()
})

it('requires explicit supersession when correcting an active judgment', async () => {
  const priorId = hash('9')
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, activeAdjudicationIds: [priorId] })
  mount(true, vi.fn(), {
    ...data,
    adjudications: [
      {
        adjudicationId: priorId,
        assessmentId: assessment.assessmentId,
        validationId: assessment.validationId,
        supersedes: null,
        actor: 'prior-owner',
        reason: 'Synthetic previous judgment',
        decidedAt: '2026-11-09T00:00:00Z',
        contribution: {},
      },
    ],
  })
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  await screen.findByText('固定裁决包')
  fireEvent.change(screen.getByLabelText('裁决理由'), { target: { value: 'Correction requires more evidence' } })
  fireEvent.click(screen.getByLabelText('我已审阅证据，确认提交我的判断'))
  const submit = screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement
  expect(submit.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('明确纠正已有裁决'), { target: { value: priorId } })
  fireEvent.click(submit)
  await waitFor(() => expect(api.skills.adjudicate).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.skills.adjudicate).mock.calls[0][0].supersedes).toBe(priorId)
})

it('does not treat a modified behavior file as a reasonable unchanged stop', async () => {
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({
    ...packet,
    assessment: { ...assessment, behaviorChanged: true },
  })
  mount()
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  await screen.findByText('固定裁决包')
  fireEvent.change(screen.getByLabelText('任务判断'), { target: { value: 'stop_confirmed' } })
  fireEvent.change(screen.getByLabelText('退出类型'), { target: { value: 'needs_evidence' } })
  fireEvent.change(screen.getByLabelText('违规判断'), { target: { value: 'none' } })
  fireEvent.click(screen.getByLabelText('引用事件 event-1'))
  fireEvent.change(screen.getByLabelText('裁决理由'), { target: { value: 'Synthetic changed stop' } })
  fireEvent.click(screen.getByLabelText('我已审阅证据，确认提交我的判断'))
  expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.skills.adjudicate).not.toHaveBeenCalled()
})

it('does not count an already healthy unchanged task as a repair', async () => {
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({
    ...packet,
    assessment: { ...assessment, verification: { ...assessment.verification, status: 'passed' } },
  })
  mount()
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  await screen.findByText('固定裁决包')
  fireEvent.change(screen.getByLabelText('任务判断'), { target: { value: 'repair_confirmed' } })
  fireEvent.change(screen.getByLabelText('违规判断'), { target: { value: 'none' } })
  fireEvent.click(screen.getByLabelText('引用事件 event-1'))
  fireEvent.change(screen.getByLabelText('裁决理由'), { target: { value: 'No behavior change is not a repair' } })
  fireEvent.click(screen.getByLabelText('我已审阅证据，确认提交我的判断'))
  expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.skills.adjudicate).not.toHaveBeenCalled()
})
