import { z } from 'zod'
import type { SkillLifecycle, SkillLifecyclePackage } from '../../shared/contract'

const hash = z.string().regex(/^[a-f0-9]{64}$/)
const hashes = z
  .array(hash)
  .max(200)
  .refine((values) => new Set(values).size === values.length, '摘要引用不得重复')
const kind = z.enum(['memory', 'tool'])
const status = z.enum(['healthy', 'changed', 'unavailable', 'invalid', 'restored'])
const skillId = z.string().regex(/^[a-z0-9][a-z0-9-]{0,79}$/)
const sequence = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER)
export const skillDependencyReleases = z.array(z.object({ skillId, revision: hash, releaseId: hash }).strict())
export const skillLifecycle: z.ZodType<SkillLifecycle> = z
  .object({
    schemaVersion: z.literal(1),
    enabled: z.boolean(),
    watermark: hash,
    subjects: z.array(
      z
        .object({ subjectId: hash, kind, expectedDigest: hash, observedDigest: hash.nullable(), status, observationId: hash.nullable() })
        .strict(),
    ),
    roots: z.array(
      z
        .object({
          rootId: hash,
          originSkillId: skillId,
          originRevision: hash,
          subjectId: hash,
          kind,
          status: z.enum(['active', 'restored_pending']),
          latestObservationId: hash,
          paths: z.array(z.array(z.string().min(1)).min(1)),
          reason: z.string(),
        })
        .strict(),
    ),
    observations: z.array(
      z
        .object({
          observationId: hash,
          subjectId: hash,
          kind,
          expectedDigest: hash,
          observedDigest: hash.nullable(),
          status,
          previousObservationId: hash.nullable(),
          detectorVersion: z.string(),
          reason: z.string(),
          observedAt: z.string(),
        })
        .strict(),
    ),
    decisions: z.array(
      z
        .object({ decisionId: hash, action: z.string(), actor: z.string(), reason: z.string(), decidedAt: z.string(), evidenceIds: hashes })
        .strict(),
    ),
    dependencies: z.array(
      z.object({ skillId, revision: hash, releaseId: hash.nullable(), state: z.string(), allowed: z.boolean() }).strict(),
    ),
    gate: z.object({ status: z.enum(['PASS', 'BLOCK']), reasons: z.array(z.string()) }).strict(),
  })
  .strict()
export const skillLifecyclePackage: z.ZodType<SkillLifecyclePackage> = z
  .object({
    kind: z.literal('lifecycle'),
    projectId: z.string(),
    skillId,
    revision: hash,
    sequence,
    contentHash: hash,
    exportHash: hash.nullable(),
    sourceWatermark: hashes,
    lifecycleWatermark: hash,
    activeRootIds: hashes,
    currentChallengeIds: hashes,
    evidenceIds: hashes,
    dependencyReleases: skillDependencyReleases,
    actor: z.string(),
    preparedAt: z.string(),
    packageId: hash,
    packageHash: hash,
  })
  .strict()
export const skillImpactScan = z.object({ sequence, changed: z.boolean(), lifecycle: skillLifecycle }).strict()
export const skillLifecycleMutation = z.object({ state: z.string(), sequence, lifecycle: skillLifecycle }).strict()
