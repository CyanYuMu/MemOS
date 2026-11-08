import { z } from 'zod'
import type { SkillAssessment, SkillAdjudicationPackage, SkillGovernanceState } from '../../shared/contract'

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const record = z.record(z.string(), z.unknown())
const status = z.enum(['passed', 'failed', 'error', 'needs_review'])
export const skillAssessment: z.ZodType<SkillAssessment> = z
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
