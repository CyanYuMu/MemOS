import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type {
  SkillDetail,
  SkillJob,
  SkillLoadPreview,
  SkillReadiness,
  SkillRef,
  SkillReviewPackage,
  SkillRule,
  SkillSummary,
} from '../lib/api'
import { useMeta, useProject, useProjectRole } from '../lib/store'
import { PageHeader } from '../components/ui'
import { Button } from '../components/ui/button'
import { SkillExperiments } from './SkillExperiments'

const states: Record<string, string> = {
  candidate: '候选',
  needs_evidence: '待补证据',
  blocked: '已阻断',
  published: '已发布',
  paused: '已暂停',
  withdrawn: '已撤回',
}
const readinessNames: Record<string, string> = { PASS: '可提交人工发布', NEEDS_EVIDENCE: '需要补证据', BLOCK: '阻断发布' }
const jobNames: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  passed: '本次执行已完成',
  failed: '执行失败',
  unknown: '结果未知',
}
const reasons: Record<string, string> = {
  open_challenges: '存在未解决的审阅挑战',
  independent_validation_missing: '尚缺独立任务验证',
  independent_task_missing: '尚缺独立任务验证',
  not_published: '此版本尚未发布',
  unsupported_dependencies: '适配器尚不支持声明的依赖',
  source_changed: '来源水位已变化',
  revision_blocked: '当前版本已被阻断',
  revision_paused: '当前版本已暂停',
  revision_withdrawn: '当前版本已撤回',
  export_missing: '尚缺固定版本的原生 Skill 导出',
  evaluation_pending: '仍有评测任务未结束',
  validation_failure_unresolved: '存在尚未解决的失败验证',
  validation_unknown: '存在结果未知的验证',
  branch_coverage_missing: '独立任务尚未覆盖全部必需分支',
  boundary_coverage_missing: '独立任务尚未覆盖全部边界',
  scope_coverage_missing: '发布范围尚未获得完整任务覆盖',
  validation_binding_mismatch: '验证报告与固定候选或导出不匹配',
  release_unavailable: '无有效发布许可，候选、挑战、状态或依赖未满足要求',
  management_test_only: '合成管理测试记录不能用于正常加载',
  outside_release_scope: '当前任务超出已批准的发布范围',
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : '请求失败，请重新读取后再操作'
}
function JsonText({ value }: { value: unknown }) {
  return <pre className="skills-json">{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre>
}
function Reasons({ value }: { value: string[] }) {
  return value.length ? (
    <ul className="skills-reasons">
      {value.map((reason, i) => (
        <li key={i}>{reasons[reason] ? `${reasons[reason]}（${reason}）` : reason}</li>
      ))}
    </ul>
  ) : null
}
function Readiness({ value }: { value: SkillReadiness }) {
  return (
    <div className={`skills-notice ${value.status === 'PASS' ? '' : 'skills-notice--warning'}`}>
      <strong>{readinessNames[value.status] ?? '发布资格未知'}</strong>
      <Reasons value={value.reasons} />
      {value.status === 'PASS' && <p>检查通过后，仍需管理者核对固定审阅包并明确批准。</p>}
    </div>
  )
}
function TextList({ title, values }: { title: string; values: string[] }) {
  return (
    <section className="skills-text-section">
      <h3>{title}</h3>
      {values.length ? (
        <ul>
          {values.map((value, i) => (
            <li key={i}>{value}</li>
          ))}
        </ul>
      ) : (
        <p className="skills-muted">未提供</p>
      )}
    </section>
  )
}

/** 切项目时卸载整个工作区，旧项目的异步响应及审阅包不能进入新项目。 */
export function Skills() {
  const { currentProjectId, ready } = useProject()
  const { role } = useProjectRole()
  const { account } = useMeta()
  const canManage = account?.platformAdmin === true || role === 'owner'
  const canWrite = canManage || role === 'editor'
  return (
    <div className="skills-page">
      <PageHeader title="Skill 管理" desc="从来源证据到固定版本：隔离评测、人工审阅与加载准入。" />
      {!ready || !currentProjectId ? (
        <p role="status">正在确认当前项目…</p>
      ) : (
        <SkillsProject key={currentProjectId} projectId={currentProjectId} canWrite={canWrite} canManage={canManage} />
      )}
    </div>
  )
}

function SkillsProject({ projectId, canWrite, canManage }: { projectId: string; canWrite: boolean; canManage: boolean }) {
  const [items, setItems] = useState<SkillSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [state, setState] = useState('')
  const [selected, setSelected] = useState('')
  const [reload, setReload] = useState(0)
  const refresh = useCallback(() => {
    setLoading(true)
    setError('')
    setReload((n) => n + 1)
  }, [])
  useEffect(() => {
    let active = true
    api.skills
      .list({ projectId })
      .then((result) => {
        if (active) setItems(result.items)
      })
      .catch((cause: unknown) => {
        if (active) setError(errorText(cause))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [projectId, reload])
  const visible = items.filter(
    (item) =>
      (!state || item.state === state) &&
      `${item.title} ${item.skillId} ${item.tags.join(' ')}`.toLowerCase().includes(query.toLowerCase()),
  )
  const chosen = visible.find((item) => `${item.skillId}@${item.revision}` === selected) ?? visible[0]
  return (
    <>
      <div className="skills-process" aria-label="Skill 状态说明">
        <span>01 来源与结构</span>
        <span>02 隔离评测</span>
        <span>03 规则审阅</span>
        <span>04 人工发布</span>
      </div>
      <p className="skills-muted skills-boundary">结构通过 ≠ 任务通过 ≠ 规则正确 ≠ 已发布。候选版本始终保持不可变。</p>
      <div className="skills-layout">
        <aside className="skills-catalog" aria-label="Skill 列表">
          <div className="skills-catalog__filters">
            <label htmlFor="skill-search">搜索 Skill</label>
            <input
              id="skill-search"
              className="skills-input"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="名称、标签或 ID"
            />
            <label htmlFor="skill-state">管理状态</label>
            <select id="skill-state" className="skills-input" value={state} onChange={(event) => setState(event.target.value)}>
              <option value="">全部状态</option>
              {Object.entries(states).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <div className="skills-row">
              <span className="skills-muted">{visible.length} 个版本</span>
              <Button size="sm" onClick={refresh} disabled={loading}>
                刷新列表
              </Button>
            </div>
          </div>
          {loading && (
            <p role="status" className="skills-empty">
              正在读取候选…
            </p>
          )}
          {error && (
            <p role="alert" className="skills-empty">
              {error}
            </p>
          )}
          {!loading && !error && !items.length && <p className="skills-empty">当前项目还没有 Skill 候选</p>}
          {!loading && !error && items.length > 0 && !visible.length && <p className="skills-empty">没有匹配的版本，请调整筛选条件。</p>}
          {visible.map((item) => (
            <button
              type="button"
              className={`skills-list-item ${chosen === item ? 'is-selected' : ''}`}
              key={`${item.skillId}@${item.revision}`}
              aria-pressed={chosen === item}
              onClick={() => setSelected(`${item.skillId}@${item.revision}`)}
            >
              <span className="skills-row">
                <strong>{item.title}</strong>
                <span className={`skills-badge skills-badge--${item.state}`}>{states[item.state] ?? item.state}</span>
              </span>
              <span className="skills-list-item__meta">
                {item.ruleCount} 条规则 · {item.validationCount} 次验证
              </span>
              <code>{item.revision.slice(0, 12)}</code>
              <span className="skills-list-item__readiness">{readinessNames[item.readiness.status] ?? '发布资格未知'}</span>
            </button>
          ))}
        </aside>
        {chosen ? (
          <SkillWorkspace
            key={`${chosen.skillId}@${chosen.revision}`}
            projectId={projectId}
            skillId={chosen.skillId}
            revision={chosen.revision}
            canWrite={canWrite}
            canManage={canManage}
            onChange={refresh}
          />
        ) : (
          <div className="skills-panel skills-empty skills-empty--main">
            <h2>经验成为 Skill 之前</h2>
            <p>在这里查看适用边界、逐条核对来源，并将评测与发布决定留在同一条证据链中。</p>
          </div>
        )}
      </div>
      <SkillExperiments projectId={projectId} canWrite={canWrite} />
    </>
  )
}

function SkillWorkspace({
  projectId,
  skillId,
  revision,
  canWrite,
  canManage,
  onChange,
}: SkillRef & { canWrite: boolean; canManage: boolean; onChange: () => void }) {
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  useEffect(() => {
    let active = true
    api.skills
      .detail({ projectId, skillId, revision })
      .then((value) => {
        if (value.projectId !== projectId || value.candidate.skill_id !== skillId || value.candidate.revision !== revision)
          throw new Error('响应版本与当前选择不匹配，请重新读取')
        if (active) setDetail(value)
      })
      .catch((cause: unknown) => {
        if (active) setError(errorText(cause))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [projectId, skillId, revision, reload])
  const refresh = useCallback(() => {
    setLoading(true)
    setError('')
    setReload((n) => n + 1)
  }, [])
  if (loading)
    return (
      <div className="skills-panel skills-empty" role="status">
        正在读取固定版本及管理状态…
      </div>
    )
  if (error || !detail)
    return (
      <div className="skills-panel skills-empty">
        <p role="alert">{error || '未读取到详情'}</p>
        <Button onClick={refresh}>重试读取详情</Button>
      </div>
    )
  const { candidate, management } = detail
  return (
    <main className="skills-detail">
      <section className="skills-panel skills-detail__heading">
        <div className="skills-row">
          <span className="skills-eyebrow">固定版本 · {candidate.purpose}</span>
          <div className="skills-actions">
            <span className={`skills-badge skills-badge--${management.state}`}>{states[management.state] ?? management.state}</span>
            <Button size="sm" onClick={refresh}>
              重新读取详情
            </Button>
          </div>
        </div>
        <h2>{candidate.title}</h2>
        <p>{candidate.goal}</p>
        <div className="skills-tags">
          {candidate.tags.map((tag) => (
            <span key={tag}>{tag}</span>
          ))}
        </div>
        <dl className="skills-identities">
          <dt>Skill ID</dt>
          <dd>{skillId}</dd>
          <dt>规范修订号</dt>
          <dd>
            <code>{revision}</code>
          </dd>
          <dt>原生投影版本</dt>
          <dd>
            <code>{management.projectionVersion ?? '尚未绑定'}</code>
          </dd>
          <dt>原生导出摘要</dt>
          <dd>
            <code>{management.exportHash ?? '尚未生成'}</code>
          </dd>
          <dt>管理序号</dt>
          <dd>{management.sequence}</dd>
        </dl>
        <p className="skills-muted">
          规范修订号绑定 Skill 内容；原生投影版本与导出摘要绑定当前 Agent 工件。投影升级可能产生新的导出摘要，历史运行保留各自绑定。
        </p>
        <div className="skills-status-grid">
          <div>
            <span>来源与结构</span>
            <strong>已读取登记版本</strong>
            <small>引用有效不代表语义正确</small>
          </div>
          <div>
            <span>独立任务验证</span>
            <strong>{detail.validations.some((item) => item.kind === 'independent_task') ? '已有记录，待审阅' : '尚缺独立验证'}</strong>
            <small>开发评测不替代独立任务</small>
          </div>
          <div>
            <span>规则内容</span>
            <strong>需人工审阅</strong>
            <small>逐条判断来源是否支持结论</small>
          </div>
          <div>
            <span>发布状态</span>
            <strong>发布状态：{states[management.state] ?? '未知'}</strong>
            <small>正常加载仍需服务端准入</small>
          </div>
        </div>
        <Readiness value={management.readiness} />
      </section>
      <section className="skills-panel">
        <h2>适用范围与验收</h2>
        <div className="skills-text-grid">
          <TextList title="适用条件" values={candidate.applicability} />
          <TextList title="不适用场景" values={candidate.exclusions} />
          <TextList title="前置要求" values={candidate.prerequisites} />
          <TextList title="约束" values={candidate.constraints} />
          <TextList title="验收要求" values={candidate.acceptance} />
          <TextList title="已知局限" values={candidate.limitations} />
        </div>
        <details>
          <summary>退出条件与依赖</summary>
          <JsonText value={{ exits: candidate.exits, requires: candidate.requires, host_limits: candidate.host_limits }} />
        </details>
      </section>
      <section className="skills-panel">
        <div className="skills-row">
          <h2>规则与来源证据</h2>
          <span className="skills-muted">入口：{candidate.entry_rule}</span>
        </div>
        <p className="skills-muted">引用定位与主张分开显示；证据内容仅作文本展示。</p>
        {candidate.rules.map((rule) => (
          <RuleEvidence key={rule.rule_id} rule={rule} reference={{ projectId, skillId, revision }} />
        ))}
        <details>
          <summary>来源 MemorySnapshot 与证据版本</summary>
          <JsonText value={candidate.source_refs} />
        </details>
      </section>
      <Evaluation detail={detail} reference={{ projectId, skillId, revision }} canWrite={canWrite} />
      <Review
        key={management.sequence}
        detail={detail}
        reference={{ projectId, skillId, revision }}
        canManage={canManage}
        onRefresh={refresh}
        onChange={onChange}
      />
    </main>
  )
}

function RuleEvidence({ rule, reference }: { rule: SkillRule; reference: SkillRef }) {
  const [facts, setFacts] = useState<Record<string, unknown>[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function load() {
    setBusy(true)
    setError('')
    try {
      const result = await api.skills.evidence({ ...reference, ruleId: rule.rule_id })
      setFacts(result.facts)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }
  return (
    <article className="skills-rule">
      <div className="skills-row">
        <h3>{rule.rule_id}</h3>
        <span className="skills-badge">
          {({ empirical: '经验规则', normative: '契约规则', hypothesis: '待证假设' } as Record<string, string>)[rule.basis] ?? rule.basis}
        </span>
      </div>
      <dl className="skills-rule__steps">
        <dt>条件</dt>
        <dd>{rule.condition}</dd>
        <dt>观察</dt>
        <dd>{rule.observation}</dd>
        <dt>行动</dt>
        <dd>{rule.action}</dd>
        <dt>预期结果</dt>
        <dd>{rule.expected_result}</dd>
      </dl>
      <div className="skills-branches">
        {rule.branches.map((branch, i) => (
          <p key={i}>
            <span>{branch.when}</span>
            <code>→ {branch.target}</code>
          </p>
        ))}
      </div>
      <details>
        <summary>{rule.evidence_links.length} 条证据关联 · 语义支持待审阅</summary>
        {rule.evidence_links.map((link, i) => (
          <JsonText key={i} value={link} />
        ))}
      </details>
      <Button size="sm" loading={busy} onClick={load}>
        查看 {rule.rule_id} 来源片段
      </Button>
      {error && <p role="alert">{error}</p>}
      {facts && (
        <div className="skills-evidence">
          <h4>固定证据包片段</h4>
          {facts.length ? facts.map((fact, i) => <JsonText key={i} value={fact} />) : <p>未返回来源片段，不能据此认定规则成立。</p>}
        </div>
      )}
    </article>
  )
}

function Evaluation({ detail, reference, canWrite }: { detail: SkillDetail; reference: SkillRef; canWrite: boolean }) {
  const [caseId, setCaseId] = useState(detail.caseOptions[0]?.caseId ?? '')
  const [job, setJob] = useState<SkillJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<SkillLoadPreview | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [pollError, setPollError] = useState('')
  const [pollRetry, setPollRetry] = useState(0)
  const jobId = job?.jobId
  // 只轮询查询端点。发起任务与发布操作均不自动重试。
  useEffect(() => {
    if (!jobId) return
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      try {
        const result = await api.skills.job({ projectId: reference.projectId, jobId: jobId! })
        if (!active) return
        setJob(result)
        setPollError('')
        if (result.status === 'queued' || result.status === 'running') timer = setTimeout(poll, 1500)
      } catch (cause) {
        if (active) setPollError(errorText(cause))
      }
    }
    void poll()
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [jobId, reference.projectId, pollRetry])
  async function evaluate() {
    if (!canWrite || !caseId) return
    setBusy(true)
    setError('')
    try {
      setJob(await api.skills.evaluate({ ...reference, caseId, operationId: crypto.randomUUID() }))
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }
  async function loadPreview() {
    setPreviewBusy(true)
    setPreview(null)
    try {
      setPreview(await api.skills.loadPreview({ ...reference, caseId, mode: 'normal' }))
    } catch (cause) {
      setPreview({ allowed: false, reasons: [errorText(cause)] })
    } finally {
      setPreviewBusy(false)
    }
  }
  const running = job?.status === 'queued' || job?.status === 'running'
  return (
    <section className="skills-panel">
      <h2>候选隔离评测</h2>
      <p className="skills-muted">isolated_dev 仅验证固定版本加载、隔离与回收链，不能单独支持普通发布，也不代表 Skill 有因果收益。</p>
      <label className="skills-field" htmlFor="skill-case">
        服务器许可的开发任务
        <select
          id="skill-case"
          className="skills-input"
          value={caseId}
          onChange={(event) => {
            setCaseId(event.target.value)
            setPreview(null)
          }}
        >
          {!detail.caseOptions.length && <option value="">暂无可运行任务</option>}
          {detail.caseOptions.map((option) => (
            <option key={option.caseId} value={option.caseId}>
              {option.title} · {option.caseId}
            </option>
          ))}
        </select>
      </label>
      <div className="skills-actions">
        <Button variant="primary" loading={busy} disabled={!canWrite || !caseId || running || !!error} onClick={evaluate}>
          开始隔离评测
        </Button>
        <Button loading={previewBusy} disabled={!caseId} onClick={loadPreview}>
          检查 normal 加载准入
        </Button>
      </div>
      {!canWrite && <p className="skills-muted">当前角色无评测权限（需要 editor、owner 或平台管理员）。权限未知时禁用写入。</p>}
      {error && (
        <div role="alert" className="skills-notice skills-notice--warning">
          {error}
          <p>请求未自动重试。请重新读取版本，先确认是否已生成任务。</p>
        </div>
      )}
      {preview && (
        <div role="status" className={`skills-notice ${preview.allowed === true ? '' : 'skills-notice--warning'}`}>
          <strong>{preview.allowed === true ? '服务端允许此版本本次 normal 加载' : 'normal 加载被拒绝'}</strong>
          <Reasons value={preview.reasons ?? [preview.reason ?? preview.errorCode ?? '服务端未授予加载许可']} />
          <details>
            <summary>加载准入详情</summary>
            <JsonText value={preview} />
          </details>
        </div>
      )}
      {job && <JobResult job={job} />}
      {pollError && (
        <div role="alert" className="skills-notice skills-notice--warning">
          <p>运行状态读取失败：{pollError}</p>
          <Button size="sm" onClick={() => setPollRetry((n) => n + 1)}>
            重试读取运行状态
          </Button>
        </div>
      )}
      <details>
        <summary>
          历史运行（{detail.jobs.length}）与验证记录（{detail.validations.length}）
        </summary>
        {detail.jobs.map((item) => (
          <JobResult key={item.jobId} job={item} />
        ))}
        {detail.validations.map((item, i) => (
          <JsonText key={i} value={item} />
        ))}
        {!detail.jobs.length && !detail.validations.length && <p>暂无记录</p>}
      </details>
    </section>
  )
}

function JobResult({ job }: { job: SkillJob }) {
  const accepted = job.taskResult?.status === 'passed' && job.taskResult.accepted === true && job.taskResult.receiptMatches === true
  return (
    <article className="skills-job">
      <div className="skills-row">
        <strong>{jobNames[job.status] ?? '状态未知'}</strong>
        <code>{job.jobId}</code>
      </div>
      <p>
        {job.caseId ?? '任务待分配'} · {job.mode ?? 'isolated_dev'}
      </p>
      <div className="skills-status-grid">
        <div>
          <span>外部任务验收</span>
          <strong>{accepted ? '外部任务验收通过' : job.taskResult?.status === 'failed' ? '外部任务验收失败' : '外部任务验收未知'}</strong>
          <small>验收与最终补丁绑定：{job.taskResult?.receiptMatches === true ? '已核验' : '未证实'}</small>
        </div>
        <div>
          <span>固定版本加载</span>
          <strong>{job.skillUsage?.loading === 'verified' ? '加载已核验' : '加载未证实'}</strong>
          <small>事件采集：{job.skillUsage?.capture === 'complete' ? '完整' : '不完整或未知'}</small>
        </div>
        <div>
          <span>规则使用</span>
          <strong>规则使用：未审查</strong>
          <small>声明使用不等于正确应用</small>
        </div>
      </div>
      {job.skillUsage?.declaredRuleIds.length ? <p>声明的规则：{job.skillUsage.declaredRuleIds.join('、')}</p> : <p>尚无规则使用声明</p>}
      {job.skillUsage?.endedAdmission === 'needs_review' && (
        <p className="skills-notice skills-notice--warning">运行期间准入条件变化，结果需要复核。</p>
      )}
      {job.errorCode && <p role="alert">运行错误：{job.errorCode}</p>}
    </article>
  )
}

type ReviewAction = 'needs_evidence' | 'block' | 'challenge' | 'pause' | 'withdraw'
const actionNames: Record<ReviewAction, string> = {
  needs_evidence: '要求补证据',
  block: '阻断发布',
  challenge: '登记新挑战',
  pause: '暂停此版本',
  withdraw: '撤回此版本',
}
function Review({
  detail,
  reference,
  canManage,
  onRefresh,
  onChange,
}: {
  detail: SkillDetail
  reference: SkillRef
  canManage: boolean
  onRefresh: () => void
  onChange: () => void
}) {
  const [proposal, setProposal] = useState<SkillReviewPackage | null>(null)
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [success, setSuccess] = useState('')
  const [action, setAction] = useState<ReviewAction | ''>('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  function fail(cause: unknown) {
    setProposal(null)
    setConfirmed(false)
    setConflict(true)
    setError(
      cause instanceof ApiError && cause.status === 409
        ? '状态冲突，请重新读取当前版本并重新审阅。不会自动重试批准。'
        : `${errorText(cause)}。请重新读取后确认操作结果。`,
    )
  }
  async function prepare() {
    if (!canManage) return
    setBusy(true)
    setError('')
    setConfirmed(false)
    try {
      const value = await api.skills.prepareReview(reference)
      if (
        value.skillId !== reference.skillId ||
        value.revision !== reference.revision ||
        value.sequence !== detail.management.sequence ||
        value.contentHash !== detail.candidate.content_hash
      )
        throw new Error('审阅包绑定内容已变化')
      setProposal(value)
    } catch (cause) {
      fail(cause)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  async function submit(publish: boolean) {
    if (!canManage || !reason.trim() || conflict || (publish && (!proposal || !confirmed || proposal.readiness.status !== 'PASS'))) return
    setBusy(true)
    setError('')
    const body = { ...reference, expectedSequence: detail.management.sequence, operationId: crypto.randomUUID(), reason: reason.trim() }
    try {
      if (publish && proposal)
        await api.skills.publish({
          ...body,
          packageId: proposal.packageId,
          packageHash: proposal.packageHash,
          expectedSequence: proposal.sequence,
        })
      else if (action === 'needs_evidence' || action === 'block') await api.skills.decide({ ...body, decision: action })
      else if (action === 'pause' || action === 'withdraw') await api.skills.status({ ...body, action })
      else if (action === 'challenge') await api.skills.challenge(body)
      else return
      setSuccess(publish ? '发布事务已提交，请以重新读取后的状态为准。' : '审阅决定已提交。')
      setProposal(null)
      setConfirmed(false)
      setConflict(true)
      onChange()
      onRefresh()
    } catch (cause) {
      fail(cause)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }
  const canPublish =
    canManage &&
    !!proposal &&
    proposal.readiness.status === 'PASS' &&
    detail.management.readiness.status === 'PASS' &&
    confirmed &&
    !!reason.trim() &&
    !conflict
  return (
    <section className="skills-panel">
      <div className="skills-row">
        <h2>人工审阅与发布</h2>
        <span className="skills-muted">序号 {detail.management.sequence}</span>
      </div>
      <Readiness value={detail.management.readiness} />
      <details open={detail.management.challenges.length > 0}>
        <summary>审阅挑战（{detail.management.challenges.length}）</summary>
        {detail.management.challenges.map((item, i) => (
          <JsonText value={item} key={i} />
        ))}
      </details>
      <Button loading={busy} disabled={!canManage || conflict} onClick={prepare}>
        生成固定审阅包
      </Button>
      {!canManage && <p className="skills-muted">人工审阅和发布需要 owner 或平台管理员权限；editor 只能发起隔离评测。</p>}
      {proposal && (
        <div className="skills-review-package">
          <h3>待确认的固定审阅包</h3>
          <dl className="skills-identities">
            <dt>审阅包</dt>
            <dd>{proposal.packageId}</dd>
            <dt>包摘要</dt>
            <dd>
              <code>{proposal.packageHash}</code>
            </dd>
            <dt>固定序号</dt>
            <dd>{proposal.sequence}</dd>
            <dt>内容摘要</dt>
            <dd>
              <code>{proposal.contentHash}</code>
            </dd>
            <dt>原生导出</dt>
            <dd>
              <code>{proposal.exportHash ?? '尚未生成或未提供'}</code>
            </dd>
            {proposal.actor && (
              <>
                <dt>审阅主体</dt>
                <dd>{proposal.actor}</dd>
              </>
            )}
            {proposal.preparedAt && (
              <>
                <dt>生成时间</dt>
                <dd>{proposal.preparedAt}</dd>
              </>
            )}
          </dl>
          <Readiness value={proposal.readiness} />
          <h4>人工核对清单</h4>
          <p className="skills-muted">
            逐条核对证据是否支持规则、分支行为是否正确，以及验证是否保留发现真实错误的能力。覆盖记录不能替代语义判断。
          </p>
          <div className="skills-review-checklist">
            {proposal.reviewChecklist.map((item) => (
              <div key={item.ruleId} className="skills-rule">
                <div className="skills-row">
                  <strong>{item.ruleId}</strong>
                  <span>
                    独立分支覆盖 {item.coveredBranches.length} / {item.branchCount}
                  </span>
                </div>
                <p className="skills-muted">{item.coveredBranches.length ? item.coveredBranches.join('、') : '尚无独立验证覆盖记录'}</p>
              </div>
            ))}
          </div>
          <details>
            <summary>绑定的验证报告与来源水位</summary>
            <JsonText value={{ validationIds: proposal.validationIds, sourceWatermark: proposal.sourceWatermark }} />
          </details>
          <label className="skills-confirm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={!canManage || proposal.readiness.status !== 'PASS' || conflict}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            我已核对固定审阅包、逐条规则依据、独立任务与分支覆盖，确认发布此精确版本。
          </label>
          <Button variant="primary" disabled={!canPublish} loading={busy} onClick={() => submit(true)}>
            确认发布此版本
          </Button>
        </div>
      )}
      <label className="skills-field" htmlFor="skill-review-reason">
        审阅理由
        <textarea
          id="skill-review-reason"
          maxLength={2000}
          className="skills-input"
          rows={3}
          value={reason}
          disabled={!canManage || conflict || busy}
          onChange={(event) => {
            setReason(event.target.value)
            setConfirmed(false)
          }}
          placeholder="说明依据、缺口或风险；发布、退回、挑战和状态变更均必须记录理由。"
        />
      </label>
      <div className="skills-actions">
        <label className="skills-field" htmlFor="skill-review-action">
          其他管理决定
          <select
            id="skill-review-action"
            className="skills-input"
            value={action}
            disabled={!canManage || conflict || busy}
            onChange={(event) => setAction(event.target.value as ReviewAction)}
          >
            <option value="">选择决定</option>
            {Object.entries(actionNames).map(([value, label]) => (
              <option
                key={value}
                value={value}
                disabled={
                  (value === 'pause' && detail.management.state !== 'published') ||
                  (value === 'withdraw' && !['published', 'paused'].includes(detail.management.state))
                }
              >
                {label}
              </option>
            ))}
          </select>
        </label>
        <Button disabled={!canManage || !action || !reason.trim() || conflict} loading={busy} onClick={() => submit(false)}>
          提交管理决定
        </Button>
      </div>
      {error && (
        <p role="alert" className="skills-notice skills-notice--warning">
          {error}
        </p>
      )}
      {success && <p role="status">{success}</p>}
      {conflict && <Button onClick={onRefresh}>重新读取当前版本</Button>}
      <details>
        <summary>历史发布记录（{detail.releases.length}）</summary>
        {detail.releases.length ? (
          detail.releases.map((release, i) => <JsonText key={i} value={release} />)
        ) : (
          <p>尚无发布记录。结构报告或任务通过不会自动创建 Release。</p>
        )}
      </details>
    </section>
  )
}
