import { it, expect } from 'vitest'
import { c2V2Assessment } from './skill-c2-v2.test-fixture'
import { skillAssessment } from './skill-governance.contract'

it('preserves schema 5 calibration admission separately from passing repair facts', () => {
  const value = c2V2Assessment()
  expect(skillAssessment.parse(value)).toEqual(value)
  value.executionQualification.calibration.cyclesUsed = 3
  value.executionQualification.calibration.budgetExceeded = true
  value.executionQualification.calibration.qualified = false
  value.executionQualification.calibration.issues = ['repair_budget_exceeded']
  value.executionQualification.planQualified = false
  value.executionQualification.planIssues = ['calibration:repair_budget_exceeded']
  expect(skillAssessment.parse(value)).toEqual(value)
})

it.each([
  'version',
  'protocol',
  'missing_plan',
  'unbound_trial',
  'fake_qualification',
  'boolean_cycle',
  'missing_calibration',
  'swapped_groups',
])('rejects invalid schema 5 qualification: %s', (change) => {
  const value = c2V2Assessment()
  const raw = value as unknown as Record<string, unknown>
  if (change === 'version') raw.schemaVersion = 6
  if (change === 'protocol') raw.collectorVersion = 'c2-review-1'
  if (change === 'missing_plan') delete raw.executionQualification
  if (change === 'unbound_trial') value.executionQualification.trial.runId = 'foreign'
  if (change === 'fake_qualification') value.executionQualification.calibration.budgetExceeded = true
  if (change === 'boolean_cycle') (value.executionQualification.calibration as unknown as Record<string, unknown>).cyclesUsed = true
  if (change === 'missing_calibration') (value.executionQualification as unknown as Record<string, unknown>).calibration = undefined
  if (change === 'swapped_groups') (value.executionQualification.calibration as unknown as Record<string, unknown>).group = 'skill'
  expect(skillAssessment.safeParse(value).success).toBe(false)
})
