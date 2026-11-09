import { describe, expect, it } from 'vitest'
import { skillAssessment } from './skill-governance.contract'
import { releaseAssessment } from './skill-release.test-fixture'

describe('P5 explicit protocol', () => {
  it.each(['repair', 'healthy', 'out_of_scope', 'missing_evidence', 'unresolved', 'tool_failure'] as const)(
    'accepts %s with its own identity',
    (kind) => {
      const raw = JSON.parse(JSON.stringify(releaseAssessment(kind)).replaceAll('release-', 'p5-'))
      raw.schemaVersion = 7
      expect(skillAssessment.parse(raw)).toMatchObject({ schemaVersion: 7, collectorVersion: 'p5-review-1' })
      raw.collectorVersion = 'release-review-1'
      expect(skillAssessment.safeParse(raw).success).toBe(false)
    },
  )
})
