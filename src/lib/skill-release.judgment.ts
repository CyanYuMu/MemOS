import type { SkillBranchClaim, SkillJudgment, SkillReleaseAssessment, SkillP5Assessment } from '../../shared/contract'

const boundaries = {
  healthy: null,
  repair: null,
  out_of_scope: 'not_applicable',
  missing_evidence: 'needs_evidence',
  unresolved: 'unresolved',
  tool_failure: 'needs_evidence',
} as const
export function releaseJudgmentSupported(assessment: SkillReleaseAssessment | SkillP5Assessment, judgment: SkillJudgment) {
  const expected = {
    healthy_candidate: 'healthy_confirmed',
    repair_candidate: 'repair_confirmed',
    stop_candidate: 'stop_confirmed',
    unresolved_candidate: 'unresolved_confirmed',
    incomplete: null,
    failed: null,
  }[assessment.outcome]
  return (
    assessment.taskOutcome.eligible &&
    judgment.taskDecision === expected &&
    judgment.violations === 'none' &&
    judgment.boundary === boundaries[assessment.taskOutcome.kind] &&
    judgment.applicability === (assessment.taskOutcome.kind === 'out_of_scope' ? 'out_of_scope' : 'in_scope')
  )
}

export function releaseBranchSupported(
  assessment: SkillReleaseAssessment | SkillP5Assessment,
  branch: SkillBranchClaim,
  judgment: SkillJudgment,
) {
  if (
    !releaseJudgmentSupported(assessment, judgment) ||
    judgment.usageDecision !== 'correct' ||
    !assessment.executionQualification.planQualified
  )
    return false
  const events = new Map(assessment.events.map((event, index) => [event.id, { ...event, index }]))
  const condition = branch.conditionRefs.map((id) => events.get(id))
  const actions = branch.actionRefs.map((id) => events.get(id))
  if (condition.some((event) => !event) || actions.some((event) => !event)) return false
  const declarations = actions.filter((event) => {
    const display = event?.value.display
    return (
      event?.kind === 'rule_declaration' &&
      typeof display === 'object' &&
      display !== null &&
      'rule_id' in display &&
      display.rule_id === branch.branchId.split('#')[0]
    )
  })
  const observedBefore = condition.some(
    (event) => event?.kind === 'tool_completed' && declarations.some((declaration) => event.index < declaration!.index),
  )
  const actualAction = actions.some((event) => event && ['tool_started', 'tool_completed', 'assistant_text'].includes(event.kind))
  const checks = new Map<string, string | null>(
    assessment.processVerification.checkpoints.flatMap((checkpoint) =>
      checkpoint.checks.map((check) => [`check:${checkpoint.checkIndex}:${check.name}`, checkpoint.completedEvent] as const),
    ),
  )
  const fault = assessment.publicVerification.fault
  if (fault) checks.set(`fault:${fault.checkIndex}:${fault.code}`, fault.completedEvent)
  const hidden = new Set(assessment.checks.map((check) => `final:${check.name}`))
  return (
    observedBefore &&
    actualAction &&
    branch.checkRefs.every((id) => checks.has(id) || hidden.has(id)) &&
    branch.checkRefs.some((id) => checks.has(id) && [...branch.conditionRefs, ...branch.actionRefs].includes(checks.get(id)!))
  )
}
