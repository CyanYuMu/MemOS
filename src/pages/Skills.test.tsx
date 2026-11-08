// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, ApiError } from '../lib/api'
import { makeT } from '../lib/i18n'
import { skillDetail } from '../lib/skills.contract'
import { Skills } from './Skills'

const identity = vi.hoisted(() => ({ projectId: 'project-a', role: 'owner' as string | null, admin: false }))
vi.mock('../lib/store', () => ({
  useT: () => makeT('zh'),
  useProject: () => ({ currentProjectId: identity.projectId, ready: true }),
  useProjectRole: () => ({ role: identity.role }),
  useMeta: () => ({ account: { platformAdmin: identity.admin } }),
}))

const candidate = {
  skill_id: 'fixture-repair',
  revision: 'a'.repeat(64),
  title: 'Mock 与 fixture 修复',
  goal: '恢复行为并保留检错能力',
  purpose: 'repair',
  tags: ['python'],
  applicability: ['pytest mock'],
  exclusions: ['生产故障'],
  prerequisites: ['复现失败'],
  constraints: ['禁止删除断言'],
  acceptance: ['回归通过'],
  limitations: ['尚缺独立验证'],
  entry_rule: 'diagnose',
  exits: { success: '验收通过', not_applicable: '不适用', needs_evidence: '需要证据', unresolved: '停止' },
  rules: [
    {
      rule_id: 'diagnose',
      condition: 'mock 失败',
      observation: '核对调用',
      action: '<script>alert(1)</script>',
      expected_result: '定位契约差异',
      basis: 'empirical',
      branches: [{ when: '证据不足', target: 'exit:needs_evidence' }],
      evidence_links: [
        {
          fact_id: 'fact-1',
          kind: 'supports',
          claim: '支持观察',
          target_kind: 'MemorySnapshot',
          target_id: 'snapshot-1',
          locator: '/memory',
        },
      ],
    },
  ],
  semantic_review: 'pending',
  independent_validation: 'not_run',
  content_hash: 'content-hash',
  source_refs: [],
  host_limits: [],
  requires: [],
}
function detail(readiness = 'BLOCK', state = 'blocked') {
  return {
    projectId: identity.projectId,
    candidate,
    management: {
      state,
      sequence: 7,
      projectionVersion: null,
      exportHash: null,
      readiness: { status: readiness, reasons: readiness === 'PASS' ? [] : ['open_challenges', 'independent_validation_missing'] },
      challenges: ['验收遗漏'],
    },
    validations: [],
    jobs: [],
    releases: [],
    caseOptions: [{ caseId: 'dev-fixture', title: '受控 fixture 任务' }],
  }
}
const reviewPackage = {
  packageId: 'review-1',
  packageHash: 'package-hash',
  sequence: 7,
  skillId: candidate.skill_id,
  revision: candidate.revision,
  readiness: { status: 'PASS', reasons: [] },
  validationIds: ['validation-1'],
  sourceWatermark: ['hash'],
  contentHash: 'content-hash',
  reviewChecklist: [{ ruleId: 'diagnose', branchCount: 1, coveredBranches: ['diagnose#0'] }],
}
beforeEach(() => {
  identity.projectId = 'project-a'
  identity.role = 'owner'
  identity.admin = false
  vi.spyOn(api.skills, 'list').mockResolvedValue({
    items: [
      {
        skillId: candidate.skill_id,
        revision: candidate.revision,
        title: candidate.title,
        purpose: 'repair',
        tags: ['python'],
        state: 'blocked',
        sequence: 7,
        readiness: { status: 'BLOCK', reasons: ['open_challenges'] },
        ruleCount: 1,
        validationCount: 0,
      },
    ],
    total: 1,
  })
  vi.spyOn(api.skills, 'experiments').mockResolvedValue({ items: [], total: 0 })
  vi.spyOn(api.skills, 'detail').mockResolvedValue(detail())
  vi.spyOn(api.skills, 'evaluate').mockResolvedValue({ jobId: 'job-1', status: 'queued' })
  vi.spyOn(api.skills, 'job').mockResolvedValue({
    jobId: 'job-1',
    status: 'passed',
    skillId: candidate.skill_id,
    revision: candidate.revision,
    caseId: 'dev-fixture',
    mode: 'isolated_dev',
    taskResult: { status: 'passed', accepted: true, receiptMatches: true },
    skillUsage: { loading: 'verified', capture: 'complete', usageReview: 'unreviewed', declaredRuleIds: ['diagnose'] },
  })
  vi.spyOn(api.skills, 'prepareReview').mockResolvedValue(reviewPackage)
  vi.spyOn(api.skills, 'publish').mockResolvedValue({ state: 'published', sequence: 8 })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
async function page() {
  const view = render(<Skills />)
  await screen.findByRole('heading', { name: candidate.title })
  return view
}

it('规范修订号与当前原生投影版本和导出摘要并列展示', async () => {
  const exportHash = 'd'.repeat(64)
  const current = detail()
  vi.mocked(api.skills.detail).mockResolvedValue(
    skillDetail.parse({
      ...current,
      management: { ...current.management, projectionVersion: 'management-native-3', exportHash },
      jobs: [{ jobId: 'historical-job', status: 'unknown', revision: candidate.revision, exportHash: 'e'.repeat(64) }],
    }),
  )
  await page()
  expect(screen.getByText('规范修订号').nextElementSibling?.textContent).toBe(candidate.revision)
  expect(screen.getByText('原生投影版本').nextElementSibling?.textContent).toBe('management-native-3')
  expect(screen.getByText('原生导出摘要').nextElementSibling?.textContent).toBe(exportHash)
  expect(screen.getByText(/历史运行保留各自绑定/)).toBeTruthy()
})

it.each([null, 'viewer'])('未知权限和 viewer 无法发起评测或审阅（%s）', async (role) => {
  identity.role = role
  await page()
  expect((screen.getByRole('button', { name: '开始隔离评测' }) as HTMLButtonElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: '生成固定审阅包' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.skills.evaluate).not.toHaveBeenCalled()
})

it('editor 可以评测但不能准备发布', async () => {
  identity.role = 'editor'
  await page()
  expect((screen.getByRole('button', { name: '开始隔离评测' }) as HTMLButtonElement).disabled).toBe(false)
  expect((screen.getByRole('button', { name: '生成固定审阅包' }) as HTMLButtonElement).disabled).toBe(true)
})

it('任务通过和规则使用未审查分别显示，不把开发评测显示为已发布', async () => {
  await page()
  fireEvent.click(screen.getByRole('button', { name: '开始隔离评测' }))
  await screen.findByText('外部任务验收通过')
  expect(screen.getByText('规则使用：未审查')).toBeTruthy()
  expect(screen.getByText(/isolated_dev 仅验证/)).toBeTruthy()
  expect(screen.queryByText('发布状态：已发布')).toBeNull()
})

it('发布绑定精确审阅包、序号，要求逐项人工确认和理由', async () => {
  vi.mocked(api.skills.detail).mockResolvedValue(detail('PASS', 'candidate'))
  await page()
  fireEvent.click(screen.getByRole('button', { name: '生成固定审阅包' }))
  await screen.findByText('package-hash')
  const publish = screen.getByRole('button', { name: '确认发布此版本' }) as HTMLButtonElement
  expect(publish.disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('审阅理由'), { target: { value: '已核对独立任务、分支、来源和适用边界' } })
  fireEvent.click(screen.getByLabelText(/我已核对固定审阅包/))
  fireEvent.click(publish)
  await waitFor(() => expect(api.skills.publish).toHaveBeenCalledTimes(1))
  expect(api.skills.publish).toHaveBeenCalledWith(
    expect.objectContaining({
      projectId: 'project-a',
      skillId: candidate.skill_id,
      revision: candidate.revision,
      packageId: 'review-1',
      packageHash: 'package-hash',
      expectedSequence: 7,
      reason: '已核对独立任务、分支、来源和适用边界',
      operationId: expect.any(String),
    }),
  )
})

it('409 使审阅包失效，只允许重新读取，不能重试发布', async () => {
  vi.mocked(api.skills.detail).mockResolvedValue(detail('PASS', 'candidate'))
  vi.mocked(api.skills.publish).mockRejectedValue(
    new ApiError('状态已变化', 409, 409, undefined, { errorCode: 'sequence_conflict', currentSequence: 8 }),
  )
  await page()
  fireEvent.click(screen.getByRole('button', { name: '生成固定审阅包' }))
  await screen.findByText('package-hash')
  fireEvent.change(screen.getByLabelText('审阅理由'), { target: { value: '已审阅' } })
  fireEvent.click(screen.getByLabelText(/我已核对固定审阅包/))
  fireEvent.click(screen.getByRole('button', { name: '确认发布此版本' }))
  await screen.findByText(/状态冲突，请重新读取/)
  expect(api.skills.publish).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('button', { name: '确认发布此版本' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '重新读取当前版本' }))
  await waitFor(() => expect(api.skills.detail).toHaveBeenCalledTimes(2))
  expect(api.skills.publish).toHaveBeenCalledTimes(1)
})

it('候选文本以纯文本渲染，且阻断原因直接可见', async () => {
  await page()
  expect(screen.getByText('<script>alert(1)</script>')).toBeTruthy()
  expect(document.querySelector('script')).toBeNull()
  expect(screen.getAllByText(/open_challenges/).length).toBeGreaterThan(0)
})

it('切项目丢弃旧项目详情与固定审阅包', async () => {
  vi.mocked(api.skills.detail).mockResolvedValue(detail('PASS', 'candidate'))
  const view = await page()
  fireEvent.click(screen.getByRole('button', { name: '生成固定审阅包' }))
  await screen.findByText('package-hash')
  identity.projectId = 'project-b'
  identity.role = 'viewer'
  vi.mocked(api.skills.list).mockResolvedValue({ items: [], total: 0 })
  view.rerender(<Skills />)
  await screen.findByText('当前项目还没有 Skill 候选')
  expect(screen.queryByText('package-hash')).toBeNull()
  expect(screen.queryByRole('button', { name: '确认发布此版本' })).toBeNull()
})

it('逐条证据通过规则 ID 查询，并保留来源事实', async () => {
  const evidence = vi
    .spyOn(api.skills, 'evidence')
    .mockResolvedValue({ rule: candidate.rules[0], facts: [{ fact_id: 'fact-1', target_kind: 'MemorySnapshot', value: '真实回读内容' }] })
  await page()
  fireEvent.click(screen.getByRole('button', { name: '查看 diagnose 来源片段' }))
  await screen.findByText(/真实回读内容/)
  expect(evidence).toHaveBeenCalledWith({
    projectId: 'project-a',
    skillId: candidate.skill_id,
    revision: candidate.revision,
    ruleId: 'diagnose',
  })
})

it('normal 加载拒绝理由明确显示，不能因为任务通过而忽略', async () => {
  vi.spyOn(api.skills, 'loadPreview').mockResolvedValue({ allowed: false, reasons: ['not_published'] })
  await page()
  fireEvent.click(screen.getByRole('button', { name: '检查 normal 加载准入' }))
  await screen.findByText('normal 加载被拒绝')
  expect(screen.getByText(/此版本尚未发布/)).toBeTruthy()
})

it('BLOCK 审阅包不允许确认发布，具体阻断原因可见', async () => {
  vi.mocked(api.skills.prepareReview).mockResolvedValue({
    ...reviewPackage,
    readiness: { status: 'BLOCK', reasons: ['independent_validation_missing'] },
  })
  await page()
  fireEvent.click(screen.getByRole('button', { name: '生成固定审阅包' }))
  await screen.findByText('package-hash')
  expect((screen.getByLabelText(/我已核对固定审阅包/) as HTMLInputElement).disabled).toBe(true)
  expect((screen.getByRole('button', { name: '确认发布此版本' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api.skills.publish).not.toHaveBeenCalled()
})

it('绑定到其他候选的审阅包不会显示批准按钮', async () => {
  vi.mocked(api.skills.prepareReview).mockResolvedValue({ ...reviewPackage, revision: 'b'.repeat(64) })
  await page()
  fireEvent.click(screen.getByRole('button', { name: '生成固定审阅包' }))
  await screen.findByText(/审阅包绑定内容已变化/)
  expect(screen.queryByRole('button', { name: '确认发布此版本' })).toBeNull()
})

it.each(['needs_evidence', 'block'])('补证据或阻断必须携带理由和当前序号（%s）', async (decision) => {
  const decide = vi.spyOn(api.skills, 'decide').mockResolvedValue({ state: decision === 'block' ? 'blocked' : decision, sequence: 8 })
  await page()
  fireEvent.change(screen.getByLabelText('其他管理决定'), { target: { value: decision } })
  expect((screen.getByRole('button', { name: '提交管理决定' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('审阅理由'), { target: { value: '缺少异常分支证据' } })
  fireEvent.click(screen.getByRole('button', { name: '提交管理决定' }))
  await waitFor(() =>
    expect(decide).toHaveBeenCalledWith(
      expect.objectContaining({
        projectId: 'project-a',
        expectedSequence: 7,
        decision,
        reason: '缺少异常分支证据',
        operationId: expect.any(String),
      }),
    ),
  )
})

it.each(['pause', 'withdraw'])('已发布版本支持带理由的状态变更（%s）', async (action) => {
  vi.mocked(api.skills.detail).mockResolvedValue(detail('PASS', 'published'))
  const change = vi.spyOn(api.skills, 'status').mockResolvedValue({ state: action === 'pause' ? 'paused' : 'withdrawn', sequence: 8 })
  await page()
  fireEvent.change(screen.getByLabelText('其他管理决定'), { target: { value: action } })
  fireEvent.change(screen.getByLabelText('审阅理由'), { target: { value: '工具契约发生变化，先停止新加载' } })
  fireEvent.click(screen.getByRole('button', { name: '提交管理决定' }))
  await waitFor(() =>
    expect(change).toHaveBeenCalledWith(expect.objectContaining({ action, expectedSequence: 7, reason: '工具契约发生变化，先停止新加载' })),
  )
})

it('运行查询失败可重试查询但不会重复创建任务', async () => {
  vi.mocked(api.skills.job).mockRejectedValueOnce(new Error('服务暂时不可用'))
  await page()
  fireEvent.click(screen.getByRole('button', { name: '开始隔离评测' }))
  await screen.findByText(/运行状态读取失败/)
  fireEvent.click(screen.getByRole('button', { name: '重试读取运行状态' }))
  await screen.findByText('外部任务验收通过')
  expect(api.skills.evaluate).toHaveBeenCalledTimes(1)
  expect(api.skills.job).toHaveBeenCalledTimes(2)
})

it('结果未知或验收不匹配时不能显示任务通过', async () => {
  vi.mocked(api.skills.job).mockResolvedValue({
    jobId: 'job-1',
    status: 'unknown',
    taskResult: { status: 'passed', accepted: true, receiptMatches: false },
    skillUsage: { loading: 'unknown', capture: 'partial', usageReview: 'unreviewed', declaredRuleIds: [] },
    errorCode: 'receipt_mismatch',
  })
  await page()
  fireEvent.click(screen.getByRole('button', { name: '开始隔离评测' }))
  await screen.findByText('结果未知')
  expect(screen.queryByText('外部任务验收通过')).toBeNull()
  expect(screen.getByText('加载未证实')).toBeTruthy()
  expect(screen.getByText(/receipt_mismatch/)).toBeTruthy()
})

it('详情接口失败有可恢复错误，不显示旧详情', async () => {
  vi.mocked(api.skills.detail).mockRejectedValueOnce(new Error('详情读取失败'))
  render(<Skills />)
  await screen.findByRole('alert')
  expect(screen.queryByRole('heading', { name: candidate.title })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '重试读取详情' }))
  await screen.findByRole('heading', { name: candidate.title })
})

it('搜索和状态筛选不会混用其他版本的审阅包', async () => {
  await page()
  fireEvent.change(screen.getByLabelText('管理状态'), { target: { value: 'published' } })
  await screen.findByText('没有匹配的版本，请调整筛选条件。')
  expect(screen.queryByRole('heading', { name: candidate.title })).toBeNull()
  fireEvent.change(screen.getByLabelText('管理状态'), { target: { value: '' } })
  await screen.findByRole('heading', { name: candidate.title })
  fireEvent.change(screen.getByLabelText('搜索 Skill'), { target: { value: 'no-such-skill' } })
  expect(screen.queryByRole('heading', { name: candidate.title })).toBeNull()
})
