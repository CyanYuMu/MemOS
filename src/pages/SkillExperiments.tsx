import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { SkillExperimentDetail, SkillExperimentGroup, SkillExperimentSummary } from '../../shared/contract'
import { Button } from '../components/ui/button'

const groups: Record<SkillExperimentGroup, string> = { none: '无经验', raw_memory: '原始 Memory', skill: '提炼 Skill' }
const phases = {
  frozen: '已冻结，等待执行',
  running: '固定实验执行中',
  completed: '执行结束，仍需审阅',
  interrupted: '执行中断，保留未知记录',
}
const outcomes = {
  queued: '等待执行',
  running: '执行中',
  passed: '行为验收通过',
  failed: '行为验收未通过',
  unknown: '结果未知',
  infra_invalid: '基础设施无效',
}
const number = (value: number | null, digits = 2) => (value === null ? '未知' : value.toFixed(digits))
const errorText = (value: unknown) => (value instanceof Error ? value.message : '实验查询失败，请刷新后核对')

/** 只操作宿主登记的冻结实验；完成、使用审阅和发布资格是不同结论。 */
export function SkillExperiments({ projectId, canWrite }: { projectId: string; canWrite: boolean }) {
  const [items, setItems] = useState<SkillExperimentSummary[]>([])
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<SkillExperimentDetail | null>(null)
  const [listError, setListError] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [reload, setReload] = useState(0)
  const [detailReload, setDetailReload] = useState(0)
  const chosen = items.find((item) => item.experimentId === selected) ?? items[0]
  const experimentId = chosen?.experimentId ?? ''
  useEffect(() => {
    let active = true
    api.skills
      .experiments({ projectId })
      .then((value) => {
        if (active) {
          setItems(value.items)
          setListError('')
        }
      })
      .catch((cause: unknown) => {
        if (active) setListError(errorText(cause))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [projectId, reload])
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    async function read() {
      try {
        const value = await api.skills.experiment({ projectId, experimentId })
        if (!active) return
        if (
          value.experimentId !== experimentId ||
          value.manifestHash !== chosen?.manifestHash ||
          value.revision !== chosen?.revision ||
          value.skillId !== chosen?.skillId
        )
          throw new Error('实验身份与当前选择不匹配，请重新读取')
        setDetail(value)
        setError('')
        if (value.status === 'running')
          timer = setTimeout(() => {
            void read()
          }, 2000)
      } catch (cause) {
        if (active) {
          setError(errorText(cause))
          setDetail(null)
        }
      }
    }
    if (experimentId) void read()
    return () => {
      active = false
      if (timer) clearTimeout(timer)
    }
  }, [projectId, experimentId, chosen?.manifestHash, chosen?.revision, chosen?.skillId, reload, detailReload])
  const current = detail?.experimentId === experimentId ? detail : null
  async function start() {
    if (!current || current.status !== 'frozen' || !canWrite || starting || error) return
    setStarting(true)
    try {
      await api.skills.startExperiment({ projectId, experimentId, operationId: crypto.randomUUID() })
      setDetailReload((value) => value + 1)
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setStarting(false)
    }
  }
  return (
    <section className="skills-panel skills-experiments" aria-label="固定三组实验">
      <div className="skills-row">
        <div>
          <h2>固定三组独立实验</h2>
          <p className="skills-muted">无经验 · 原始 Memory · 提炼 Skill。七个独立场景，共 21 次执行。</p>
        </div>
        <Button variant="outline" onClick={() => setReload((value) => value + 1)}>
          刷新实验
        </Button>
      </div>
      <p className="skills-notice skills-notice--warning">
        实验完成不代表发布批准；边界判断、规则使用和严重违规仍需人工审阅，声明不计作独立覆盖。
      </p>
      {loading && <p role="status">正在读取已登记实验…</p>}
      {(listError || error) && (
        <p role="alert" className="skills-error">
          {listError || error}
        </p>
      )}
      {!loading && !listError && !items.length && <p className="skills-muted">本项目尚无冻结实验。任务、来源与预算需由宿主先登记。</p>}
      {!!items.length && (
        <label className="skills-experiment-select">
          实验清单
          <select
            aria-label="选择固定实验"
            value={experimentId}
            onChange={(event) => {
              setSelected(event.target.value)
              setError('')
            }}
          >
            {items.map((item) => (
              <option key={item.experimentId} value={item.experimentId}>
                {item.experimentId} · {item.revision.slice(0, 12)}
              </option>
            ))}
          </select>
        </label>
      )}
      {current && (
        <>
          <div className="skills-row">
            <strong>{phases[current.status]}</strong>
            <span>
              {current.completed} / {current.planned} 单元已结束
            </span>
            <Button
              disabled={!canWrite || current.status !== 'frozen' || starting || !!error}
              onClick={() => {
                void start()
              }}
            >
              {starting ? '正在提交…' : '开始固定实验'}
            </Button>
          </div>
          <dl className="skills-experiment-identities">
            <dt>实验清单摘要</dt>
            <dd>
              <code>{current.manifestHash}</code>
            </dd>
            <dt>固定候选版本</dt>
            <dd>
              <code>{current.revision}</code>
            </dd>
          </dl>
          <p className="skills-muted">
            每单元上限 {current.budget.timeoutSeconds} 秒 / {current.budget.maxTurns} 轮 / {current.budget.maxBudgetUsd} USD；经验容量{' '}
            {current.budget.contextBytes} 字节。完整三组配对：{current.summary.completePairs} / 7。
          </p>
          <div className="skills-table-scroll">
            <table className="skills-experiment-table">
              <caption>分层结果与全部执行成本</caption>
              <thead>
                <tr>
                  <th>实验组</th>
                  <th>H1–H3 修复</th>
                  <th>H4–H7 任务解决</th>
                  <th>边界待审</th>
                  <th>未知 / 基础设施无效</th>
                  <th>耗时（秒）</th>
                  <th>报告费用（USD）</th>
                </tr>
              </thead>
              <tbody>
                {current.summary.groups.map((score) => (
                  <tr key={score.group}>
                    <th>{groups[score.group]}</th>
                    <td>
                      {score.primaryRepair.passed} / {score.primaryRepair.denominator}
                      <small>计划 {score.primaryRepair.planned}</small>
                    </td>
                    <td>
                      {score.boundary.taskSolved} / {score.boundary.planned}
                    </td>
                    <td>{score.boundary.pendingReview}</td>
                    <td>
                      {score.unknown} / {score.infraInvalid}
                    </td>
                    <td>{number(score.elapsedSeconds)}</td>
                    <td>
                      {number(score.reportedCostUsd, 4)}
                      <small>费用未知 {score.costUnknown} 次</small>
                    </td>
                  </tr>
                ))}
                {!current.summary.groups.length && (
                  <tr>
                    <td colSpan={7}>尚无执行结果</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="skills-table-scroll">
            <table className="skills-experiment-table">
              <caption>逐任务记录</caption>
              <thead>
                <tr>
                  <th>任务 / 组</th>
                  <th>执行结果</th>
                  <th>修复分</th>
                  <th>加载 / 采集</th>
                  <th>最终凭条</th>
                  <th>边界 / 规则 / 违规判断</th>
                  <th>耗时 / 费用</th>
                </tr>
              </thead>
              <tbody>
                {current.rows.map((row) => (
                  <tr key={row.cellId}>
                    <th>
                      {row.caseId}
                      <small>{groups[row.group]}</small>
                    </th>
                    <td>
                      {outcomes[row.status]}
                      {row.failureReasons.map((reason) => (
                        <small key={reason}>{reason}</small>
                      ))}
                    </td>
                    <td>{row.repairPass ?? '不适用或未知'}</td>
                    <td>
                      {row.loading} / {row.capture}
                    </td>
                    <td>{row.receiptVerified ? '已绑定核验' : '未核验'}</td>
                    <td>待人工审阅</td>
                    <td>
                      {number(row.elapsedSeconds)} 秒<small>{number(row.reportedCostUsd, 4)} USD</small>
                    </td>
                  </tr>
                ))}
                {!current.rows.length && (
                  <tr>
                    <td colSpan={7}>等待固定任务执行</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <ul className="skills-reasons">
            {current.limitations.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
