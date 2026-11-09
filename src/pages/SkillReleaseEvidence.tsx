import type { SkillReleaseAssessment } from '../../shared/contract'

const kindNames = {
  healthy: '健康确认',
  repair: '缺陷修复',
  out_of_scope: '范围外停止',
  missing_evidence: '缺少合同证据',
  unresolved: '仍有可复现失败',
  tool_failure: '公开检查工具不可用',
}
export function SkillReleaseEvidence({ assessment }: { assessment: SkillReleaseAssessment }) {
  const { taskOutcome, processVerification: process, executionQualification: plan, publicVerification } = assessment
  return (
    <section aria-label="新版独立任务证据">
      <h3>新版独立任务：{kindNames[taskOutcome.kind]}</h3>
      <p>
        {taskOutcome.eligible ? '任务结局可供审阅' : '任务结局证据不足'} · {plan.planQualified ? '批次计划合规' : '批次计划不合规'} ·{' '}
        {process.cyclesUsed} / {process.maxCycles} 轮
      </p>
      <p className="skills-muted">
        健康、修复和合理停止分别记载。停止不会提高修复成功数；工具错误不表示业务检查通过。受托裁决须注明授权及执行者，不能代称用户逐条亲审。
      </p>
      <p>
        原始隐藏检查：{assessment.initialVerification.status} → {assessment.verification.status} ·{' '}
        {assessment.behaviorChanged ? '存在行为修改' : '无行为修改'}
      </p>
      {(!taskOutcome.eligible || !plan.planQualified) && (
        <p role="alert" className="skills-notice skills-notice--warning">
          {[...taskOutcome.reasons, ...plan.planIssues].join('、')}。保留原始检查，不授予缺少证据的分支覆盖。
        </p>
      )}
      {publicVerification.fault && (
        <p className="skills-notice skills-notice--warning">
          第 {publicVerification.fault.checkIndex} 次检查实际返回 registered_check_unavailable；结果已绑定事件{' '}
          {publicVerification.fault.completedEvent}。该次未运行检查程序，没有业务退出码。
        </p>
      )}
      {process.checkpoints.map((checkpoint) => (
        <details key={checkpoint.checkIndex}>
          <summary>
            公开检查 #{checkpoint.checkIndex} · scoped {checkpoint.groupStatus.scoped ?? '未知'} · Dream{' '}
            {checkpoint.groupStatus.dream ?? '未知'} · {checkpoint.cyclesUsed} 轮
          </summary>
          <p>
            送达：{checkpoint.startedEvent ?? '缺失'} → {checkpoint.completedEvent ?? '缺失'}
          </p>
          <p>
            快照 <code>{checkpoint.snapshot}</code>
          </p>
          <p>编辑引用：{checkpoint.editEventRefs.join('、') || '无'}</p>
          <table className="skills-governance-table">
            <thead>
              <tr>
                <th>公开检查引用</th>
                <th>结果</th>
              </tr>
            </thead>
            <tbody>
              {checkpoint.checks.map((check) => (
                <tr key={check.name}>
                  <td>
                    check:{checkpoint.checkIndex}:{check.name}
                  </td>
                  <td>{check.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ))}
      <details>
        <summary>新版任务与计划绑定</summary>
        <pre className="skills-json">
          {JSON.stringify(
            { suiteId: assessment.suiteId, taskOutcome, executionQualification: plan, publicVerification, processVerification: process },
            null,
            2,
          )}
        </pre>
      </details>
    </section>
  )
}
