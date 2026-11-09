import { useEffect, useRef, useState } from 'react'
import type { SkillLifecycle, SkillLifecyclePackage, SkillRef } from '../../shared/contract'
import { api, ApiError } from '../lib/api'
import { Button } from '../components/ui/button'

const statusNames = {
  healthy: '当前一致',
  changed: '内容已变化',
  unavailable: '暂不可读取',
  invalid: '身份或内容无效',
  restored: '观察已恢复',
}
const decisions = {
  restore: '恢复指定根因 / 手工暂停',
  retain: '保留阻断',
  supersede: '关联已发布的新版本',
  addressed: '挑战已有证据回应',
  upheld: '保留挑战',
  superseded: '挑战由新版本替代',
}
type Decision = keyof typeof decisions
function Json({ value }: { value: unknown }) {
  return <pre className="skills-json">{JSON.stringify(value, null, 2)}</pre>
}
function Refs({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: { id: string; name: string }[]
  value: string[]
  onChange: (value: string[]) => void
}) {
  return (
    <label className="skills-governance-field">
      {label}
      <select multiple value={value} onChange={(event) => onChange(Array.from(event.target.selectedOptions, (option) => option.value))}>
        {options.map((row) => (
          <option key={row.id} value={row.id}>
            {row.name}
          </option>
        ))}
      </select>
    </label>
  )
}

