import { z } from 'zod'
import type { SkillAssessment, SkillAdjudicationPackage, SkillGovernanceState } from '../../shared/contract'
import { validateReleaseAssessment } from './skill-release.validation'

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const record = z.record(z.string(), z.unknown())
const status = z.enum(['passed', 'failed', 'error', 'needs_review'])
const assessmentFields = z
  .object({
    assessmentId: hash,
    validationId: hash,
    caseId: z.string(),
    contentHash: hash,
    exportHash: hash,
    candidateSnapshot: hash,
    reportSha256: hash,
    eventsSha256: hash,
    captureComplete: z.boolean(),
    loadVerified: z.boolean(),
    behaviorChanged: z.boolean(),
    originalStatus: status,
    verification: z.object({
      status,
      receiptSha256: hash,
      protocol: z.string(),
      matches: z.boolean(),
      sameFinalPatch: z.boolean(),
      posthoc: z.boolean(),
    }),
    checks: z.array(z.object({ name: z.string(), status: z.string() })),
    events: z.array(z.object({ id: z.string(), kind: z.string(), value: record })),
    branchCandidates: z.array(z.string()),
    coveragePlanHash: hash,
  })
  .passthrough()

const legacyAssessment = assessmentFields.extend({ schemaVersion: z.literal(1).optional() })
const branchAssessmentFields = assessmentFields
  .extend({
    schemaVersion: z.literal(2),
    collectorVersion: z.literal('branch-review-1'),
    collectorDigest: hash,
    runId: z.string().min(1),
    attemptId: z.string().min(1),
    caseRevision: z.string().min(1),
    lineageId: z.string().min(1),
    split: z.literal('holdout'),
    purpose: z.enum(['actual', 'management_test']),
    verification: z
      .object({
        status: z.enum(['passed', 'failed', 'error']),
        receiptSha256: hash,
        protocol: z.literal('branch-receipt-1'),
        matches: z.boolean(),
        sameFinalPatch: z.boolean(),
        posthoc: z.literal(false),
      })
      .strict(),
    checks: z.array(z.object({ name: z.string().min(1), status: z.enum(['passed', 'failed', 'error']) }).strict()),
    events: z.array(
      z
        .object({
          id: z.string().min(1),
          kind: z.enum(['tool_started', 'tool_completed', 'assistant_text', 'rule_declaration']),
          value: record,
        })
        .strict(),
    ),
    initialVerification: z
      .object({
        status: z.enum(['passed', 'failed', 'error']),
        receiptSha256: hash,
        protocol: z.literal('branch-receipt-1'),
        matches: z.boolean(),
        candidateSnapshot: hash,
      })
      .strict(),
    publicVerification: z
      .object({
        initialExitCode: z.number().int().nullable(),
        finalExitCode: z.number().int().nullable(),
        recordsVerified: z.boolean(),
        deliveriesVerified: z.boolean(),
        deliveryRefs: z.array(
          z
            .object({
              checkIndex: z.number().int().positive(),
              startedEvent: z.string().min(1),
              completedEvent: z.string().min(1),
            })
            .strict(),
        ),
      })
      .strict(),
    outcome: z.enum(['repair_candidate', 'failed', 'incomplete']),
    batchId: hash,
    taskDigest: hash,
  })
  .strict()

const branchAssessment = branchAssessmentFields.superRefine((assessment, context) => {
  const { deliveriesVerified, deliveryRefs } = assessment.publicVerification
  const events = new Map(assessment.events.map((event) => [event.id, event.kind]))
  const valid = deliveriesVerified
    ? deliveryRefs.length >= 2 &&
      deliveryRefs.every(
        (ref, index) =>
          ref.checkIndex === index + 1 &&
          events.get(ref.startedEvent) === 'tool_started' &&
          events.get(ref.completedEvent) === 'tool_completed',
      )
    : deliveryRefs.length === 0
  if (!valid)
    context.addIssue({
      code: 'custom',
      path: ['publicVerification', 'deliveryRefs'],
      message: '公开检查的送达引用不完整或未对应实际事件',
    })
})

const checkStatus = z.enum(['passed', 'failed', 'error'])
const checkRows = z.array(z.object({ name: z.string().min(1), status: checkStatus }).strict()).max(100)
const extensionReceipt = z
  .object({
    status: checkStatus,
    receiptSha256: hash,
    protocol: z.literal('extension-receipt-1'),
    matches: z.boolean(),
    sameFinalPatch: z.boolean(),
    posthoc: z.literal(false),
  })
  .strict()
