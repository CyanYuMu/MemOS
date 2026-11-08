import { z } from 'zod'
import type {
  SkillDetail,
  SkillJob,
  SkillReviewPackage,
  SkillRule,
  SkillSummary,
  SkillExperimentSummary,
  SkillExperimentDetail,
} from '../../shared/contract'

const texts = z.array(z.string())
const record = z.record(z.string(), z.unknown())
const readiness = z.object({ status: z.string(), reasons: texts })
export const skillRule: z.ZodType<SkillRule> = z
  .object({
    rule_id: z.string(),
    condition: z.string(),
    observation: z.string(),
    action: z.string(),
    expected_result: z.string(),
    basis: z.string(),
    branches: z.array(z.object({ when: z.string(), target: z.string() })),
    evidence_links: z.array(record),
    validation_status: z.string().optional(),
  })
  .passthrough()
const summary: z.ZodType<SkillSummary> = z.object({
  skillId: z.string(),
  revision: z.string(),
  title: z.string(),
  purpose: z.string(),
  tags: texts,
  state: z.string(),
  sequence: z.number().int().nonnegative(),
  readiness,
  ruleCount: z.number().int().nonnegative(),
  validationCount: z.number().int().nonnegative(),
})
export const skillList = z.object({ items: z.array(summary), total: z.number().int().nonnegative() })
export const skillJob: z.ZodType<SkillJob> = z
  .object({
    jobId: z.string(),
    status: z.string(),
    skillId: z.string().optional(),
    revision: z.string().optional(),
    caseId: z.string().optional(),
    mode: z.string().optional(),
    taskResult: z.object({ status: z.string(), accepted: z.boolean(), receiptMatches: z.boolean() }).nullish(),
    skillUsage: z
      .object({
        loading: z.string(),
        capture: z.string(),
        usageReview: z.string(),
        declaredRuleIds: texts,
        endedAdmission: z.string().optional(),
      })
      .nullish(),
    validationId: z.string().nullish(),
    errorCode: z.string().nullish(),
  })
  .passthrough()
export const skillDetail: z.ZodType<SkillDetail> = z.object({
  projectId: z.string(),
  candidate: z
    .object({
      skill_id: z.string(),
      revision: z.string(),
      title: z.string(),
      goal: z.string(),
      purpose: z.string(),
      tags: texts,
      applicability: texts,
      exclusions: texts,
      prerequisites: texts,
      constraints: texts,
      acceptance: texts,
      limitations: texts,
      entry_rule: z.string(),
      exits: z.record(z.string(), z.string()),
      rules: z.array(skillRule),
      semantic_review: z.string(),
      independent_validation: z.string(),
      content_hash: z.string(),
      source_refs: z.array(record),
      host_limits: texts,
      requires: z.array(z.unknown()),
    })
    .passthrough(),
  management: z
    .object({
      state: z.string(),
      sequence: z.number().int().nonnegative(),
      projectionVersion: z.string().nullable(),
      exportHash: z.string().nullable(),
      readiness,
      challenges: z.array(z.unknown()),
    })
    .passthrough(),
  validations: z.array(record),
  jobs: z.array(skillJob),
  releases: z.array(record),
  caseOptions: z.array(z.object({ caseId: z.string(), title: z.string() })),
})
export const skillEvidence = z.object({ rule: skillRule, facts: z.array(record) })
export const skillReview: z.ZodType<SkillReviewPackage> = z.object({
  projectId: z.string().optional(),
  packageId: z.string(),
  packageHash: z.string(),
  sequence: z.number().int().nonnegative(),
  skillId: z.string(),
  revision: z.string(),
  readiness,
  validationIds: texts,
  sourceWatermark: texts,
  contentHash: z.string(),
  manifestId: z.string().optional(),
  exportHash: z.string().nullish(),
  actor: z.string().optional(),
  preparedAt: z.string().optional(),
  reviewChecklist: z.array(z.object({ ruleId: z.string(), branchCount: z.number().int().nonnegative(), coveredBranches: texts })),
})

const count = z.number().int().nonnegative()
const amount = z.number().nonnegative().nullable()
const group = z.enum(['none', 'raw_memory', 'skill'])
const experimentFields = {
  experimentId: z.string(),
  skillId: z.string(),
  revision: z.string(),
  manifestHash: z.string(),
  status: z.enum(['frozen', 'running', 'completed', 'interrupted']),
  planned: count,
  completed: count,
}
export const skillExperimentSummary: z.ZodType<SkillExperimentSummary> = z.object(experimentFields)
export const skillExperimentList = z.object({ items: z.array(skillExperimentSummary), total: count })
export const skillExperimentDetail: z.ZodType<SkillExperimentDetail> = z.object({
  ...experimentFields,
  groups: z.array(group),
  budget: z.object({ timeoutSeconds: z.number().positive(), maxTurns: count, maxBudgetUsd: z.number().positive(), contextBytes: count }),
  rows: z.array(
    z.object({
      cellId: z.string(),
      caseId: z.string(),
      group,
      status: z.enum(['queued', 'running', 'passed', 'failed', 'unknown', 'infra_invalid']),
      assessmentKind: z.enum(['repair', 'boundary_repair', 'manual_boundary']),
      repairPass: z.union([z.literal(0), z.literal(1)]).nullable(),
      boundaryPass: z.union([z.literal(0), z.literal(1)]).nullable(),
      branchSupported: z.boolean().nullable(),
      criticalViolation: z.boolean().nullable(),
      manualReview: z.literal('pending'),
      taskAccepted: z.boolean(),
      receiptVerified: z.boolean(),
      loading: z.string(),
      capture: z.string(),
      elapsedSeconds: amount,
      reportedCostUsd: amount,
      toolCalls: count.nullable(),
      failureReasons: texts,
    }),
  ),
  summary: z.object({
    groups: z.array(
      z.object({
        group,
        primaryRepair: z.object({ passed: count, denominator: count, planned: count }),
        boundary: z.object({ taskSolved: count, planned: count, pendingReview: count }),
        unknown: count,
        infraInvalid: count,
        elapsedSeconds: amount,
        reportedCostUsd: amount,
        costUnknown: count,
      }),
    ),
    completePairs: count,
  }),
  publication: z.literal('not_approved'),
  limitations: texts,
})
