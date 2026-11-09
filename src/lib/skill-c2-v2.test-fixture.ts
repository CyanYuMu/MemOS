import { c2Assessment } from './skill-c2.test-fixture'

export function c2V2Assessment() {
  const original = c2Assessment()
  const trial = {
    unit: 'c2-skill',
    group: 'skill' as const,
    runId: original.runId,
    attemptId: original.attemptId,
    reportSha256: original.reportSha256,
    eventsSha256: original.eventsSha256,
    initialReceiptSha256: original.initialVerification.receiptSha256,
    receiptSha256: original.verification.receiptSha256,
    status: 'passed' as const,
    taskAccepted: true,
    captureComplete: true,
    loading: 'verified' as const,
    processComplete: true,
    maxCycles: 2,
    cyclesUsed: 1,
    budgetExceeded: false,
    unverifiedEdits: false,
    policyMatches: true,
    qualified: true,
    issues: [] as string[],
  }
  return {
    ...original,
    schemaVersion: 5 as const,
    collectorVersion: 'c2-review-2' as const,
    executionQualification: {
      protocol: 'c2-plan-1' as const,
      policyDigest: '9'.repeat(64),
      batchProtocol: 'c2-batch-2' as const,
      batchId: original.batchId,
      manifestSha256: 'a'.repeat(64),
      calibration: {
        ...trial,
        unit: 'c2-none',
        group: 'none' as const,
        runId: 'none-run',
        attemptId: 'none-attempt',
        loading: 'not_requested' as const,
      },
      trial,
      planQualified: true,
      planIssues: [] as string[],
    },
  }
}