const extensionAssessmentFields = branchAssessmentFields
  .extend({
    schemaVersion: z.literal(3),
    collectorVersion: z.literal('extension-review-1'),
    verification: extensionReceipt,
    initialVerification: z
      .object({
        status: checkStatus,
        receiptSha256: hash,
        protocol: z.literal('extension-receipt-1'),
        matches: z.boolean(),
        candidateSnapshot: hash,
      })
      .strict(),
    suiteId: z.string().min(1),
    checkMapping: z
      .array(z.object({ id: z.string().min(1), group: z.enum(['scoped', 'dream']), ruleCheck: z.string().regex(/^[a-z_]+$/) }).strict())
      .min(1)
      .max(100),
    processVerification: z
      .object({
        version: z.literal('extension-process-1'),
        complete: z.boolean(),
        issues: z.array(z.string().min(1)).max(100),
        maxCycles: z.literal(2),
        cyclesUsed: z.number().int().min(0).max(100),
        budgetExceeded: z.boolean(),
        unverifiedEdits: z.boolean(),
        checkpoints: z
          .array(
            z
              .object({
                checkIndex: z.number().int().positive(),
                snapshot: hash,
                recordSha256: hash,
                startedEvent: z.string().min(1).nullable(),
                completedEvent: z.string().min(1).nullable(),
                checks: checkRows,
                groupStatus: z.object({ scoped: checkStatus.optional(), dream: checkStatus.optional() }).strict(),
                cyclesUsed: z.number().int().min(0).max(100),
                editEventRefs: z.array(z.string().min(1)).max(100),
              })
              .strict(),
          )
          .max(100),
      })
      .strict(),
    intermediateVerifications: z
      .array(
        z
          .object({
            snapshot: hash,
            checkIndices: z.array(z.number().int().positive()).min(1).max(100),
            receiptSha256: hash,
            protocol: z.literal('extension-receipt-1'),
            matches: z.boolean(),
            status: checkStatus,
            checks: checkRows,
          })
          .strict(),
      )
      .max(100),
  })
  .strict()
const c2AssessmentFields = extensionAssessmentFields.extend({
  schemaVersion: z.literal(4),
  collectorVersion: z.literal('c2-review-1'),
  verification: extensionReceipt.extend({ protocol: z.literal('c2-receipt-1') }),
  initialVerification: extensionAssessmentFields.shape.initialVerification.extend({
    protocol: z.literal('c2-receipt-1'),
    checks: checkRows,
  }),
  processVerification: extensionAssessmentFields.shape.processVerification.extend({ version: z.literal('c2-process-1') }),
  intermediateVerifications: z.array(z.never()).max(0),
})

const executionUnit = z
  .object({
    unit: z.string().min(1),
    group: z.enum(['none', 'skill']),
    runId: z.string().min(1),
    attemptId: z.string().min(1),
    reportSha256: hash,
    eventsSha256: hash,
    initialReceiptSha256: hash,
    receiptSha256: hash,
    status: z.enum(['passed', 'failed', 'incomplete']),
    taskAccepted: z.boolean(),
    captureComplete: z.boolean(),
    loading: z.enum(['not_requested', 'verified', 'unknown']),
    processComplete: z.boolean(),
    maxCycles: z.number().int().nonnegative(),
    cyclesUsed: z.number().int().nonnegative(),
    budgetExceeded: z.boolean(),
    unverifiedEdits: z.boolean(),
    policyMatches: z.boolean(),
    qualified: z.boolean(),
    issues: z.array(z.string().min(1)),
  })
  .strict()
const c2V2AssessmentFields = c2AssessmentFields.extend({
  schemaVersion: z.literal(5),
  collectorVersion: z.literal('c2-review-2'),
  executionQualification: z
    .object({
      protocol: z.literal('c2-plan-1'),
      policyDigest: hash,
      batchProtocol: z.literal('c2-batch-2'),
      batchId: hash,
      manifestSha256: hash,
      calibration: executionUnit.extend({ unit: z.literal('c2-none'), group: z.literal('none') }),
      trial: executionUnit.extend({ unit: z.literal('c2-skill'), group: z.literal('skill') }),
      planQualified: z.boolean(),
      planIssues: z.array(z.string().min(1)),
    })
    .strict(),
})

