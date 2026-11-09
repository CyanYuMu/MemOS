import { z } from 'zod'
import type { SkillAssessment, SkillAdjudicationPackage, SkillGovernanceState } from '../../shared/contract'

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
const extensionAssessment = branchAssessmentFields
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
  .superRefine((assessment, context) => {
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
    if (!unique(names) || mapping.some((row) => row.id !== `${row.group}.${row.ruleCheck}`))
      invalid(['checkMapping'], '检查映射与分组不一致')
    if (events.size !== assessment.events.length) invalid(['events'], '事件引用重复')
    if (!checksValid(assessment.checks, assessment.verification.status !== 'error')) invalid(['checks'], '最终检查不完整或顺序不一致')
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
          deliveries.some(
            (row) => row.checkIndex === checkpoint.checkIndex && row.startedEvent === start.id && row.completedEvent === end.id,
          )
      valid &&= unique(checkpoint.editEventRefs) && checkpoint.editEventRefs.every((id) => isEdit(id) && !allEdits.includes(id))
      allEdits.push(...checkpoint.editEventRefs)
      if (process.complete) {
        cycles += Number(checkpoint.editEventRefs.length > 0)
        valid &&= !!start && !!end && previousEnd < start.index && checkpoint.cyclesUsed === cycles
        valid &&= checkpoint.editEventRefs.every(
          (id) => previousEnd < events.get(id)!.index && events.get(id)!.index < (start?.index ?? -1),
        )
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
  })

// A new version must satisfy its own complete shape, never fall back to the
// permissive historical projection and lose initial/delivery evidence.
export const skillAssessment: z.ZodType<SkillAssessment> = z.union([legacyAssessment, branchAssessment, extensionAssessment])

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
