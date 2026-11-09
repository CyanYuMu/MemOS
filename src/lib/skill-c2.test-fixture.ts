// Synthetic transport facts; never imported by application code.
import { extensionAssessment } from './skill-extension.test-fixture'

export function c2Assessment() {
  const original = extensionAssessment()
  const checkpoints = structuredClone(original.processVerification.checkpoints.slice(0, 2))
  checkpoints[0].checks = original.checkMapping.map((row) => ({
    name: row.id,
    status: ['scoped.fake_scope_contract', 'scoped.scope_generalization'].includes(row.id) ? 'failed' : 'passed',
  }))
  checkpoints[0].groupStatus = { scoped: 'failed', dream: 'passed' }
  checkpoints[1].checks = original.checkMapping.map((row) => ({ name: row.id, status: 'passed' }))
  checkpoints[1].groupStatus = { scoped: 'passed', dream: 'passed' }
  return {
    ...original,
    schemaVersion: 4 as const,
    collectorVersion: 'c2-review-1' as const,
    caseId: 'branch-scoped-healthy-dream',
    suiteId: 'scoped-healthy-dream-c2-v1',
    branchCandidates: ['confirm_contract#3', 'repair_scoped_reads#0', 'verify_scoped_reads#1', 'verify_dream_update#0'],
    candidateSnapshot: checkpoints[1].snapshot,
    verification: { ...original.verification, protocol: 'c2-receipt-1' as const },
    initialVerification: { ...original.initialVerification, protocol: 'c2-receipt-1' as const, checks: checkpoints[0].checks },
    checks: checkpoints[1].checks,
    events: original.events.slice(0, 7),
    publicVerification: { ...original.publicVerification, deliveryRefs: original.publicVerification.deliveryRefs.slice(0, 2) },
    processVerification: { ...original.processVerification, version: 'c2-process-1' as const, cyclesUsed: 1, checkpoints },
    intermediateVerifications: [],
  }
}
