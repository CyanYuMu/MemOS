import { useEffect, useRef, useState } from 'react'
import type {
  SkillAdjudicationPackage,
  SkillExtensionAssessment,
  SkillBranchClaim,
  SkillGovernanceState,
  SkillJudgment,
  SkillRef,
} from '../../shared/contract'
import { api, ApiError } from '../lib/api'
import { Button } from '../components/ui/button'

const emptyJudgment = (): SkillJudgment => ({
  taskDecision: 'needs_evidence',
  historyDecision: 'retain',
  usageDecision: 'unknown',
  applicability: 'unknown',
  boundary: null,
  violations: 'unknown',
  reason: '',
  eventRefs: [],
  branches: [],
})
const taskNames = { repair_confirmed: '修复成立', stop_confirmed: '合理停止', failed: '真实失败', needs_evidence: '证据不足' }
const usageNames = { correct: '正确使用', misused: '使用不当', not_used: '未使用', unknown: '不可判断' }
const scopeNames = { in_scope: '范围内', out_of_scope: '范围外', unknown: '尚不明确' }
const boundaryNames = { not_applicable: '不适用', needs_evidence: '缺少证据', unresolved: '无法解决' }
const outcomeNames = { repair_candidate: '待确认的修复', failed: '修复未通过', incomplete: '事实尚不完整' }

function Json({ value }: { value: unknown }) {
  return <pre className="skills-json">{JSON.stringify(value, null, 2)}</pre>
}

function Choice({
  label,
  value,
  options,
  disabledOptions = [],
  onChange,
}: {
  label: string
  value: string
  options: Record<string, string>
  disabledOptions?: string[]
  onChange: (value: string) => void
}) {
  return (
    <label className="skills-governance-field">
      {label}
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {Object.entries(options).map(([key, name]) => (
          <option key={key} value={key} disabled={disabledOptions.includes(key)}>
            {name}
          </option>
        ))}
      </select>
    </label>
  )
}