const releaseAssessmentFields = c2V2AssessmentFields
  .extend({
    schemaVersion: z.literal(6),
    collectorVersion: z.literal('release-review-1'),
    verification: extensionReceipt.extend({ protocol: z.literal('release-receipt-1') }),
    initialVerification: c2AssessmentFields.shape.initialVerification.extend({ protocol: z.literal('release-receipt-1') }),
    publicVerification: branchAssessmentFields.shape.publicVerification.extend({
      fault: z
        .object({
          checkIndex: z.literal(2),
          code: z.literal('registered_check_unavailable'),
          recordSha256: hash,
          startedEvent: z.string().min(1),
          completedEvent: z.string().min(1),
        })
        .strict()
        .nullable(),
    }),
    processVerification: c2AssessmentFields.shape.processVerification.extend({ version: z.literal('release-process-1') }),
    outcome: z.enum(['healthy_candidate', 'repair_candidate', 'stop_candidate', 'unresolved_candidate', 'incomplete', 'failed']),
    taskOutcome: z
      .object({
        kind: z.enum(['healthy', 'repair', 'out_of_scope', 'missing_evidence', 'unresolved', 'tool_failure']),
        eligible: z.boolean(),
        reasons: z.array(z.string().min(1)),
      })
      .strict(),
    executionQualification: c2V2AssessmentFields.shape.executionQualification.extend({
      protocol: z.literal('release-plan-1'),
      batchProtocol: z.literal('release-batch-1'),
      calibration: executionUnit.extend({ unit: z.literal('release-none'), group: z.literal('none') }),
      trial: executionUnit.extend({ group: z.literal('skill') }),
    }),
  })
  .strict()