export function SkillLifecyclePanel({
  reference,
  data,
  sequence,
  contentHash,
  exportHash,
  validations,
  canManage,
  onRefresh,
}: {
  reference: SkillRef
  data: SkillLifecycle
  sequence: number
  contentHash: string
  exportHash: string | null
  validations: Record<string, unknown>[]
  canManage: boolean
  onRefresh: () => void
}) {
  const [packet, setPacket] = useState<SkillLifecyclePackage | null>(null)
  const [decision, setDecision] = useState<Decision>('retain')
  const [reason, setReason] = useState('')
  const [rootIds, setRootIds] = useState<string[]>([])
  const [validationIds, setValidationIds] = useState<string[]>([])
  const [evidenceIds, setEvidenceIds] = useState<string[]>([])
  const [challengeId, setChallengeId] = useState('')
  const [replacementRevision, setReplacementRevision] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const challenge = ['addressed', 'upheld', 'superseded'].includes(decision)
  const replacement = decision === 'supersede' || decision === 'superseded'
  const choices = validations
    .filter((row) => typeof row.validationId === 'string' && packet?.evidenceIds.includes(row.validationId))
    .map((row) => ({ id: String(row.validationId), name: `${row.caseId ?? '独立验证'} · ${row.status ?? '未知'} · ${row.validationId}` }))
  function invalidate() {
    setPacket(null)
    setConfirmed(false)
    setStale(true)
  }
  function fail(cause: unknown) {
    invalidate()
    setError(
      cause instanceof ApiError && cause.status === 409
        ? '状态已变化，请重读后准备新包；不会自动重试。'
        : `${cause instanceof Error ? cause.message : '请求失败'}。请重读后核对结果。`,
    )
  }
  async function scan() {
    if (!canManage || !reason.trim() || busy || stale) return
    setBusy(true)
    setError('')
    setConfirmed(false)
    try {
      await api.skills.scanImpact({ ...reference, expectedSequence: sequence, operationId: crypto.randomUUID(), reason })
      if (mounted.current) {
        invalidate()
        onRefresh()
      }
    } catch (cause) {
      if (mounted.current) fail(cause)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  async function prepare() {
    if (!canManage || busy || stale) return
    setBusy(true)
    setError('')
    setConfirmed(false)
    try {
      const value = await api.skills.prepareImpact(reference)
      if (!mounted.current) return
      if (
        value.projectId !== reference.projectId ||
        value.skillId !== reference.skillId ||
        value.revision !== reference.revision ||
        value.sequence !== sequence ||
        value.contentHash !== contentHash ||
        value.exportHash !== exportHash ||
        value.lifecycleWatermark !== data.watermark
      )
        throw new Error('影响包与当前版本、序号或生命周期水位不匹配')
      setPacket(value)
      setRootIds([])
      setValidationIds([])
      setEvidenceIds([])
      setChallengeId('')
    } catch (cause) {
      if (mounted.current) fail(cause)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  const rootsRestored = rootIds.every((id) => data.roots.some((row) => row.rootId === id && row.status === 'restored_pending'))
  const canSubmit =
    canManage &&
    !!packet &&
    !busy &&
    !stale &&
    confirmed &&
    !!reason.trim() &&
    (!replacement || /^[a-f0-9]{64}$/.test(replacementRevision)) &&
    (!challenge || !!challengeId) &&
    (decision !== 'addressed' || evidenceIds.length > 0) &&
    (decision !== 'restore' || (validationIds.length > 0 && rootsRestored))
  async function submit() {
    if (!canSubmit || !packet) return
    const mutation = {
      ...reference,
      expectedSequence: packet.sequence,
      operationId: crypto.randomUUID(),
      reason,
      packageId: packet.packageId,
      packageHash: packet.packageHash,
      replacementRevision: replacement ? replacementRevision : null,
    }
    setBusy(true)
    setError('')
    try {
      if (decision === 'addressed' || decision === 'upheld' || decision === 'superseded')
        await api.skills.resolveChallenge({ ...mutation, decision, challengeId, evidenceIds })
      else
        await api.skills.reviewImpact({
          ...mutation,
          decision,
          rootIds: decision === 'supersede' ? [] : rootIds,
          validationIds: decision === 'supersede' ? [] : validationIds,
        })
      if (mounted.current) {
        invalidate()
        onRefresh()
      }
    } catch (cause) {
      if (mounted.current) fail(cause)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  const changed =
    <T,>(setter: (value: T) => void) =>
    (value: T) => {
      setter(value)
      setConfirmed(false)
    }
  return (
    <section className="skills-panel skills-lifecycle" aria-label="生命周期与失效传播">
      <div className="skills-row">
        <h2>生命周期与失效传播</h2>
        <strong>{data.enabled ? data.gate.status : '尚未绑定观察主体'}</strong>
      </div>
      <p>
        {data.roots.length} 个未解除根因 · 管理序号 {sequence}。恢复一个根因不会解除其他根因；恢复观察仍需新验证与固定包审阅。
      </p>
      <p className="skills-muted">
        以下展示实际会话主体及追加理由。用户授权的助手代办须在理由中注明，不代表用户逐条亲审。来源不可读取与来源内容变化分别记录。
      </p>
      {data.gate.reasons.length > 0 && <p className="skills-notice skills-notice--warning">准入阻断：{data.gate.reasons.join('、')}</p>}
      <h3>来源与工具最新观察</h3>
      <table className="skills-governance-table">
        <thead>
          <tr>
            <th>主体</th>
            <th>状态</th>
            <th>预期 / 当前摘要</th>
          </tr>
        </thead>
        <tbody>
          {data.subjects.map((row) => (
            <tr key={row.subjectId}>
              <td>
                {row.kind === 'memory' ? '来源 Memory' : '工具'}
                <br />
                <code>{row.subjectId}</code>
              </td>
              <td>{statusNames[row.status]}</td>
              <td>
                <code>{row.expectedDigest}</code>
                <br />
                <code>{row.observedDigest ?? '未取得有效摘要'}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.roots.map((row) => (
        <details key={row.rootId} open>
          <summary>
            {row.kind === 'memory' ? '来源' : '工具'}根因 ·{' '}
            <span>{row.status === 'restored_pending' ? '已恢复观察，待审阅解除' : '仍在影响'}</span>
          </summary>
          <p>{row.reason}</p>
          <p>
            起源：{row.originSkillId} · <code>{row.originRevision}</code>
          </p>
          <p>
            根因 <code>{row.rootId}</code>
          </p>
          {row.paths.map((path, index) => (
            <p key={index}>
              固定依赖路径：<code>{path.join(' → ')}</code>
            </p>
          ))}
        </details>
      ))}
      <details>
        <summary>固定依赖版本与发布绑定</summary>
        {data.dependencies.length ? (
          data.dependencies.map((row) => (
            <p key={`${row.skillId}:${row.revision}`}>
              {row.skillId} · {row.state} · {row.allowed ? '准入允许' : '准入阻断'}
              <br />
              <code>{row.revision}</code>
              <br />
              Release <code>{row.releaseId ?? '尚无发布'}</code>
            </p>
          ))
        ) : (
          <p>没有固定 REQUIRES 依赖。</p>
        )}
      </details>
      <details>
        <summary>完整观察历史</summary>
        <Json value={data.observations} />
      </details>
      <details>
        <summary>恢复、替代及挑战处置历史</summary>
        {data.decisions.map((row) => (
          <div key={row.decisionId}>
            <h4>{row.action}</h4>
            <p>
              执行主体：{row.actor} · {row.decidedAt}
            </p>
            <p>{row.reason}</p>
            <Json value={row} />
          </div>
        ))}
      </details>
      <p className="skills-muted">撤回与发布在下方“人工审阅与发布”区处理。关联新版不会重新绑定已有依赖，也不会撤回旧版本。</p>
      {error && (
        <p role="alert" className="skills-notice skills-notice--warning">
          {error}
        </p>
      )}
      {stale && <Button onClick={onRefresh}>重新读取生命周期</Button>}
      <label className="skills-field">
        生命周期审阅理由
        <textarea
          maxLength={2000}
          value={reason}
          disabled={!canManage || busy || stale}
          onChange={(event) => changed(setReason)(event.target.value)}
          placeholder="说明授权、执行者、实际依据及保留的缺口"
        />
      </label>
      <div className="skills-actions">
        <Button disabled={!canManage || busy || stale || !reason.trim()} onClick={scan}>
          检查当前来源与工具
        </Button>
        <Button disabled={!canManage || busy || stale} onClick={prepare}>
          准备固定影响包
        </Button>
      </div>
      {!canManage && <p>检查和处置需要 owner 或平台管理员权限。</p>}
      {packet && (
        <div className="skills-review-package">
          <h3>固定影响包</h3>
          <p>
            执行主体：<span>{packet.actor}</span>
          </p>
          <p>
            包摘要 <code>{packet.packageHash}</code> · 序号 {packet.sequence}
          </p>
          <p>
            生命周期水位 <code>{packet.lifecycleWatermark}</code>
          </p>
          <details>
            <summary>包内根因、挑战与证据绑定</summary>
            <Json value={packet} />
          </details>
          <label className="skills-governance-field">
            处置决定
            <select value={decision} onChange={(event) => changed(setDecision)(event.target.value as Decision)}>
              {Object.entries(decisions).map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          {challenge ? (
            <>
              <label className="skills-governance-field">
                当前挑战
                <select value={challengeId} onChange={(event) => changed(setChallengeId)(event.target.value)}>
                  <option value="">选择固定包中的挑战</option>
                  {packet.currentChallengeIds.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
              </label>
              <Refs
                label="挑战依据"
                options={packet.evidenceIds.map((id) => ({ id, name: id }))}
                value={evidenceIds}
                onChange={changed(setEvidenceIds)}
              />
            </>
          ) : (
            !replacement && (
              <>
                <Refs
                  label="待处置根因"
                  options={packet.activeRootIds.map((id) => ({
                    id,
                    name: `${data.roots.find((row) => row.rootId === id)?.status ?? '待核对'} · ${id}`,
                  }))}
                  value={rootIds}
                  onChange={changed(setRootIds)}
                />
                <Refs label="恢复后的独立验证" options={choices} value={validationIds} onChange={changed(setValidationIds)} />
              </>
            )
          )}
          {decision === 'restore' && (
            <p className="skills-muted">只有已恢复的所选根因可提交；必须选择恢复后的独立验证。服务器仍会核对新鲜度、裁决及依赖资格。</p>
          )}
          {replacement && (
            <label className="skills-field">
              已发布的新版本摘要
              <input value={replacementRevision} onChange={(event) => changed(setReplacementRevision)(event.target.value)} maxLength={64} />
            </label>
          )}
          <label className="skills-confirm">
            <input
              type="checkbox"
              aria-label="确认此固定影响包"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            已核对固定包、当前证据及执行身份，确认提交这项处置。
          </label>
          <Button disabled={!canSubmit} loading={busy} onClick={submit}>
            提交固定包处置
          </Button>
        </div>
      )}
    </section>
  )
}
