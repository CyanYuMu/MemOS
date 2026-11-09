// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { SkillAssessment, SkillAdjudicationPackage, SkillGovernanceState, SkillBranchAssessment } from '../../shared/contract'
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

const branchAssessment: SkillBranchAssessment = {
  ...assessment,
  schemaVersion: 2,
  collectorVersion: 'branch-review-1',
  collectorDigest: hash('5'),
  runId: 'run',
  attemptId: 'attempt',
  caseRevision: '1',
  lineageId: 'independent-scoped',
  split: 'holdout',
  purpose: 'actual',
  caseId: 'branch-scoped-table',
  behaviorChanged: true,
  originalStatus: 'passed',
  verification: { ...assessment.verification, status: 'passed', protocol: 'branch-receipt-1' },
  initialVerification: {
    status: 'failed',
    receiptSha256: hash('6'),
    protocol: 'branch-receipt-1',
    matches: true,
    candidateSnapshot: hash('7'),
  },
  publicVerification: {
    initialExitCode: 1,
    finalExitCode: 0,
    recordsVerified: true,
    deliveriesVerified: true,
    deliveryRefs: [
      { checkIndex: 1, startedEvent: 'event-start', completedEvent: 'event-1' },
      { checkIndex: 2, startedEvent: 'event-start-2', completedEvent: 'event-2' },
    ],
  },
  outcome: 'repair_candidate',
  batchId: hash('8'),
  taskDigest: hash('9'),
  branchCandidates: ['confirm_contract#3'],
  events: [
    ...assessment.events,
    { id: 'event-start', kind: 'tool_started', value: { display: { tool_name: 'Read' } } },
    { id: 'event-start-2', kind: 'tool_started', value: { display: { tool_name: 'mcp__memos_run__run_visible_checks' } } },
    { id: 'event-2', kind: 'tool_completed', value: { display: {} } },
    { id: 'rule-1', kind: 'rule_declaration', value: { display: { rule_id: 'confirm_contract', phase: 'select' } } },
  ],
}

function mountBranch(value = branchAssessment) {
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, assessment: value })
  return mount(true, vi.fn(), { ...data, assessments: [value] })
}

async function prepareRepair() {
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  await screen.findByText('固定裁决包')
  fireEvent.change(screen.getByLabelText('任务判断'), { target: { value: 'repair_confirmed' } })
  fireEvent.change(screen.getByLabelText('违规判断'), { target: { value: 'none' } })
  fireEvent.click(screen.getByLabelText('引用事件 event-1'))
  fireEvent.change(screen.getByLabelText('裁决理由'), { target: { value: 'Reviewed both states and received checks' } })
  fireEvent.click(screen.getByLabelText('我已审阅证据，确认提交我的判断'))
}

it('shows initial/final and delivered public checks separately from historical rechecks', async () => {
  mountBranch()
  expect(screen.getByText('初始 / 最终隐藏验收')).toBeTruthy()
  expect(screen.queryByText('原验收 / 当前复验')).toBeNull()
  expect(screen.getByText('failed / passed')).toBeTruthy()
  expect(screen.getByText('公开检查：1 → 0')).toBeTruthy()
  expect(screen.getByText('执行工件已核验；工具返回已送达')).toBeTruthy()
  expect(screen.getByText('待确认的修复')).toBeTruthy()
  expect(screen.getByText(/候选分支不代表已覆盖/)).toBeTruthy()
  await prepareRepair()
  expect((screen.getByRole('option', { name: '合理停止' }) as HTMLOptionElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(false)
})

it.each(['initial_pass', 'initial_unbound', 'public_failure', 'undelivered', 'unverified', 'incomplete'])(
  'cannot confirm a branch repair with %s even when final hidden checks pass',
  async (change) => {
    const value = structuredClone(branchAssessment)
    if (change === 'initial_pass') value.initialVerification.status = 'passed'
    if (change === 'initial_unbound') value.initialVerification.matches = false
    if (change === 'public_failure') value.publicVerification.finalExitCode = 1
    if (change === 'undelivered') value.publicVerification.deliveriesVerified = false
    if (change === 'unverified') value.publicVerification.recordsVerified = false
    if (change === 'incomplete') value.outcome = 'incomplete'
    mountBranch(value)
    await prepareRepair()
    expect((screen.getByRole('option', { name: '修复成立' }) as HTMLOptionElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.skills.adjudicate).not.toHaveBeenCalled()
  },
)

it('requires the selected branch rule declaration in addition to observation and action references', async () => {
  mountBranch()
  await prepareRepair()
  fireEvent.change(screen.getByLabelText('规则使用'), { target: { value: 'correct' } })
  fireEvent.change(screen.getByLabelText('适用范围判断'), { target: { value: 'in_scope' } })
  fireEvent.click(screen.getByLabelText('计入分支 confirm_contract#3'))
  function select(label: string, ids: string[]) {
    const input = screen.getByLabelText(label) as HTMLSelectElement
    for (const option of input.options) option.selected = ids.includes(option.value)
    fireEvent.change(input)
  }
  select('confirm_contract#3 条件证据', ['event-1'])
  select('confirm_contract#3 行动证据', ['event-start'])
  select('confirm_contract#3 验收证据', ['diagnosis'])
  fireEvent.change(screen.getByLabelText('confirm_contract#3 说明'), { target: { value: 'Observed defect, action and declared rule' } })
  const submit = screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement
  expect(submit.disabled).toBe(true)
  select('confirm_contract#3 行动证据', ['event-start', 'rule-1'])
  expect(submit.disabled).toBe(false)
  fireEvent.change(screen.getByLabelText('任务判断'), { target: { value: 'needs_evidence' } })
  expect(submit.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('任务判断'), { target: { value: 'repair_confirmed' } })
  select('confirm_contract#3 条件证据', ['rule-1'])
  expect(submit.disabled).toBe(true)
})

it('shows every schema 3 checkpoint and requires check-specific evidence selected by a human', async () => {
  const { extensionAssessment } = await import('../lib/skill-extension.test-fixture')
  const value = extensionAssessment()
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, assessment: value })
  mount(true, vi.fn(), { ...data, assessments: [value] })
  expect(screen.getByText('组合任务的过程证据')).toBeTruthy()
  expect(screen.getByText(/保守的编辑尝试批次/)).toBeTruthy()
  expect(screen.getByText('公开检查 #2')).toBeTruthy()
  expect(screen.getByText('中间隐藏复核')).toBeTruthy()
  await prepareRepair()
  expect((screen.getByLabelText('计入分支 verify_scoped_reads#0') as HTMLInputElement).checked).toBe(false)
  fireEvent.click(screen.getByLabelText('计入分支 verify_scoped_reads#0'))
  const options = screen.getByLabelText('verify_scoped_reads#0 验收证据') as HTMLSelectElement
  expect([...options.options].map((option) => option.value)).toContain('check:2:scoped.caller_contract')
  expect([...options.options].map((option) => option.value)).toContain('check:3:dream.caller_contract')
  expect([...options.options].some((option) => option.selected)).toBe(false)
  expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(true)
})

it('shows C2 initial healthy Dream and keeps healthy-path coverage for human selection', async () => {
  const { c2Assessment } = await import('../lib/skill-c2.test-fixture')
  const value = c2Assessment()
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, assessment: value })
  mount(true, vi.fn(), { ...data, assessments: [value] })
  expect(screen.getByText('scoped 读修复与健康 Dream 复验')).toBeTruthy()
  expect(screen.getByText('初始隐藏检查：scoped failed · Dream passed')).toBeTruthy()
  expect(screen.getByText('公开检查 #2')).toBeTruthy()
  expect(screen.queryByText('中间隐藏复核')).toBeNull()
  await prepareRepair()
  expect((screen.getByLabelText('计入分支 verify_scoped_reads#1') as HTMLInputElement).checked).toBe(false)
  expect(api.skills.adjudicate).not.toHaveBeenCalled()
})

