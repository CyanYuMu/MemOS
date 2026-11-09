import { expect, it } from 'vitest'
import { c2Assessment } from './skill-c2.test-fixture'
import { skillAssessment } from './skill-governance.contract'

it('accepts C2 evidence with initial hidden healthy Dream and no C1 midpoint', () => {
  const value = c2Assessment()
  expect(skillAssessment.parse(value)).toEqual(value)
})

it.each(['receipt', 'process', 'collector', 'initial_checks', 'midpoint', 'missing_group', 'delivery', 'unknown_version'])(
  'rejects malformed C2 evidence: %s',
  (change) => {
    const value = c2Assessment()
    const raw = value as unknown as Record<string, unknown>
    if (change === 'receipt') raw.verification = { ...value.verification, protocol: 'extension-receipt-1' }
    if (change === 'process') raw.processVerification = { ...value.processVerification, version: 'extension-process-1' }
    if (change === 'collector') raw.collectorVersion = 'extension-review-1'
    if (change === 'initial_checks') raw.initialVerification = { ...value.initialVerification, checks: undefined }
    if (change === 'midpoint') raw.intermediateVerifications = [{ snapshot: value.candidateSnapshot }]
    if (change === 'missing_group') value.initialVerification.checks.pop()
    if (change === 'delivery') value.processVerification.checkpoints[1].completedEvent = 'missing'
    if (change === 'unknown_version') raw.schemaVersion = 5
    expect(skillAssessment.safeParse(value).success).toBe(false)
  },
)