function References({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string[]
  options: { id: string; label: string }[]
  onChange: (value: string[]) => void
}) {
  return (
    <label className="skills-governance-field">
      {label}
      <select multiple value={value} onChange={(event) => onChange(Array.from(event.target.selectedOptions, (option) => option.value))}>
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}

function ProcessEvidence({ assessment }: { assessment: SkillExtensionAssessment }) {
  const process = assessment.processVerification
  return (
    <section aria-label="组合任务的过程证据">
      <h3>组合任务的过程证据</h3>
      <p>
        {assessment.suiteId} · 已完成 {process.cyclesUsed} / {process.maxCycles} 轮 · {process.checkpoints.length} 次公开检查
      </p>
      <p className="skills-muted">
        轮次统计保守的编辑尝试批次：同一批次内多次 Edit/Write 计一轮，失败或无变化的尝试也计入；无编辑的重复检查不增加轮次。
      </p>
      {!process.complete && (
        <p role="alert" className="skills-notice skills-notice--warning">
          过程证据不完整，不能凭最终绿灯确认修复或分支覆盖。缺口：{process.issues.join('、') || '尚待核验'}
        </p>
      )}
      {process.budgetExceeded && (
        <p role="alert" className="skills-notice skills-notice--warning">
          编辑尝试已超出两轮预算。可独立判断补丁行为，不能认定正确使用或计入分支覆盖。
        </p>
      )}
      {process.unverifiedEdits && <p className="skills-notice skills-notice--warning">最后一次检查后仍有未复验的编辑尝试。</p>}
      {process.checkpoints.map((checkpoint) => (
        <div key={checkpoint.checkIndex}>
          <h4>公开检查 #{checkpoint.checkIndex}</h4>
          <p>
            scoped：{checkpoint.groupStatus.scoped ?? '未知'} · Dream：{checkpoint.groupStatus.dream ?? '未知'} · 已完成{' '}
            {checkpoint.cyclesUsed} 轮
          </p>
          <p>
            快照 <code>{checkpoint.snapshot}</code>
          </p>
          <p>
            送达事件：<code>{checkpoint.startedEvent ?? '开始记录缺失'}</code> →{' '}
            <code>{checkpoint.completedEvent ?? '结果送达未证实'}</code>
          </p>
          <p>
            编辑尝试引用：{checkpoint.editEventRefs.length ? checkpoint.editEventRefs.map((ref) => <code key={ref}>{ref} </code>) : '无'}
          </p>
          <details>
            <summary>检查 #{checkpoint.checkIndex} 的逐项结果与凭据</summary>
            <p>
              记录摘要 <code>{checkpoint.recordSha256}</code>
            </p>
            <table className="skills-governance-table">
              <thead>
                <tr>
                  <th>检查引用</th>
                  <th>结果</th>
                </tr>
              </thead>
              <tbody>
                {checkpoint.checks.map((check) => (
                  <tr key={check.name}>
                    <td>
                      <code>
                        check:{checkpoint.checkIndex}:{check.name}
                      </code>
                    </td>
                    <td>{check.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </div>
      ))}
      <h4>中间隐藏复核</h4>
      <p className="skills-muted">
        Agent 退出后复核预登记条件命中的所有快照，不代表 Agent 当时看到了隐藏检查。中间状态、及时规则声明和后续真实行动均须逐项审阅。
      </p>
      {!assessment.intermediateVerifications.length && <p>没有符合转换条件的中间快照。最终修复通过不能补出中间转换分支。</p>}
      {assessment.intermediateVerifications.map((row) => (
        <details key={row.snapshot}>
          <summary>
            公开检查 {row.checkIndices.map((index) => `#${index}`).join('、')} · 隐藏 {row.status} ·{' '}
            {row.matches ? '凭条已核验' : '凭条未证实'}
          </summary>
          <Json value={row} />
        </details>
      ))}
    </section>
  )
}

export function SkillGovernance({
  reference,
  data,
  sequence,
  contentHash,
  exportHash,
  canManage,
  onRefresh,
}: {
  reference: SkillRef
  data: SkillGovernanceState
  sequence: number
  contentHash: string
  exportHash: string | null
  canManage: boolean
  onRefresh: () => void
}) {
  const current = data.assessments.filter((item) => data.currentAssessments[item.validationId] === item.assessmentId)
  const [selected, setSelected] = useState(current[0]?.assessmentId ?? '')
  const [packet, setPacket] = useState<SkillAdjudicationPackage | null>(null)
  const [judgment, setJudgment] = useState(emptyJudgment)
  const [confirmed, setConfirmed] = useState(false)
  const [supersedes, setSupersedes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const assessment = packet?.assessment ?? current.find((item) => item.assessmentId === selected)
  const branchAssessment = assessment?.schemaVersion === 2 || assessment?.schemaVersion === 3 ? assessment : null
  const extensionAssessment = assessment?.schemaVersion === 3 ? assessment : null
  const processAllowsCoverage =
    !extensionAssessment ||
    (extensionAssessment.processVerification.complete &&
      !extensionAssessment.processVerification.budgetExceeded &&
      !extensionAssessment.processVerification.unverifiedEdits)
  const budgetExceeded = extensionAssessment?.processVerification.budgetExceeded === true
  const covered = data.coverage.filter((item) => item.status === 'covered').length
  function fail(cause: unknown) {
    setPacket(null)
    setConfirmed(false)
    setNeedsRefresh(true)
    setError(
      cause instanceof ApiError && cause.status === 409
        ? '状态已变化，请重新读取后准备新的裁决包；不会自动重试。'
        : `${cause instanceof Error ? cause.message : '请求失败'}。请重新读取并确认结果。`,
    )
  }
  async function prepare() {
    if (!canManage || busy || !selected || needsRefresh) return
    setBusy(true)
    setError('')
    setConfirmed(false)
    try {
      const value = await api.skills.prepareAdjudication({ ...reference, assessmentId: selected })
      if (!mounted.current) return
      if (
        value.projectId !== reference.projectId ||
        value.skillId !== reference.skillId ||
        value.revision !== reference.revision ||
        value.sequence !== sequence ||
        value.contentHash !== contentHash ||
        value.exportHash !== exportHash ||
        value.assessmentId !== selected ||
        value.assessment.assessmentId !== selected
      )
        throw new Error('裁决包与当前版本不匹配')
      setPacket(value)
    } catch (cause) {
      if (mounted.current) fail(cause)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  async function submit() {
    if (!packet || !canSubmit) return
    setBusy(true)
    setError('')
    try {
      await api.skills.adjudicate({
        ...reference,
        packageId: packet.packageId,
        packageHash: packet.packageHash,
        expectedSequence: packet.sequence,
        operationId: crypto.randomUUID(),
        reason: judgment.reason,
        judgment,
        supersedes: supersedes || null,
      })
      if (!mounted.current) return
      setPacket(null)
      setConfirmed(false)
      onRefresh()
    } catch (cause) {
      if (mounted.current) fail(cause)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  function setBranch(branchId: string, field: keyof Omit<SkillBranchClaim, 'branchId'>, value: string | string[]) {
    setJudgment((prior) => ({
      ...prior,
      branches: prior.branches.map((branch) => (branch.branchId === branchId ? { ...branch, [field]: value } : branch)),
    }))
  }
  const proven =
    assessment?.captureComplete &&
    assessment.loadVerified &&
    assessment.verification.matches &&
    assessment.verification.sameFinalPatch &&
    (!branchAssessment ||
      (branchAssessment.initialVerification.matches &&
        branchAssessment.publicVerification.recordsVerified &&
        branchAssessment.publicVerification.deliveriesVerified &&
        branchAssessment.publicVerification.deliveryRefs.length >= 2)) &&
    (!extensionAssessment || (extensionAssessment.processVerification.complete && !extensionAssessment.processVerification.unverifiedEdits))
  const repairSupported =
    proven &&
    assessment?.verification.status === 'passed' &&
    assessment.behaviorChanged &&
    (!branchAssessment ||
      (branchAssessment.outcome === 'repair_candidate' &&
        branchAssessment.initialVerification.status === 'failed' &&
        branchAssessment.publicVerification.initialExitCode === 1 &&
        branchAssessment.publicVerification.finalExitCode === 0))
  const taskSupported =
    judgment.taskDecision === 'needs_evidence' ||
    judgment.taskDecision === 'failed' ||
    (proven &&
      judgment.violations === 'none' &&
      (judgment.taskDecision === 'repair_confirmed'
        ? repairSupported && judgment.boundary === null
        : !branchAssessment &&
          assessment?.behaviorChanged === false &&
          ['passed', 'needs_review'].includes(assessment.verification.status) &&
          judgment.boundary !== null))
  const branchComplete = judgment.branches.every((branch) => {
    if (
      !processAllowsCoverage ||
      !branch.reason.trim() ||
      !branch.conditionRefs.length ||
      !branch.actionRefs.length ||
      !branch.checkRefs.length
    )
      return false
    if (!branchAssessment) return true
    const events = branchAssessment.events
    const refs = [...branch.conditionRefs, ...branch.actionRefs]
    const ruleId = branch.branchId.split('#')[0]
    // A declaration records selected guidance; a tool observation and an actual
    // action are still required separately before a branch can be submitted.
    return (
      judgment.taskDecision === 'repair_confirmed' &&
      judgment.violations === 'none' &&
      judgment.usageDecision === 'correct' &&
      judgment.applicability === 'in_scope' &&
      events.some((event) => branch.conditionRefs.includes(event.id) && event.kind === 'tool_completed') &&
      events.some((event) => branch.actionRefs.includes(event.id) && ['tool_started', 'assistant_text'].includes(event.kind)) &&
      events.some((event) => {
        const display = event.value.display
        return (
          refs.includes(event.id) &&
          event.kind === 'rule_declaration' &&
          typeof display === 'object' &&
          display !== null &&
          'rule_id' in display &&
          display.rule_id === ruleId
        )
      })
    )
  })
  const canSubmit =
    canManage &&
    !busy &&
    !!packet &&
    confirmed &&
    !!judgment.reason.trim() &&
    taskSupported &&
    (!branchAssessment || judgment.historyDecision === 'retain') &&
    branchComplete &&
    (!budgetExceeded || judgment.usageDecision !== 'correct') &&
    (judgment.taskDecision === 'needs_evidence' || judgment.eventRefs.length > 0)
  const previous = data.adjudications.filter(
    (item) => item.validationId === assessment?.validationId && packet?.activeAdjudicationIds.includes(item.adjudicationId),
  )
  const eventOptions = (assessment?.events ?? []).map((event) => ({ id: event.id, label: `${event.kind} · ${event.id}` }))
  return (
    <section className="skills-panel skills-governance">
      <div className="skills-row">
        <h2>执行证据与人工裁决</h2>
        <strong>
          已覆盖 {covered} / {data.coverage.length} 个分支
        </strong>
      </div>
      <p className="skills-muted">任务结果、规则使用和分支覆盖分别判断。裁决追加到历史记录；提交不会自动发布 Skill。</p>
      <details>
        <summary>查看分支覆盖矩阵</summary>
        <table className="skills-governance-table">
          <thead>
            <tr>
              <th>分支</th>
              <th>状态</th>
              <th>裁决依据</th>
            </tr>
          </thead>
          <tbody>
            {data.coverage.map((row) => (
              <tr key={row.branchId}>
                <td>
                  <code>{row.branchId}</code>
                </td>
                <td>{row.status === 'covered' ? '已有证据' : '待补证据'}</td>
                <td>{row.adjudicationIds.length ? row.adjudicationIds.map((id) => <code key={id}>{id}</code>) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      {!current.length ? (
        <p>尚无宿主核验的待审材料。原运行记录保留，不能仅凭通过标签判断覆盖。</p>
      ) : (
        <>
          <label className="skills-governance-field">
            待审任务
            <select
              value={selected}
              disabled={busy}
              onChange={(event) => {
                setSelected(event.target.value)
                setPacket(null)
                setJudgment(emptyJudgment())
                setConfirmed(false)
                setSupersedes('')
                setError('')
              }}
            >
              {current.map((item) => (
                <option key={item.assessmentId} value={item.assessmentId}>
                  {item.caseId} · {item.assessmentId.slice(0, 10)}
                </option>
              ))}
            </select>
          </label>
          {assessment && (
            <>
              <div className="skills-status-grid">
                <div>
                  <span>{branchAssessment ? '初始 / 最终隐藏验收' : '原验收 / 当前复验'}</span>
                  <strong>
                    {branchAssessment ? branchAssessment.initialVerification.status : assessment.originalStatus} /{' '}
                    {assessment.verification.status}
                  </strong>
                  <small>
                    {branchAssessment
                      ? '分别绑定初始快照与最终补丁'
                      : assessment.verification.posthoc
                        ? '同一补丁事后复验，不增加样本'
                        : '原协议核验'}
                  </small>
                </div>
                <div>
                  <span>事实绑定</span>
                  <strong>{proven ? '采集、加载与补丁已核验' : '仍有未证实项'}</strong>
                  <small>行为文件变化：{assessment.behaviorChanged ? '有' : '无'}</small>
                </div>
                <div>
                  <span>可提交覆盖的分支</span>
                  <strong>{assessment.branchCandidates.length} 个</strong>
                  <small>{branchAssessment ? '候选分支不代表已覆盖，仍需人工判断' : '仍需事件及判据支持'}</small>
                </div>
                {branchAssessment && (
                  <>
                    <div>
                      <span>公开检查与实际送达</span>
                      <strong>
                        公开检查：{branchAssessment.publicVerification.initialExitCode ?? '未知'} →{' '}
                        {branchAssessment.publicVerification.finalExitCode ?? '未知'}
                      </strong>
                      <small>
                        {branchAssessment.publicVerification.recordsVerified ? '执行工件已核验' : '执行工件待核验'}；
                        {branchAssessment.publicVerification.deliveriesVerified ? '工具返回已送达' : '工具送达未证实'}
                      </small>
                    </div>
                    <div>
                      <span>本次任务结果</span>
                      <strong>{outcomeNames[branchAssessment.outcome]}</strong>
                      <small>
                        批次 {branchAssessment.batchId.slice(0, 12)} · {branchAssessment.publicVerification.deliveryRefs.length} 次送达记录
                      </small>
                    </div>
                  </>
                )}
              </div>
              {extensionAssessment && <ProcessEvidence assessment={extensionAssessment} />}
              <details>
                <summary>固定身份与验收检查</summary>
                <Json
                  value={{
                    validationId: assessment.validationId,
                    assessmentId: assessment.assessmentId,
                    candidateSnapshot: assessment.candidateSnapshot,
                    exportHash: assessment.exportHash,
                    reportSha256: assessment.reportSha256,
                    verification: assessment.verification,
                    ...(branchAssessment
                      ? {
                          initialVerification: branchAssessment.initialVerification,
                          publicVerification: branchAssessment.publicVerification,
                          outcome: branchAssessment.outcome,
                          batchId: branchAssessment.batchId,
                          taskDigest: branchAssessment.taskDigest,
                        }
                      : {}),
                    ...(extensionAssessment
                      ? { suiteId: extensionAssessment.suiteId, checkMapping: extensionAssessment.checkMapping }
                      : {}),
                    checks: assessment.checks,
                  }}
                />
              </details>
            </>
          )}
          <div className="skills-actions">
            <Button disabled={!canManage || busy || needsRefresh} onClick={prepare}>
              准备人工裁决包
            </Button>
            {needsRefresh && <Button onClick={onRefresh}>重新读取裁决材料</Button>}
          </div>
          {!canManage && <p className="skills-muted">只有项目管理者可以提交人工判断。</p>}
        </>
      )}
      {error && <p role="alert">{error}</p>}
      {packet && assessment && (
        <div className="skills-governance-form">
          <h3>固定裁决包</h3>
          <code className="skills-governance-id">{packet.packageId}</code>
          <p>
            序号 {packet.sequence} · 审阅人 {packet.actor}
          </p>
          <div className="skills-text-grid">
            <Choice
              label="任务判断"
              value={judgment.taskDecision}
              options={taskNames}
              disabledOptions={branchAssessment ? ['stop_confirmed', ...(!repairSupported ? ['repair_confirmed'] : [])] : []}
              onChange={(value) => setJudgment((prior) => ({ ...prior, taskDecision: value as SkillJudgment['taskDecision'] }))}
            />
            <Choice
              label="历史异常"
              value={judgment.historyDecision}
              options={{ retain: '保留原判断', infra_explained: '验收器异常已有解释' }}
              disabledOptions={branchAssessment ? ['infra_explained'] : []}
              onChange={(value) => setJudgment((prior) => ({ ...prior, historyDecision: value as SkillJudgment['historyDecision'] }))}
            />
            <Choice
              label="规则使用"
              value={judgment.usageDecision}
              options={usageNames}
              disabledOptions={budgetExceeded ? ['correct'] : []}
              onChange={(value) => setJudgment((prior) => ({ ...prior, usageDecision: value as SkillJudgment['usageDecision'] }))}
            />
            <Choice
              label="适用范围判断"
              value={judgment.applicability}
              options={scopeNames}
              onChange={(value) => setJudgment((prior) => ({ ...prior, applicability: value as SkillJudgment['applicability'] }))}
            />
            <Choice
              label="退出类型"
              value={judgment.boundary ?? ''}
              options={{ '': '未选择 / 修复成功', ...boundaryNames }}
              onChange={(value) => setJudgment((prior) => ({ ...prior, boundary: value ? (value as SkillJudgment['boundary']) : null }))}
            />
            <Choice
              label="违规判断"
              value={judgment.violations}
              options={{ unknown: '尚不明确', none: '未发现严重违规', serious: '存在严重违规' }}
              onChange={(value) => setJudgment((prior) => ({ ...prior, violations: value as SkillJudgment['violations'] }))}
            />
          </div>
          <p className="skills-muted">
            {branchAssessment ? '当前任务协议只支持修复判断，不能确认健康或合理停止。' : '合理停止不会计为修复成功。'}
            以下是公开过程记录，不含私有推理；选择支撑此次判断的事件。
          </p>
          <div className="skills-governance-events">
            {assessment.events.map((event) => (
              <div key={event.id}>
                <label>
                  <input
                    type="checkbox"
                    aria-label={`引用事件 ${event.id}`}
                    checked={judgment.eventRefs.includes(event.id)}
                    onChange={(change) =>
                      setJudgment((prior) => ({
                        ...prior,
                        eventRefs: change.target.checked ? [...prior.eventRefs, event.id] : prior.eventRefs.filter((id) => id !== event.id),
                      }))
                    }
                  />
                  {event.kind} · <code>{event.id}</code>
                </label>
                <details>
                  <summary>查看事件内容</summary>
                  <Json value={event.value} />
                </details>
              </div>
            ))}
          </div>
          <h3>逐分支证据</h3>
          {branchAssessment && (
            <p className="skills-muted">
              每个分支须在条件或行动证据中引用对应规则的声明，并另选工具观察及实际行动。规则声明本身不能证明条件成立或已经完成行动。
            </p>
          )}
          {!assessment.branchCandidates.length && <p>此任务尚无已登记的分支验证计划。可以审阅任务和异常，不能增加分支覆盖。</p>}
          {assessment.branchCandidates.map((id) => {
            const branch = judgment.branches.find((item) => item.branchId === id)
            return (
              <div key={id}>
                <label>
                  <input
                    type="checkbox"
                    checked={!!branch}
                    disabled={!processAllowsCoverage}
                    onChange={(event) =>
                      setJudgment((prior) => ({
                        ...prior,
                        branches: event.target.checked
                          ? [...prior.branches, { branchId: id, conditionRefs: [], actionRefs: [], checkRefs: [], reason: '' }]
                          : prior.branches.filter((item) => item.branchId !== id),
                      }))
                    }
                  />
                  计入分支 {id}
                </label>
                {branch && (
                  <div className="skills-text-grid">
                    <References
                      label={`${id} 条件证据`}
                      value={branch.conditionRefs}
                      options={eventOptions}
                      onChange={(value) => setBranch(id, 'conditionRefs', value)}
                    />
                    <References
                      label={`${id} 行动证据`}
                      value={branch.actionRefs}
                      options={eventOptions}
                      onChange={(value) => setBranch(id, 'actionRefs', value)}
                    />
                    <References
                      label={`${id} 验收证据`}
                      value={branch.checkRefs}
                      options={
                        extensionAssessment
                          ? extensionAssessment.processVerification.checkpoints.flatMap((checkpoint) =>
                              checkpoint.checks.map((check) => ({
                                id: `check:${checkpoint.checkIndex}:${check.name}`,
                                label: `公开检查 #${checkpoint.checkIndex} · ${check.name} · ${check.status}`,
                              })),
                            )
                          : assessment.checks.map((check) => ({ id: check.name, label: `${check.name} · ${check.status}` }))
                      }
                      onChange={(value) => setBranch(id, 'checkRefs', value)}
                    />
                    <label className="skills-governance-field">
                      {id} 说明
                      <textarea maxLength={2000} value={branch.reason} onChange={(event) => setBranch(id, 'reason', event.target.value)} />
                    </label>
                  </div>
                )}
              </div>
            )
          })}
          {previous.length > 0 && (
            <Choice
              label="明确纠正已有裁决"
              value={supersedes}
              options={{
                '': '请选择要纠正的记录',
                ...Object.fromEntries(previous.map((item) => [item.adjudicationId, `${item.actor} · ${item.adjudicationId}`])),
              }}
              onChange={setSupersedes}
            />
          )}
          <label className="skills-governance-field">
            裁决理由
            <textarea
              value={judgment.reason}
              maxLength={2000}
              onChange={(event) => setJudgment((prior) => ({ ...prior, reason: event.target.value }))}
            />
          </label>
          {!taskSupported && (
            <p className="skills-notice skills-notice--warning">当前凭条、行为变化或选择的退出类型不能支持该结论，请补齐证据或保留待审。</p>
          )}
          <label>
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            我已审阅证据，确认提交我的判断
          </label>
          <div className="skills-actions">
            <Button disabled={!canSubmit || (previous.length > 0 && !supersedes)} onClick={submit}>
              提交我的裁决
            </Button>
          </div>
        </div>
      )}
      <details>
        <summary>已有裁决（{data.adjudications.length}）</summary>
        <Json value={data.adjudications} />
      </details>
    </section>
  )
}