it('allows an overbudget repair judgment but blocks correct use and all branch coverage', async () => {
  const { extensionAssessment } = await import('../lib/skill-extension.test-fixture')
  const value = extensionAssessment()
  value.processVerification.cyclesUsed = 3
  value.processVerification.budgetExceeded = true
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, assessment: value })
  mount(true, vi.fn(), { ...data, assessments: [value] })
  expect(screen.getByText(/编辑尝试已超出两轮预算/)).toBeTruthy()
  await prepareRepair()
  expect((screen.getByRole('option', { name: '修复成立' }) as HTMLOptionElement).disabled).toBe(false)
  expect((screen.getByRole('option', { name: '正确使用' }) as HTMLOptionElement).disabled).toBe(true)
  expect((screen.getByLabelText('计入分支 verify_scoped_reads#0') as HTMLInputElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('规则使用'), { target: { value: 'correct' } })
  expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(true)
})

it('cannot confirm final green checks when schema 3 process evidence is incomplete', async () => {
  const { extensionAssessment } = await import('../lib/skill-extension.test-fixture')
  const value = extensionAssessment()
  value.processVerification.complete = false
  value.processVerification.issues = ['unverified_edits']
  value.processVerification.unverifiedEdits = true
  value.outcome = 'incomplete'
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, assessment: value })
  mount(true, vi.fn(), { ...data, assessments: [value] })
  expect(screen.getByText(/过程证据不完整/)).toBeTruthy()
  await prepareRepair()
  expect((screen.getByRole('option', { name: '修复成立' }) as HTMLOptionElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(true)
})

it('submits the exact manually selected checkpoint references without creating coverage automatically', async () => {
  const { extensionAssessment } = await import('../lib/skill-extension.test-fixture')
  const value = extensionAssessment()
  vi.mocked(api.skills.prepareAdjudication).mockResolvedValue({ ...packet, assessment: value })
  mount(true, vi.fn(), { ...data, assessments: [value] })
  await prepareRepair()
  expect(api.skills.adjudicate).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('规则使用'), { target: { value: 'correct' } })
  fireEvent.change(screen.getByLabelText('适用范围判断'), { target: { value: 'in_scope' } })
  fireEvent.click(screen.getByLabelText('计入分支 verify_scoped_reads#0'))
  function select(label: string, ids: string[]) {
    const input = screen.getByLabelText(label) as HTMLSelectElement
    for (const option of input.options) option.selected = ids.includes(option.value)
    fireEvent.change(input)
  }
  const checkRefs = value.processVerification.checkpoints
    .slice(1)
    .flatMap((checkpoint) => checkpoint.checks.map((check) => `check:${checkpoint.checkIndex}:${check.name}`))
  select('verify_scoped_reads#0 条件证据', ['event-2'])
  select('verify_scoped_reads#0 行动证据', ['edit-2', 'rule-1'])
  select('verify_scoped_reads#0 验收证据', checkRefs)
  fireEvent.change(screen.getByLabelText('verify_scoped_reads#0 说明'), {
    target: { value: 'Reviewed observed intermediate state, timely declaration and next edit' },
  })
  fireEvent.click(screen.getByRole('button', { name: '提交我的裁决' }))
  await waitFor(() => expect(api.skills.adjudicate).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.skills.adjudicate).mock.calls[0][0].judgment.branches[0]).toMatchObject({
    branchId: 'verify_scoped_reads#0',
    checkRefs,
    conditionRefs: ['event-2'],
    actionRefs: ['rule-1', 'edit-2'],
  })
})