function validateProcessAssessment(
  assessment: z.infer<typeof extensionAssessmentFields> | z.infer<typeof c2AssessmentFields> | z.infer<typeof c2V2AssessmentFields>,
  context: z.RefinementCtx,
) {
  const invalid = (path: string[], message: string) => context.addIssue({ code: 'custom', path, message })
  const process = assessment.processVerification
  const events = new Map(assessment.events.map((event, index) => [event.id, { ...event, index }]))
  const mapping = assessment.checkMapping
  const names = mapping.map((row) => row.id)
  const publicEvidence = assessment.publicVerification
  const unique = (values: unknown[]) => new Set(values).size === values.length
  const displayTool = (id: string) => {
    const value = events.get(id)?.value.display
    return typeof value === 'object' && value !== null && 'tool_name' in value ? value.tool_name : undefined
  }
  const isEdit = (id: string) => events.get(id)?.kind === 'tool_started' && ['Edit', 'Write'].includes(String(displayTool(id)))
  const checksValid = (checks: { name: string; status: string }[], complete: boolean) => {
    const actual = checks.map((row) => row.name)
    const expected = complete ? names : names.filter((name) => actual.includes(name))
    return unique(actual) && actual.join('|') === expected.join('|')
  }
  if (!unique(names) || mapping.some((row) => row.id !== `${row.group}.${row.ruleCheck}`)) invalid(['checkMapping'], '检查映射与分组不一致')
  if (events.size !== assessment.events.length) invalid(['events'], '事件引用重复')
  if (!checksValid(assessment.checks, assessment.verification.status !== 'error')) invalid(['checks'], '最终检查不完整或顺序不一致')
  if (
    (assessment.schemaVersion === 4 || assessment.schemaVersion === 5) &&
    !checksValid(assessment.initialVerification.checks, assessment.initialVerification.status !== 'error')
  )
    invalid(['initialVerification', 'checks'], '初始隐藏检查不完整或顺序不一致')
  if (assessment.schemaVersion === 5) {
    const plan = assessment.executionQualification
    const trial = plan.trial
    for (const unit of [plan.calibration, trial]) {
      const qualified =
        unit.status === 'passed' &&
        unit.taskAccepted &&
        unit.captureComplete &&
        unit.processComplete &&
        unit.loading === (unit.group === 'none' ? 'not_requested' : 'verified') &&
        unit.maxCycles === 2 &&
        unit.cyclesUsed <= 2 &&
        !unit.budgetExceeded &&
        !unit.unverifiedEdits &&
        unit.policyMatches
      if (qualified !== unit.qualified || unit.qualified !== (unit.issues.length === 0))
        invalid(['executionQualification'], '会话事实与计划资格不一致')
    }
    const issues = [...plan.calibration.issues.map((issue) => `calibration:${issue}`), ...trial.issues.map((issue) => `trial:${issue}`)]
    if (plan.planQualified !== (plan.calibration.qualified && trial.qualified) || plan.planIssues.join('|') !== issues.join('|'))
      invalid(['executionQualification'], '批次计划结论与会话资格不一致')
    if (
      plan.batchId !== assessment.batchId ||
      trial.runId !== assessment.runId ||
      trial.attemptId !== assessment.attemptId ||
      trial.reportSha256 !== assessment.reportSha256 ||
      trial.eventsSha256 !== assessment.eventsSha256 ||
      trial.initialReceiptSha256 !== assessment.initialVerification.receiptSha256 ||
      trial.receiptSha256 !== assessment.verification.receiptSha256 ||
      trial.captureComplete !== assessment.captureComplete ||
      trial.processComplete !== process.complete ||
      trial.maxCycles !== process.maxCycles ||
      trial.cyclesUsed !== process.cyclesUsed ||
      trial.budgetExceeded !== process.budgetExceeded ||
      trial.unverifiedEdits !== process.unverifiedEdits
    )
      invalid(['executionQualification', 'trial'], '计划资格没有绑定当前执行事实')
  }
  const deliveries = publicEvidence.deliveryRefs
  if (
    publicEvidence.deliveriesVerified
      ? !deliveries.length ||
        deliveries.some(
          (row, index) =>
            row.checkIndex !== index + 1 ||
            events.get(row.startedEvent)?.kind !== 'tool_started' ||
            events.get(row.completedEvent)?.kind !== 'tool_completed' ||
            events.get(row.startedEvent)!.index >= events.get(row.completedEvent)!.index,
        )
      : deliveries.length > 0
  )
    invalid(['publicVerification', 'deliveryRefs'], '公开检查送达引用无效')
  if (process.budgetExceeded !== (process.cyclesUsed > 2 || (process.cyclesUsed >= 2 && process.unverifiedEdits)))
    invalid(['processVerification', 'budgetExceeded'], '轮次与预算状态不一致')
  let cycles = 0
  let previousEnd = -1
  const allEdits: string[] = []
  for (const [index, checkpoint] of process.checkpoints.entries()) {
    let valid = checkpoint.checkIndex === index + 1 && checksValid(checkpoint.checks, process.complete)
    const checks = new Map(checkpoint.checks.map((row) => [row.name, row.status]))
    const groupsValid = (['scoped', 'dream'] as const).every((group) => {
      const statuses = mapping.filter((row) => row.group === group).map((row) => checks.get(row.id) ?? 'error')
      const expected = statuses.includes('error') ? 'error' : statuses.includes('failed') ? 'failed' : 'passed'
      return checkpoint.groupStatus[group] === expected
    })
    const errorProjection =
      !process.complete &&
      process.issues.length > 0 &&
      ((checkpoint.groupStatus.scoped === 'error' && checkpoint.groupStatus.dream === 'error') ||
        (!checkpoint.checks.length && !Object.keys(checkpoint.groupStatus).length))
    valid &&= groupsValid || errorProjection
    const start = checkpoint.startedEvent === null ? undefined : events.get(checkpoint.startedEvent)
    const end = checkpoint.completedEvent === null ? undefined : events.get(checkpoint.completedEvent)
    if (checkpoint.startedEvent !== null)
      valid &&= start?.kind === 'tool_started' && displayTool(checkpoint.startedEvent) === 'mcp__memos_run__run_visible_checks'
    if (checkpoint.completedEvent !== null) valid &&= end?.kind === 'tool_completed'
    if (start && end)
      valid &&=
        start.index < end.index &&
        deliveries.some((row) => row.checkIndex === checkpoint.checkIndex && row.startedEvent === start.id && row.completedEvent === end.id)
    valid &&= unique(checkpoint.editEventRefs) && checkpoint.editEventRefs.every((id) => isEdit(id) && !allEdits.includes(id))
    allEdits.push(...checkpoint.editEventRefs)
    if (process.complete) {
      cycles += Number(checkpoint.editEventRefs.length > 0)
      valid &&= !!start && !!end && previousEnd < start.index && checkpoint.cyclesUsed === cycles
      valid &&= checkpoint.editEventRefs.every((id) => previousEnd < events.get(id)!.index && events.get(id)!.index < (start?.index ?? -1))
      valid &&= checkpoint.checks.every((row) => row.status !== 'error')
      previousEnd = end?.index ?? previousEnd
    }
    if (!valid) invalid(['processVerification', 'checkpoints', String(index)], '检查点状态、轮次或事件引用不一致')
  }
  if (process.complete) {
    const first = process.checkpoints[0]
    const final = process.checkpoints.at(-1)
    const edits = assessment.events.filter((event) => isEdit(event.id)).map((event) => event.id)
    const expectedExit = (checkpoint: typeof first | undefined) => (checkpoint?.checks.every((row) => row.status === 'passed') ? 0 : 1)
    if (publicEvidence.initialExitCode !== expectedExit(first) || publicEvidence.finalExitCode !== expectedExit(final))
      invalid(['publicVerification'], '完整检查与公开退出码不一致')
    if (
      process.issues.length ||
      process.unverifiedEdits ||
      process.checkpoints.length < 2 ||
      !publicEvidence.recordsVerified ||
      !publicEvidence.deliveriesVerified ||
      deliveries.length !== process.checkpoints.length ||
      process.cyclesUsed !== cycles ||
      first?.cyclesUsed !== 0 ||
      first?.editEventRefs.length ||
      first?.snapshot !== assessment.initialVerification.candidateSnapshot ||
      final?.snapshot !== assessment.candidateSnapshot ||
      allEdits.join('|') !== edits.join('|')
    )
      invalid(['processVerification'], '完整过程缺少基线、最终绑定或编辑记录')
  }
  const hidden = assessment.intermediateVerifications
  if (!unique(hidden.map((row) => row.snapshot))) invalid(['intermediateVerifications'], '中间复核重复')
  for (const row of hidden) {
    if (
      !unique(row.checkIndices) ||
      row.checkIndices.some((index) => process.checkpoints[index - 1]?.snapshot !== row.snapshot) ||
      !checksValid(row.checks, row.status !== 'error')
    )
      invalid(['intermediateVerifications'], '中间复核与公开快照不匹配')
  }
}

