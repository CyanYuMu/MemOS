// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { SkillJudgment, SkillReleaseAssessment } from '../../shared/contract'
import { skillAssessment } from '../lib/skill-governance.contract'
import { releaseAssessment } from '../lib/skill-release.test-fixture'
import { SkillReleaseEvidence } from './SkillReleaseEvidence'
import { releaseJudgmentSupported, releaseBranchSupported } from '../lib/skill-release.judgment'
afterEach(cleanup)
const judgment: SkillJudgment = {
  taskDecision: 'healthy_confirmed',
  applicability: 'in_scope',
  violations: 'none',
  boundary: null,
  usageDecision: 'correct',
  historyDecision: 'retain',
  reason: '用户授权的助手代办',
  eventRefs: [],
  branches: [],
}
it('健康确认与修复成功不混淆', () => {
  const value = skillAssessment.parse(releaseAssessment('healthy')) as SkillReleaseAssessment
  render(<SkillReleaseEvidence assessment={value} />)
  expect(screen.getByText('新版独立任务：健康确认')).toBeTruthy()
  expect(releaseJudgmentSupported(value, judgment)).toBe(true)
  expect(releaseJudgmentSupported(value, { ...judgment, taskDecision: 'repair_confirmed' })).toBe(false)
})
it('工具故障保留原始结果与缺证据边界', () => {
  const value = skillAssessment.parse(releaseAssessment('tool_failure')) as SkillReleaseAssessment
  render(<SkillReleaseEvidence assessment={value} />)
  expect(screen.getByText(/没有业务退出码/)).toBeTruthy()
  expect(releaseJudgmentSupported(value, { ...judgment, taskDecision: 'stop_confirmed', boundary: 'needs_evidence' })).toBe(true)
  expect(releaseJudgmentSupported(value, { ...judgment, taskDecision: 'repair_confirmed' })).toBe(false)
})
it('范围外停止要求范围外判断，未解决保留失败', () => {
  const outside = skillAssessment.parse(releaseAssessment('out_of_scope')) as SkillReleaseAssessment
  expect(releaseJudgmentSupported(outside, { ...judgment, taskDecision: 'stop_confirmed', boundary: 'not_applicable' })).toBe(false)
  expect(
    releaseJudgmentSupported(outside, {
      ...judgment,
      taskDecision: 'stop_confirmed',
      boundary: 'not_applicable',
      applicability: 'out_of_scope',
    }),
  ).toBe(true)
  const value = skillAssessment.parse(releaseAssessment('unresolved')) as SkillReleaseAssessment
  render(<SkillReleaseEvidence assessment={value} />)
  expect(screen.getByText(/原始隐藏检查：failed → failed/)).toBeTruthy()
  expect(releaseJudgmentSupported(value, { ...judgment, taskDecision: 'unresolved_confirmed', boundary: 'unresolved' })).toBe(true)
})
it('隐藏最终检查不能替代送达证据，声明需要先有实际观察', () => {
  const value = skillAssessment.parse(releaseAssessment('repair')) as SkillReleaseAssessment
  const decision = { ...judgment, taskDecision: 'repair_confirmed' as const }
  const branch = {
    branchId: 'verify_scoped_reads#0',
    conditionRefs: ['event-1'],
    actionRefs: ['rule-1', 'event-2'],
    checkRefs: ['check:2:scoped.caller_contract'],
    reason: '合成测试：公开送达与声明',
  }
  expect(releaseBranchSupported(value, branch, decision)).toBe(true)
  expect(releaseBranchSupported(value, { ...branch, checkRefs: ['final:scoped.caller_contract'] }, decision)).toBe(false)
  expect(releaseBranchSupported(value, { ...branch, actionRefs: ['event-2'] }, decision)).toBe(false)
  expect(releaseBranchSupported(value, { ...branch, actionRefs: ['rule-1'] }, decision)).toBe(false)
})
