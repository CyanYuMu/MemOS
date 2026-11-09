import { expect, it } from 'vitest'
import { skillAssessment } from './skill-governance.contract'

const hash = 'a'.repeat(64)
const common = {
  assessmentId: hash,
  validationId: hash,
  caseId: 'branch-scoped-table',
  contentHash: hash,
  exportHash: hash,
  candidateSnapshot: hash,
  reportSha256: hash,
  eventsSha256: hash,
  captureComplete: true,
  loadVerified: true,
  behaviorChanged: true,
  originalStatus: 'passed',
  verification: {
    status: 'passed',
    receiptSha256: hash,
    protocol: 'branch-receipt-1',
    matches: true,
    sameFinalPatch: true,
    posthoc: false,
  },
  checks: [{ name: 'scope', status: 'passed' }],
  events: [],
  branchCandidates: [],
  coveragePlanHash: hash,
}
const branch = {
  ...common,
  schemaVersion: 2,
  collectorVersion: 'branch-review-1',
  collectorDigest: hash,
  runId: 'run',
  attemptId: 'attempt',
  caseRevision: '1',
  lineageId: 'independent-scoped',
  split: 'holdout',
  purpose: 'actual',
  initialVerification: { status: 'failed', receiptSha256: hash, protocol: 'branch-receipt-1', matches: true, candidateSnapshot: hash },
  publicVerification: {
    initialExitCode: 1,
    finalExitCode: 0,
    recordsVerified: true,
    deliveriesVerified: true,
    deliveryRefs: [
      { checkIndex: 1, startedEvent: 'event-1', completedEvent: 'event-2' },
      { checkIndex: 2, startedEvent: 'event-3', completedEvent: 'event-4' },
    ],
  },
  events: [
    { id: 'event-1', kind: 'tool_started', value: {} },
    { id: 'event-2', kind: 'tool_completed', value: {} },
    { id: 'event-3', kind: 'tool_started', value: {} },
    { id: 'event-4', kind: 'tool_completed', value: {} },
  ],
  outcome: 'repair_candidate',
  batchId: hash,
  taskDigest: hash,
}

it('preserves old assessment fields without reinterpreting them as branch evidence', () => {
  expect(skillAssessment.parse({ ...common, schemaVersion: 1, historicalField: 'kept' })).toHaveProperty('historicalField', 'kept')
  expect(skillAssessment.safeParse(common).success).toBe(true)
})

it('accepts a complete versioned branch assessment', () => {
  expect(skillAssessment.parse(branch)).toEqual(branch)
})

it.each([
  'missing_initial',
  'missing_delivery',
  'bad_collector',
  'unknown_version',
  'extra_field',
  'bad_exit',
  'bad_ref',
  'old_protocol',
  'posthoc',
  'extra_receipt_field',
  'unpaired_delivery',
  'unverified_refs',
])('rejects malformed branch evidence: %s', (change) => {
  const value = structuredClone(branch) as Record<string, unknown>
  if (change === 'missing_initial') delete value.initialVerification
  if (change === 'missing_delivery') value.publicVerification = { ...branch.publicVerification, deliveryRefs: undefined }
  if (change === 'bad_collector') value.collectorVersion = 'host-review-1'
  if (change === 'unknown_version') value.schemaVersion = 4
  if (change === 'extra_field') value.unregisteredProof = true
  if (change === 'bad_exit') value.publicVerification = { ...branch.publicVerification, finalExitCode: 0.5 }
  if (change === 'bad_ref')
    value.publicVerification = {
      ...branch.publicVerification,
      deliveryRefs: [{ checkIndex: 0, startedEvent: 'event-1', completedEvent: 'event-2' }],
    }
  if (change === 'old_protocol') value.verification = { ...branch.verification, protocol: 'p41.1' }
  if (change === 'posthoc') value.verification = { ...branch.verification, posthoc: true }
  if (change === 'extra_receipt_field') value.verification = { ...branch.verification, ignoredEvidence: true }
  if (change === 'unpaired_delivery') value.events = branch.events.filter((event) => event.id !== 'event-4')
  if (change === 'unverified_refs') value.publicVerification = { ...branch.publicVerification, deliveriesVerified: false }
  expect(skillAssessment.safeParse(value).success).toBe(false)
})

it('accepts schema 3 without losing intermediate process evidence', async () => {
  const { extensionAssessment } = await import('./skill-extension.test-fixture')
  const value = extensionAssessment()
  expect(skillAssessment.parse(value)).toEqual(value)
})

it.each([
  'missing_process',
  'missing_midpoint',
  'old_protocol',
  'bad_mapping',
  'bad_cycle',
  'foreign_delivery',
  'foreign_edit',
  'wrong_groups',
  'unknown_field',
])('rejects malformed schema 3 facts: %s', async (change) => {
  const { extensionAssessment } = await import('./skill-extension.test-fixture')
  const value = extensionAssessment()
  const raw = value as unknown as Record<string, unknown>
  if (change === 'missing_process') delete raw.processVerification
  if (change === 'missing_midpoint') delete raw.intermediateVerifications
  if (change === 'old_protocol') raw.verification = { ...value.verification, protocol: 'branch-receipt-1' }
  if (change === 'bad_mapping') value.checkMapping[0].group = 'dream'
  if (change === 'bad_cycle') value.processVerification.cyclesUsed = 1
  if (change === 'foreign_delivery') value.processVerification.checkpoints[1].completedEvent = 'event-3'
  if (change === 'foreign_edit') value.processVerification.checkpoints[1].editEventRefs = ['unknown']
  if (change === 'wrong_groups') value.processVerification.checkpoints[1].groupStatus.scoped = 'failed'
  if (change === 'unknown_field') raw.unregisteredProof = true
  expect(skillAssessment.safeParse(value).success).toBe(false)
})

it('retains schema 3 infrastructure gaps and a single delivered baseline as incomplete facts', async () => {
  const { extensionAssessment } = await import('./skill-extension.test-fixture')
  const baseline = extensionAssessment()
  baseline.processVerification.complete = false
  baseline.processVerification.issues = ['checkpoint_incomplete']
  baseline.processVerification.checkpoints = baseline.processVerification.checkpoints.slice(0, 1)
  baseline.processVerification.cyclesUsed = 0
  baseline.publicVerification.deliveryRefs = baseline.publicVerification.deliveryRefs.slice(0, 1)
  baseline.intermediateVerifications = []
  baseline.outcome = 'incomplete'
  expect(skillAssessment.safeParse(baseline).success).toBe(true)
  const fault = structuredClone(baseline)
  fault.publicVerification.initialExitCode = 7
  fault.publicVerification.finalExitCode = -9
  fault.publicVerification.deliveriesVerified = false
  fault.publicVerification.deliveryRefs = []
  const checkpoint = fault.processVerification.checkpoints[0]
  checkpoint.startedEvent = checkpoint.completedEvent = null
  checkpoint.checks = []
  checkpoint.groupStatus = {}
  expect(skillAssessment.safeParse(fault).success).toBe(true)
})
