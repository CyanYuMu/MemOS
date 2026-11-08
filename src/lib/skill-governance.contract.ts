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
const branchAssessment = assessmentFields
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
  .superRefine((assessment, context) => {
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

// A new version must satisfy its own complete shape, never fall back to the
// permissive historical projection and lose initial/delivery evidence.
export const skillAssessment: z.ZodType<SkillAssessment> = z.union([legacyAssessment, branchAssessment])

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
