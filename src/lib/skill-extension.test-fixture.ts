// Synthetic UI/transport facts only; this fixture is never imported by the app.
import type { SkillExtensionAssessment } from '../../shared/contract'

const hash = (c: string) => c.repeat(64)
export function extensionAssessment(): SkillExtensionAssessment {
  const checkMapping = [
    ...[
      'caller_contract',
      'fake_scope_contract',
      'scope_generalization',
      'caller_with_fake',
      'update_regressions',
      'embedding_failure',
    ].map((ruleCheck) => ({ id: `scoped.${ruleCheck}`, group: 'scoped' as const, ruleCheck })),
    ...['caller_contract', 'fake_contract', 'caller_with_fake', 'other_actions', 'no_unintended_write'].map((ruleCheck) => ({
      id: `dream.${ruleCheck}`,
      group: 'dream' as const,
      ruleCheck,
    })),
  ]
  const events = [
    { id: 'check-1', kind: 'tool_started', value: { display: { tool_name: 'mcp__memos_run__run_visible_checks' } } },
    { id: 'event-1', kind: 'tool_completed', value: { display: {} } },
    { id: 'edit-1', kind: 'tool_started', value: { display: { tool_name: 'Edit' } } },
    { id: 'edit-1-end', kind: 'tool_completed', value: { display: {} } },
    { id: 'check-2', kind: 'tool_started', value: { display: { tool_name: 'mcp__memos_run__run_visible_checks' } } },
    { id: 'event-2', kind: 'tool_completed', value: { display: {} } },
    { id: 'rule-1', kind: 'rule_declaration', value: { display: { rule_id: 'verify_scoped_reads', phase: 'select' } } },
    { id: 'edit-2', kind: 'tool_started', value: { display: { tool_name: 'Write' } } },
    { id: 'edit-2-end', kind: 'tool_completed', value: { display: {} } },
    { id: 'check-3', kind: 'tool_started', value: { display: { tool_name: 'mcp__memos_run__run_visible_checks' } } },
    { id: 'event-3', kind: 'tool_completed', value: { display: {} } },
  ]
  const checkpoints = [1, 2, 3].map((checkIndex) => ({
    checkIndex,
    snapshot: hash(['8', 'a', 'f'][checkIndex - 1]),
    recordSha256: hash(String(checkIndex)),
    startedEvent: `check-${checkIndex}`,
    completedEvent: `event-${checkIndex}`,
    checks: checkMapping.map((row) => ({
      name: row.id,
      status: ((checkIndex === 1 && row.id === 'scoped.caller_contract') || (checkIndex < 3 && row.id === 'dream.caller_contract')
        ? 'failed'
        : 'passed') as 'failed' | 'passed',
    })),
    groupStatus: {
      scoped: checkIndex === 1 ? ('failed' as const) : ('passed' as const),
      dream: checkIndex < 3 ? ('failed' as const) : ('passed' as const),
    },
    cyclesUsed: checkIndex - 1,
    editEventRefs: checkIndex === 1 ? [] : [`edit-${checkIndex - 1}`],
  }))
  return {
    schemaVersion: 3,
    collectorVersion: 'extension-review-1',
    collectorDigest: hash('1'),
    assessmentId: hash('b'),
    validationId: hash('c'),
    caseId: 'branch-combined-state',
    contentHash: hash('d'),
    exportHash: hash('e'),
    candidateSnapshot: hash('f'),
    reportSha256: hash('1'),
    eventsSha256: hash('2'),
    captureComplete: true,
    loadVerified: true,
    behaviorChanged: true,
    originalStatus: 'passed',
    verification: {
      status: 'passed',
      receiptSha256: hash('3'),
      protocol: 'extension-receipt-1',
      matches: true,
      sameFinalPatch: true,
      posthoc: false,
    },
    initialVerification: {
      status: 'failed',
      receiptSha256: hash('4'),
      protocol: 'extension-receipt-1',
      matches: true,
      candidateSnapshot: hash('8'),
    },
    publicVerification: {
      initialExitCode: 1,
      finalExitCode: 0,
      recordsVerified: true,
      deliveriesVerified: true,
      deliveryRefs: checkpoints.map(({ checkIndex, startedEvent, completedEvent }) => ({ checkIndex, startedEvent, completedEvent })),
    },
    checks: checkpoints[2].checks,
    events,
    branchCandidates: ['verify_scoped_reads#0'],
    coveragePlanHash: hash('5'),
    runId: 'run',
    attemptId: 'attempt',
    caseRevision: '1',
    lineageId: 'combined-shared-state',
    split: 'holdout',
    purpose: 'management_test',
    outcome: 'repair_candidate',
    batchId: hash('6'),
    taskDigest: hash('7'),
    suiteId: 'combined-c1-v1',
    checkMapping,
    processVerification: {
      version: 'extension-process-1',
      complete: true,
      issues: [],
      maxCycles: 2,
      cyclesUsed: 2,
      budgetExceeded: false,
      unverifiedEdits: false,
      checkpoints,
    },
    intermediateVerifications: [
      {
        snapshot: hash('a'),
        checkIndices: [2],
        receiptSha256: hash('9'),
        protocol: 'extension-receipt-1',
        matches: true,
        status: 'failed',
        checks: checkpoints[1].checks,
      },
    ],
  }
}
