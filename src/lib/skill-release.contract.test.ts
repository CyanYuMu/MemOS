import { expect, it } from 'vitest'
import { skillAssessment } from './skill-governance.contract'
import { releaseAssessment } from './skill-release.test-fixture'

it.each(['repair', 'healthy', 'missing_evidence', 'out_of_scope', 'unresolved', 'tool_failure'] as const)(
  'schema6 显式保留 %s 结局和底层检查',
  (kind) => {
    expect(skillAssessment.parse(releaseAssessment(kind))).toMatchObject({ schemaVersion: 6, taskOutcome: { kind } })
  },
)
it('schema6 健康任务支持一次已送达检查，不能被解释为修复', () => {
  const result = skillAssessment.parse(releaseAssessment('healthy'))
  expect(result).toMatchObject({ behaviorChanged: false, outcome: 'healthy_candidate', processVerification: { cyclesUsed: 0 } })
})
it('schema6 不允许缺失判定、未知字段或旧协议冒充', () => {
  const value: Record<string, unknown> = releaseAssessment()
  delete value.taskOutcome
  expect(skillAssessment.safeParse(value).success).toBe(false)
  expect(skillAssessment.safeParse({ ...releaseAssessment(), unregistered: true }).success).toBe(false)
  expect(skillAssessment.safeParse({ ...releaseAssessment(), collectorVersion: 'c2-review-2' }).success).toBe(false)
})
it('当前运行与批次或最终凭条不匹配时拒绝', () => {
  const value = releaseAssessment()
  value.executionQualification.trial.reportSha256 = '0'.repeat(64)
  expect(skillAssessment.safeParse(value).success).toBe(false)
})
it('故障不能变成业务绿灯，也不能缺少真实送达事件', () => {
  const value = releaseAssessment('tool_failure')
  value.publicVerification.fault!.completedEvent = 'missing-event'
  expect(skillAssessment.safeParse(value).success).toBe(false)
})
it('健康确认与有限修改后未解决必须满足相应事实', () => {
  const healthy = releaseAssessment('healthy')
  healthy.behaviorChanged = true
  expect(skillAssessment.safeParse(healthy).success).toBe(false)
  const unresolved = releaseAssessment('unresolved')
  unresolved.verification.status = 'passed'
  expect(skillAssessment.safeParse(unresolved).success).toBe(false)
})
it('零修改的未解决结局保持停止，不能当成有限修复后未解决', () => {
  const value = releaseAssessment('unresolved', true)
  expect(skillAssessment.parse(value)).toMatchObject({ outcome: 'stop_candidate', behaviorChanged: false })
})

it('未解决必须保留 Dream 目标失败，不能用 scoped 失败顶替', () => {
  const value = releaseAssessment('out_of_scope')
  value.taskOutcome.kind = 'unresolved'
  expect(skillAssessment.safeParse(value).success).toBe(false)
})

it.each(['unresolved', 'tool_failure'] as const)('%s 不能把新增行为回归判为合格停止', (kind) => {
  const value = releaseAssessment(kind)
  const initial = new Map(value.initialVerification.checks.map((row) => [row.name, row.status]))
  const regression = value.checks.find((row) => initial.get(row.name) === 'passed')!
  regression.status = 'failed'
  value.verification.status = 'failed'
  value.originalStatus = 'failed'
  if (kind === 'unresolved') {
    const final = value.processVerification.checkpoints.at(-1)!
    final.checks = structuredClone(value.checks)
    final.groupStatus.scoped = 'failed'
  }
  expect(skillAssessment.safeParse(value).success).toBe(false)
})
it('任务谓词满足但采集不全时保留 incomplete 和不合规计划', () => {
  const value = releaseAssessment('healthy')
  value.captureComplete = false
  value.outcome = 'incomplete'
  value.executionQualification.trial.captureComplete = false
  value.executionQualification.trial.qualified = false
  value.executionQualification.trial.issues = ['capture_incomplete']
  value.executionQualification.planQualified = false
  value.executionQualification.planIssues = ['trial:capture_incomplete']
  expect(skillAssessment.parse(value)).toMatchObject({ outcome: 'incomplete', taskOutcome: { eligible: true } })
})