const extensionAssessment = extensionAssessmentFields.superRefine(validateProcessAssessment)
const c2Assessment = c2AssessmentFields.superRefine(validateProcessAssessment)
const c2V2Assessment = c2V2AssessmentFields.superRefine(validateProcessAssessment)

// A new version must satisfy its own complete shape, never fall back to the
// permissive historical projection and lose initial/delivery evidence.
export const skillAssessment: z.ZodType<SkillAssessment> = z.union([
  legacyAssessment,
  branchAssessment,
  extensionAssessment,
  c2Assessment,
  c2V2Assessment,
  releaseAssessmentFields.superRefine(validateReleaseAssessment),
])

export const skillGovernance: z.ZodType<SkillGovernanceState> = z.object({
  assessments: z.array(skillAssessment),
  currentAssessments: z.record(z.string(), hash),
  adjudications: z.array(
    z
      .object({
        adjudicationId: hash,
        assessmentId: hash,
        validationId: hash,
        supersedes: hash.nullable(),
        actor: z.string(),
        reason: z.string(),
        decidedAt: z.string(),
        contribution: record,
      })
      .passthrough(),
  ),
  coverage: z.array(z.object({ branchId: z.string(), status: z.enum(['covered', 'missing']), adjudicationIds: z.array(hash) })),
  qualification: record,
})

export const skillAdjudicationPackage: z.ZodType<SkillAdjudicationPackage> = z.object({
  kind: z.literal('adjudication'),
  projectId: z.string(),
  skillId: z.string(),
  revision: hash,
  sequence: z.number().int().nonnegative(),
  contentHash: hash,
  exportHash: hash.nullable(),
  sourceWatermark: z.array(z.string()),
  assessmentId: hash,
  assessment: skillAssessment,
  validation: record,
  activeAdjudicationIds: z.array(hash),
  actor: z.string(),
  preparedAt: z.string(),
  packageId: hash,
  packageHash: hash,
})

export const skillAdjudicationResult = z.object({
  adjudicationId: hash,
  sequence: z.number().int().nonnegative(),
  state: z.string(),
  contribution: record,
})
