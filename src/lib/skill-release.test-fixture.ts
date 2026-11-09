// Synthetic transport facts only; never imported by application code.
import { c2V2Assessment } from './skill-c2-v2.test-fixture'

export function releaseAssessment(
  kind: 'repair' | 'healthy' | 'missing_evidence' | 'out_of_scope' | 'unresolved' | 'tool_failure' = 'repair',
  keepUnresolvedUnedited = false,
) {
  const original = c2V2Assessment()
  const value = structuredClone(original)
  const noEdit = ['healthy', 'missing_evidence', 'out_of_scope'].includes(kind) || (kind === 'unresolved' && keepUnresolvedUnedited)
  const initiallyHealthy = ['healthy', 'missing_evidence'].includes(kind)
  const finalFailed = ['out_of_scope', 'unresolved'].includes(kind)
  const failedName = kind === 'out_of_scope' ? value.checkMapping[0].id : 'dream.caller_with_fake'
  const initialChecks = value.checkMapping.map((row) => ({
    name: row.id,
    status: !initiallyHealthy && row.id === failedName ? ('failed' as const) : ('passed' as const),
  }))
  const finalChecks = value.checkMapping.map((row) => ({
    name: row.id,
    status: finalFailed && row.id === failedName ? ('failed' as const) : ('passed' as const),
  }))
  const groupStatus = (checks: typeof initialChecks) =>
    ({
      scoped: checks.some((row) => row.name.startsWith('scoped.') && row.status === 'failed') ? 'failed' : 'passed',
      dream: checks.some((row) => row.name.startsWith('dream.') && row.status === 'failed') ? 'failed' : 'passed',
    }) as const
  const checkpoints = value.processVerification.checkpoints.slice(0, noEdit ? 1 : 2)
  checkpoints[0].checks = initialChecks
  checkpoints[0].groupStatus = groupStatus(initialChecks)
  if (!noEdit) {
    checkpoints[1].checks = finalChecks
    checkpoints[1].groupStatus = groupStatus(finalChecks)
  }
  const snapshot = checkpoints.at(-1)!.snapshot
  const fault =
    kind === 'tool_failure'
      ? {
          checkIndex: 2,
          code: 'registered_check_unavailable',
          recordSha256: checkpoints[1].recordSha256,
          startedEvent: checkpoints[1].startedEvent!,
          completedEvent: checkpoints[1].completedEvent!,
        }
      : null
  if (fault) {
    checkpoints[1].checks = []
    checkpoints[1].groupStatus = { scoped: 'error', dream: 'error' }
  }
  const suiteId = `release-${kind}`
  const trial = { ...value.executionQualification.trial, unit: `${suiteId}-skill`, cyclesUsed: noEdit ? 0 : 1 }
  return {
    ...value,
    schemaVersion: 6,
    collectorVersion: 'release-review-1',
    suiteId,
    caseId: `release-${kind}`,
    candidateSnapshot: snapshot,
    behaviorChanged: !noEdit,
    originalStatus: finalFailed ? 'failed' : 'passed',
    verification: { ...value.verification, protocol: 'release-receipt-1', status: finalFailed ? 'failed' : 'passed' },
    initialVerification: {
      ...value.initialVerification,
      protocol: 'release-receipt-1',
      status: initiallyHealthy ? 'passed' : 'failed',
      checks: initialChecks,
    },
    checks: finalChecks,
    events: noEdit ? value.events.slice(0, 2) : value.events,
    publicVerification: {
      ...value.publicVerification,
      initialExitCode: initiallyHealthy ? 0 : 1,
      finalExitCode: fault ? null : finalFailed ? 1 : 0,
      deliveryRefs: value.publicVerification.deliveryRefs.slice(0, noEdit ? 1 : 2),
      fault,
    },
    processVerification: { ...value.processVerification, version: 'release-process-1', cyclesUsed: noEdit ? 0 : 1, checkpoints },
    taskOutcome: { kind, eligible: true, reasons: [] },
    outcome:
      kind === 'repair'
        ? 'repair_candidate'
        : kind === 'healthy'
          ? 'healthy_candidate'
          : kind === 'unresolved' && !noEdit
            ? 'unresolved_candidate'
            : 'stop_candidate',
    executionQualification: {
      ...value.executionQualification,
      protocol: 'release-plan-1',
      batchProtocol: 'release-batch-1',
      calibration: { ...value.executionQualification.calibration, unit: 'release-none' },
      trial,
    },
  }
}
