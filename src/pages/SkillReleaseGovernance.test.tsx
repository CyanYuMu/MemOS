// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { api } from '../lib/api'
import { skillAssessment } from '../lib/skill-governance.contract'
import { releaseAssessment } from '../lib/skill-release.test-fixture'
import { SkillGovernance } from './SkillGovernance'
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
it.each([
  ['healthy', 'healthy_confirmed'],
  ['out_of_scope', 'stop_confirmed'],
  ['unresolved', 'unresolved_confirmed'],
  ['tool_failure', 'stop_confirmed'],
] as const)('schema6 %s 显示独立证据并开放对应判断', async (kind, decision) => {
  const value = skillAssessment.parse(releaseAssessment(kind))
  const reference = { projectId: 'project-a', skillId: 'new-skill', revision: 'a'.repeat(64) }
  vi.spyOn(api.skills, 'prepareAdjudication').mockResolvedValue({
    ...reference,
    kind: 'adjudication',
    sequence: 8,
    contentHash: value.contentHash,
    exportHash: value.exportHash,
    sourceWatermark: [],
    assessmentId: value.assessmentId,
    assessment: value,
    validation: {},
    activeAdjudicationIds: [],
    actor: 'account:reviewer',
    preparedAt: '2026-11-09',
    packageId: 'b'.repeat(64),
    packageHash: 'c'.repeat(64),
  })
  render(
    <SkillGovernance
      reference={reference}
      data={{
        assessments: [value],
        currentAssessments: { [value.validationId]: value.assessmentId },
        adjudications: [],
        coverage: [],
        qualification: {},
      }}
      sequence={8}
      contentHash={value.contentHash}
      exportHash={value.exportHash}
      canManage
      onRefresh={() => {}}
    />,
  )
  expect(screen.getByRole('region', { name: '新版独立任务证据' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '准备人工裁决包' }))
  const choices = (await screen.findByLabelText('任务判断')) as HTMLSelectElement
  expect(choices.querySelector<HTMLOptionElement>(`option[value="${decision}"]`)?.disabled).toBe(false)
  expect((screen.getByRole('button', { name: '提交我的裁决' }) as HTMLButtonElement).disabled).toBe(true)
})
