import type { z } from 'zod'
import type { SkillReleaseAssessment } from '../../shared/contract'

/** 独立检查 schema 6 的绑定和内部矛盾；旧 schema 的判据保持原样。 */
export function validateReleaseAssessment(value: SkillReleaseAssessment, context: z.RefinementCtx) {
  const invalid = (path: string, message: string) => context.addIssue({ code: 'custom', path: [path], message })
  const process = value.processVerification,
    publicEvidence = value.publicVerification,
    plan = value.executionQualification
  const events = new Map(value.events.map((event, index) => [event.id, { ...event, index }]))
  const names = value.checkMapping.map((row) => row.id)
  const unique = (items: unknown[]) => new Set(items).size === items.length
  const displayTool = (id: string) => {
    const display = events.get(id)?.value.display
    return typeof display === 'object' && display !== null && 'tool_name' in display ? String(display.tool_name) : ''
  }
  const isEdit = (id: string) => events.get(id)?.kind === 'tool_started' && ['Edit', 'Write'].includes(displayTool(id))
  const checkRows = (rows: { name: string; status: string }[], complete: boolean) => {
    const actual = rows.map((row) => row.name)
    return unique(actual) && actual.join('|') === (complete ? names : names.filter((name) => actual.includes(name))).join('|')
  }
  if (!unique(names) || value.checkMapping.some((row) => row.id !== `${row.group}.${row.ruleCheck}`))
    invalid('checkMapping', '检查映射不一致')
  if (events.size !== value.events.length) invalid('events', '事件引用重复')
  if (
    !checkRows(value.checks, value.verification.status !== 'error') ||
    !checkRows(value.initialVerification.checks, value.initialVerification.status !== 'error')
  )
    invalid('checks', '检查列表不完整或顺序改变')
  if (process.budgetExceeded !== (process.cyclesUsed > 2 || (process.cyclesUsed >= 2 && process.unverifiedEdits)))
    invalid('processVerification', '轮次预算不一致')
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
    invalid('publicVerification', '送达引用不成立')
  let cycles = 0,
    previousEnd = -1
  const allEdits: string[] = []
  for (const [index, checkpoint] of process.checkpoints.entries()) {
    const fault = publicEvidence.fault?.checkIndex === checkpoint.checkIndex
    const statuses = new Map(checkpoint.checks.map((row) => [row.name, row.status]))
    const groupsMatch = (['scoped', 'dream'] as const).every((group) => {
      const values = value.checkMapping.filter((row) => row.group === group).map((row) => statuses.get(row.id) ?? 'error')
      return checkpoint.groupStatus[group] === (values.includes('error') ? 'error' : values.includes('failed') ? 'failed' : 'passed')
    })
    const errorProjection =
      !process.complete &&
      process.issues.length > 0 &&
      ((checkpoint.groupStatus.scoped === 'error' && checkpoint.groupStatus.dream === 'error') ||
        (!checkpoint.checks.length && !Object.keys(checkpoint.groupStatus).length))
    const start = checkpoint.startedEvent === null ? undefined : events.get(checkpoint.startedEvent)
    const end = checkpoint.completedEvent === null ? undefined : events.get(checkpoint.completedEvent)
    let valid =
      checkpoint.checkIndex === index + 1 && checkRows(checkpoint.checks, process.complete && !fault) && (groupsMatch || errorProjection)
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
    if (!valid) invalid('processVerification', '公开检查点、事件或轮次绑定不成立')
  }
  const fault = publicEvidence.fault
  if (fault) {
    const checkpoint = process.checkpoints[1]
    if (
      value.taskOutcome.kind !== 'tool_failure' ||
      publicEvidence.finalExitCode !== null ||
      !checkpoint ||
      checkpoint.recordSha256 !== fault.recordSha256 ||
      checkpoint.startedEvent !== fault.startedEvent ||
      checkpoint.completedEvent !== fault.completedEvent ||
      checkpoint.checks.length ||
      checkpoint.groupStatus.scoped !== 'error' ||
      checkpoint.groupStatus.dream !== 'error'
    )
      invalid('publicVerification', '工具故障凭据与实际送达不一致')
  }
  if (process.complete) {
    const first = process.checkpoints[0],
      final = process.checkpoints.at(-1)
    const expectedExit = (row: typeof first | undefined) => (row?.checks.every((check) => check.status === 'passed') ? 0 : 1)
    if (
      process.issues.length ||
      process.unverifiedEdits ||
      !first ||
      !final ||
      !publicEvidence.recordsVerified ||
      !publicEvidence.deliveriesVerified ||
      deliveries.length !== process.checkpoints.length ||
      cycles !== process.cyclesUsed ||
      first.cyclesUsed !== 0 ||
      first.editEventRefs.length ||
      first.snapshot !== value.initialVerification.candidateSnapshot ||
      final.snapshot !== value.candidateSnapshot ||
      allEdits.join('|') !==
        value.events
          .filter((event) => isEdit(event.id))
          .map((event) => event.id)
          .join('|') ||
      publicEvidence.initialExitCode !== expectedExit(first) ||
      (!fault && publicEvidence.finalExitCode !== expectedExit(final))
    )
      invalid('processVerification', '完整过程缺少初始、最终或原生事件绑定')
  }
  for (const unit of [plan.calibration, plan.trial]) {
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
    if (unit.qualified !== qualified || unit.qualified !== (unit.issues.length === 0))
      invalid('executionQualification', '执行单元资格与事实不一致')
  }
  const trial = plan.trial
  const issues = [...plan.calibration.issues.map((issue) => `calibration:${issue}`), ...trial.issues.map((issue) => `trial:${issue}`)]
  if (
    plan.planQualified !== (plan.calibration.qualified && trial.qualified) ||
    plan.planIssues.join('|') !== issues.join('|') ||
    plan.batchId !== value.batchId ||
    trial.unit !== `${value.suiteId}-skill` ||
    trial.runId !== value.runId ||
    trial.attemptId !== value.attemptId ||
    trial.reportSha256 !== value.reportSha256 ||
    trial.eventsSha256 !== value.eventsSha256 ||
    trial.initialReceiptSha256 !== value.initialVerification.receiptSha256 ||
    trial.receiptSha256 !== value.verification.receiptSha256 ||
    trial.taskAccepted !== value.taskOutcome.eligible ||
    (trial.loading === 'verified') !== value.loadVerified ||
    trial.captureComplete !== value.captureComplete ||
    trial.processComplete !== process.complete ||
    trial.maxCycles !== process.maxCycles ||
    trial.cyclesUsed !== process.cyclesUsed ||
    trial.budgetExceeded !== process.budgetExceeded ||
    trial.unverifiedEdits !== process.unverifiedEdits ||
    trial.runId === plan.calibration.runId ||
    trial.attemptId === plan.calibration.attemptId
  )
    invalid('executionQualification', '当前运行或计划身份不匹配')
  if (value.taskOutcome.eligible) {
    const kind = value.taskOutcome.kind
    const initialFailures = new Set(value.initialVerification.checks.filter((row) => row.status === 'failed').map((row) => row.name))
    const finalFailures = value.checks.filter((row) => row.status === 'failed').map((row) => row.name)
    // A tool fault cannot justify a new behavior regression; unresolved must
    // retain the Dream failure being diagnosed, rather than an unrelated one.
    const noNewRegression = finalFailures.every((name) => initialFailures.has(name))
    const dreamFailureRemains = finalFailures.some((name) => ['dream.caller_contract', 'dream.caller_with_fake'].includes(name))
    const bound =
      value.initialVerification.matches &&
      value.verification.matches &&
      value.verification.sameFinalPatch &&
      process.complete &&
      !process.budgetExceeded &&
      !process.unverifiedEdits &&
      value.behaviorChanged === (value.initialVerification.candidateSnapshot !== value.candidateSnapshot) &&
      value.initialVerification.status ===
        (value.initialVerification.checks.some((row) => row.status === 'failed') ? 'failed' : 'passed') &&
      value.verification.status === (value.checks.some((row) => row.status === 'failed') ? 'failed' : 'passed') &&
      !value.taskOutcome.reasons.length
    const valid =
      kind === 'repair'
        ? value.behaviorChanged && value.initialVerification.status === 'failed' && value.verification.status === 'passed'
        : kind === 'healthy'
          ? !value.behaviorChanged && value.initialVerification.status === 'passed' && value.verification.status === 'passed'
          : kind === 'unresolved'
            ? value.verification.status === 'failed' && publicEvidence.finalExitCode === 1 && dreamFailureRemains && noNewRegression
            : kind === 'tool_failure'
              ? !!fault && noNewRegression
              : !value.behaviorChanged
    const outcome =
      !value.captureComplete || !value.loadVerified
        ? 'incomplete'
        : kind === 'repair'
          ? 'repair_candidate'
          : kind === 'healthy'
            ? 'healthy_candidate'
            : kind === 'unresolved' && value.behaviorChanged
              ? 'unresolved_candidate'
              : 'stop_candidate'
    if (!bound || !valid || value.outcome !== outcome) invalid('taskOutcome', '任务结局与原始行为、检查或绑定相互矛盾')
  } else if (!value.taskOutcome.reasons.length) invalid('taskOutcome', '未取得资格必须保留原因')
}
