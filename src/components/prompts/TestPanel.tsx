/** 试跑面板：按 `input_kind` 渲染输入，草稿与默认模板可并排对比。
 *
 *  结果必须说明「会不会整段存原文」——真实的对话抽取解析失败时不会报错，而是把模型原文
 *  存成一条 UserMemory，所以 `fallbackTriggered` 是这里最显眼的信息。
 *
 *  ⚠️ 输入形状以 `detail.inputSchema` 为准（`memories` 是 `[{id, memory}]`）。任何形状
 *  不认识都退回 JSON 编辑器，取值一律先归一，渲染和事件里不直接假设某个字段的类型。
 */

import { useEffect, useRef, useState } from 'react'
import { ApiError, api, retryAfterSec, type PromptDetail, type PromptRun } from '../../lib/api'
import { errText } from '../../lib/i18n'
import type { TKey } from '../../lib/i18n'
import {
  buildInput,
  compactCardLayout,
  editorKind,
  flagTextKey,
  isRepeatClick,
  makeInputState,
  sampleMatchesLang,
  schemaViolation,
  structuredTable,
  tableColumnWidths,
  versionLabel,
  type CompareLayout,
  type InputState,
} from '../../lib/prompts'
import { useLang, useStr, useT } from '../../lib/store'
import { Badge } from '../ui/badge'
import { Button } from '../ui/button'
import { Tabs } from '../ui/tabs'
import { Icon } from '../Icon'

const ROLE_KEYS: Record<string, string> = {
  user: 'prompts.test.role.user',
  assistant: 'prompts.test.role.assistant',
  system: 'prompts.test.role.system',
}

const VIEW_KEY: Record<'structured' | 'raw' | 'contract', string> = {
  structured: 'prompts.test.view.structured',
  raw: 'prompts.test.view.raw',
  contract: 'prompts.test.view.contract',
}

const SLOT_SOURCE_KEY: Record<string, string> = {
  draft: 'prompts.test.slotSource.draft',
  project: 'prompts.test.slotSource.project',
  default: 'prompts.test.slotSource.default',
}

export function TestPanel({
  detail,
  lang,
  draft,
  projectId,
  layout,
  onLayoutChange,
  onRunResult,
  onClose,
}: {
  detail: PromptDetail
  lang: string
  draft: string
  projectId: string
  layout: CompareLayout
  onLayoutChange: (layout: CompareLayout) => void
  /** 最近一次试跑的结果；保存对话框用它判断要不要勾选确认 */
  onRunResult: (r: { draft: string; ok: boolean } | null) => void
  onClose: () => void
}) {
  const t = useT()
  const str = useStr()
  const { lang: uiLang } = useLang()
  const declared = editorKind(detail.inputKind)

  // 当前语言的样例优先；没有语言字段的老样例再按 id 前缀兜底
  const langSamples = detail.sampleInputs.filter((s) => (s.lang ? s.lang === lang : sampleMatchesLang(s.id, lang)))
  const samples = langSamples.length ? langSamples : detail.sampleInputs
  const firstSample = samples[0]

  const [sampleId, setSampleId] = useState(firstSample?.id ?? '')
  const [input, setInput] = useState<InputState>(() => makeInputState(declared, firstSample?.input))

  const [compare, setCompare] = useState(false)
  const [runs, setRuns] = useState<PromptRun[] | null>(null)
  const [ranDraft, setRanDraft] = useState('')
  const [view, setView] = useState<'structured' | 'raw' | 'contract'>('structured')
  const [running, setRunning] = useState(false)
  const [testError, setTestError] = useState('')
  const [retryIn, setRetryIn] = useState(0)
  const lastRunAt = useRef(0)

  // 限流倒计时：每秒减一，到 0 之前运行按钮保持禁用
  useEffect(() => {
    if (retryIn <= 0) return
    const id = setTimeout(() => setRetryIn((n) => Math.max(0, n - 1)), 1000)
    return () => clearTimeout(id)
  }, [retryIn])

  const applySample = (id: string) => {
    const sample = samples.find((s) => s.id === id) ?? detail.sampleInputs.find((s) => s.id === id)
    setSampleId(id)
    if (sample) setInput(makeInputState(declared, sample.input))
  }

  const runNow = async () => {
    if (running || retryIn > 0) return
    const now = Date.now()
    // 800 毫秒内的重复点击直接忽略（体验层，服务端还有自己的令牌桶）
    if (isRepeatClick(lastRunAt.current, now)) return
    lastRunAt.current = now

    try {
      const built = buildInput(input)
      if (!built.ok) {
        setTestError(built.reason === 'json' ? t('prompts.test.jsonInvalid') : t('prompts.test.inputRequired'))
        return
      }
      const violation = schemaViolation(detail.inputSchema, built.input)
      if (violation) {
        setTestError(t('prompts.test.schemaInvalid')(violation))
        return
      }

      setRunning(true)
      setTestError('')
      const res = await api.prompts.test({
        projectId,
        key: detail.key,
        lang,
        body: draft,
        input: built.input,
        compareDefault: compare,
      })
      setRuns(res.runs)
      setRanDraft(draft)
      const draftRun = res.runs.find((r) => r.label === 'draft')
      onRunResult({
        draft,
        ok: Boolean(draftRun?.ok && draftRun.parseOk && draftRun.contract.ok && !draftRun.fallbackTriggered),
      })
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        // 限流不代表上一轮结果失效：保留结果，只把倒计时和原因显示出来
        setRetryIn(retryAfterSec(e) ?? 10)
      } else {
        setRuns(null)
        onRunResult(null)
      }
      setTestError(errText(uiLang, e))
    } finally {
      setRunning(false)
    }
  }

  const patch = (part: Partial<InputState>) => setInput((cur) => ({ ...cur, ...part }))

  const inputEditor = () => {
    if (input.mode === 'conversation') {
      return (
        <div className="space-y-2">
          {input.messages.map((m, i) => (
            <div key={i} className="flex items-start gap-1.5">
              <select
                className="studio-select shrink-0"
                value={m.role}
                aria-label={t('prompts.test.role')}
                onChange={(e) => patch({ messages: input.messages.map((x, idx) => (idx === i ? { ...x, role: e.target.value } : x)) })}
              >
                {['user', 'assistant', 'system'].map((r) => (
                  <option key={r} value={r}>
                    {str(ROLE_KEYS[r] as TKey)}
                  </option>
                ))}
              </select>
              <textarea
                className="studio-test-message"
                rows={2}
                value={m.content}
                aria-label={`${t('prompts.test.message')} ${i + 1}`}
                placeholder={t('prompts.test.messagePlaceholder')}
                onChange={(e) => patch({ messages: input.messages.map((x, idx) => (idx === i ? { ...x, content: e.target.value } : x)) })}
              />
              <button
                type="button"
                className="studio-icon-btn"
                aria-label={t('prompts.test.remove')}
                onClick={() =>
                  patch({ messages: input.messages.length > 1 ? input.messages.filter((_, idx) => idx !== i) : input.messages })
                }
              >
                <Icon name="i-lucide:trash-2" className="size-3.5" />
              </button>
            </div>
          ))}
          <Button size="sm" variant="ghost" onClick={() => patch({ messages: [...input.messages, { role: 'user', content: '' }] })}>
            <Icon name="i-lucide:plus" className="size-3.5" />
            {t('prompts.test.addMessage')}
          </Button>
        </div>
      )
    }

    if (input.mode === 'text') {
      return (
        <textarea
          className="studio-test-textarea"
          rows={8}
          value={input.text}
          aria-label={t('prompts.test.input')}
          placeholder={t('prompts.test.textPlaceholder')}
          onChange={(e) => patch({ text: e.target.value })}
        />
      )
    }

    if (input.mode === 'qa_pair') {
      return (
        <textarea
          className="studio-test-textarea"
          rows={7}
          value={input.qaPair}
          aria-label={t('prompts.test.input')}
          placeholder={t('prompts.test.qaPlaceholder')}
          onChange={(e) => patch({ qaPair: e.target.value })}
        />
      )
    }

    if (input.mode === 'memories') {
      return (
        <div className="space-y-2">
          {input.memories.map((m, i) => (
            <div key={i} className="flex items-start gap-1.5">
              <input
                className="studio-input studio-input--id shrink-0"
                value={m.id}
                aria-label={`${t('prompts.test.memoryId')} ${i + 1}`}
                placeholder="m1"
                onChange={(e) => patch({ memories: input.memories.map((x, idx) => (idx === i ? { ...x, id: e.target.value } : x)) })}
              />
              <textarea
                className="studio-test-message"
                rows={2}
                value={m.memory}
                aria-label={`${t('prompts.test.memory')} ${i + 1}`}
                placeholder={t('prompts.test.memoryPlaceholder')}
                onChange={(e) => patch({ memories: input.memories.map((x, idx) => (idx === i ? { ...x, memory: e.target.value } : x)) })}
              />
              <button
                type="button"
                className="studio-icon-btn"
                aria-label={t('prompts.test.remove')}
                onClick={() =>
                  patch({ memories: input.memories.length > 1 ? input.memories.filter((_, idx) => idx !== i) : input.memories })
                }
              >
                <Icon name="i-lucide:trash-2" className="size-3.5" />
              </button>
            </div>
          ))}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => patch({ memories: [...input.memories, { id: String(input.memories.length + 1), memory: '' }] })}
          >
            <Icon name="i-lucide:plus" className="size-3.5" />
            {t('prompts.test.addMemory')}
          </Button>
        </div>
      )
    }

    return (
      <div className="space-y-1.5">
        <textarea
          className="studio-test-textarea font-mono"
          rows={9}
          value={input.jsonText}
          aria-label={t('prompts.test.input')}
          onChange={(e) => patch({ jsonText: e.target.value })}
          spellCheck={false}
        />
        {input.mode !== declared && <p className="text-[11px] leading-5 text-ink-faint">{t('prompts.test.jsonFallback')}</p>}
      </div>
    )
  }

  const draftNotUsed = runs?.flatMap((r) => r.warnings ?? []).find((w) => w.code === 'draft_not_used')

  const runTitle = (run: PromptRun) => t(run.label === 'draft' ? 'prompts.test.result.draft' : 'prompts.test.result.default')

  const runBadge = (run: PromptRun) =>
    run.ok ? (
      <Badge variant={run.parseOk && run.contract.ok && !run.fallbackTriggered ? 'success' : 'warning'}>
        {run.parseOk && run.contract.ok && !run.fallbackTriggered ? t('prompts.test.contract.ok') : t('prompts.test.contract.failed')}
      </Badge>
    ) : (
      <Badge variant="destructive">{t('prompts.test.failed')}</Badge>
    )

  const slotList = (run: PromptRun) =>
    run.usedSlots && run.usedSlots.length > 0 ? (
      <ul className="space-y-1">
        {run.usedSlots.map((slot) => (
          <li key={`${slot.key}:${slot.lang}`} className="flex flex-wrap items-center gap-1.5 text-[11px] text-ink-muted">
            <code className="studio-chip">{slot.key}</code>
            <span>{slot.lang}</span>
            <span>{str((SLOT_SOURCE_KEY[slot.source] ?? 'prompts.test.slotSource.draft') as TKey)}</span>
            {slot.versionNo ? <span>{versionLabel(slot.versionNo, 0)}</span> : null}
          </li>
        ))}
      </ul>
    ) : (
      <span className="text-[11px] text-ink-faint">—</span>
    )

  const renderCards = (table: NonNullable<ReturnType<typeof structuredTable>>) => (
    <div className="studio-result-cards">
      {table.rows.map((row, i) => {
        const card = compactCardLayout(table.columns, row)
        return (
          <article key={i} className="studio-result-card">
            <p className="studio-result-card__value">{row[card.primary] || '—'}</p>
            {card.meta.length > 0 && (
              <p className="studio-result-card__meta">{card.meta.map((c) => `${c}: ${row[c] || '—'}`).join(' · ')}</p>
            )}
            {card.details.length > 0 && (
              <details className="studio-result-card__details">
                <summary>{t('prompts.test.cardDetails')}</summary>
                <dl>
                  {card.details.map((c) => (
                    <div key={c}>
                      <dt>{c}</dt>
                      <dd>{row[c] || '—'}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            )}
          </article>
        )
      })}
    </div>
  )

  const renderRun = (run: PromptRun, compact = false) => {
    const table = view === 'structured' ? structuredTable(run.output, detail.outputContract) : null
    const widths = table ? tableColumnWidths(table.columns.length) : []
    return (
      <section key={run.label} className="studio-result">
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="text-xs font-semibold text-ink">{runTitle(run)}</span>
            {runBadge(run)}
          </div>
          <span className="text-[11px] text-ink-faint">{run.model}</span>
        </header>

        {run.fallbackTriggered && (
          <div className="studio-alert studio-alert--warning" role="alert">
            <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
            <div>
              <div className="text-xs font-medium">{t('prompts.test.fallback.title')}</div>
              <p className="mt-0.5 text-xs leading-5">{t('prompts.test.fallback.desc')}</p>
            </div>
          </div>
        )}

        {!run.ok && run.error && (
          <div className="studio-alert studio-alert--danger" role="alert">
            <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
            <div>
              <div className="text-xs font-medium">{run.error.message}</div>
              <p className="mt-0.5 font-mono text-[11px] opacity-80">{run.error.code}</p>
              <p className="mt-1 text-[11px] leading-5">{t('prompts.test.errorNextStep')}</p>
            </div>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-auto p-3">
          {view === 'structured' &&
            (table ? (
              compact ? (
                renderCards(table)
              ) : (
                <table className="studio-result-table">
                  <colgroup>
                    {widths.map((w, i) => (
                      <col key={i} style={{ width: w }} />
                    ))}
                  </colgroup>
                  <thead>
                    <tr>
                      {table.columns.map((c) => (
                        <th key={c}>{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {table.rows.map((row, i) => (
                      <tr key={i}>
                        {table.columns.map((c) => (
                          <td key={c}>{row[c] ?? '—'}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )
            ) : (
              <pre className="studio-pre">
                {run.output === null
                  ? run.fallbackTriggered
                    ? t('prompts.test.fallback.rawHint')
                    : run.ok
                      ? t('prompts.test.emptyOutput')
                      : '—'
                  : JSON.stringify(run.output, null, 2)}
              </pre>
            ))}
          {view === 'raw' && (
            <div className="space-y-2">
              <pre className="studio-pre">
                {run.rawOutput || (run.ok ? t('prompts.test.emptyOutput') : t('prompts.test.errorNextStep'))}
              </pre>
              {run.truncated && <p className="text-[11px] text-ink-faint">{t('prompts.test.truncated')}</p>}
            </div>
          )}
          {view === 'contract' && (
            <div className="space-y-3">
              <dl className="studio-kv">
                <dt>{t('prompts.test.contractTitle')}</dt>
                <dd>
                  {run.contract.ok ? t('prompts.test.contract.ok') : t('prompts.test.contract.missing')(run.contract.missing.join(', '))}
                </dd>
                <dt>{t('prompts.test.parseOk')}</dt>
                <dd>{t(flagTextKey(run.parseOk))}</dd>
                <dt>{t('prompts.test.fallbackShort')}</dt>
                <dd>{t(flagTextKey(run.fallbackTriggered))}</dd>
                <dt>{t('prompts.test.latency')}</dt>
                <dd>{run.latencyMs} ms</dd>
                <dt>{t('prompts.test.model')}</dt>
                <dd>{run.model}</dd>
                <dt>{t('prompts.test.usage')}</dt>
                <dd>{t('prompts.test.usageValue')(run.usage?.promptTokens ?? 0, run.usage?.completionTokens ?? 0)}</dd>
              </dl>

              {run.usedSlots && run.usedSlots.length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-medium text-ink-secondary">{t('prompts.test.usedSlots')}</p>
                  {slotList(run)}
                </div>
              )}

              {run.warnings && run.warnings.length > 0 && (
                <ul className="space-y-1">
                  {run.warnings.map((w) => (
                    <li key={w.code + w.message} className="text-[11px] leading-5 text-warning">
                      {w.message}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </section>
    )
  }

  /** 并排看校验信息：同一行放两侧的值，避免两列各自换行后错位 */
  const renderContractCompare = (list: PromptRun[]) => (
    <section className="studio-result studio-contract">
      <div className="min-h-0 flex-1 overflow-auto p-3">
        <table className="studio-compare-table">
          <thead>
            <tr>
              <th aria-label={t('prompts.test.resultTitle')} />
              {list.map((run) => (
                <th key={run.label}>
                  <span className="mr-1.5 font-semibold text-ink">{runTitle(run)}</span>
                  {runBadge(run)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <th>{t('prompts.test.contractTitle')}</th>
              {list.map((run) => (
                <td key={run.label}>
                  {run.ok ? (
                    run.contract.ok ? (
                      t('prompts.test.contract.ok')
                    ) : (
                      t('prompts.test.contract.missing')(run.contract.missing.join(', '))
                    )
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
              ))}
            </tr>
            <tr>
              <th>{t('prompts.test.parseOk')}</th>
              {list.map((run) => (
                <td key={run.label}>{run.ok ? t(flagTextKey(run.parseOk)) : <span className="text-ink-faint">—</span>}</td>
              ))}
            </tr>
            <tr>
              <th>{t('prompts.test.fallbackShort')}</th>
              {list.map((run) => (
                <td key={run.label}>{run.ok ? t(flagTextKey(run.fallbackTriggered)) : <span className="text-ink-faint">—</span>}</td>
              ))}
            </tr>
            <tr>
              <th>{t('prompts.test.latency')}</th>
              {list.map((run) => (
                <td key={run.label}>{run.latencyMs} ms</td>
              ))}
            </tr>
            <tr>
              <th>{t('prompts.test.model')}</th>
              {list.map((run) => (
                <td key={run.label}>{run.model}</td>
              ))}
            </tr>
            <tr>
              <th>{t('prompts.test.usage')}</th>
              {list.map((run) => (
                <td key={run.label}>
                  {run.ok ? (
                    t('prompts.test.usageValue')(run.usage?.promptTokens ?? 0, run.usage?.completionTokens ?? 0)
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
              ))}
            </tr>
            <tr>
              <th>{t('prompts.test.usedSlots')}</th>
              {list.map((run) => (
                <td key={run.label}>{slotList(run)}</td>
              ))}
            </tr>
            <tr>
              <th>{t('prompts.test.warnings')}</th>
              {list.map((run) => (
                <td key={run.label}>
                  {run.warnings && run.warnings.length > 0 ? (
                    <ul className="space-y-1">
                      {run.warnings.map((w) => (
                        <li key={w.code + w.message} className="text-warning">
                          {w.message}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
              ))}
            </tr>
            {list.some((run) => !run.ok) && (
              <tr>
                <th>{t('prompts.test.failed')}</th>
                {list.map((run) => (
                  <td key={run.label}>
                    {run.ok ? (
                      <span className="text-ink-faint">—</span>
                    ) : (
                      <>
                        <div className="font-medium text-danger">{run.error?.message}</div>
                        <div className="mt-0.5 text-[11px] leading-5 text-ink-muted">{t('prompts.test.errorNextStep')}</div>
                      </>
                    )}
                  </td>
                ))}
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )

  const split = Boolean(runs && runs.length > 1 && layout === 'split')
  const columns = split ? 'grid-cols-2' : 'grid-cols-1'

  return (
    <section className="studio-test-panel studio-test-panel--expanded" aria-label={t('prompts.test.title')}>
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2">
        <div className="flex items-center gap-2">
          <h3 className="text-xs font-semibold text-ink">{t('prompts.test.title')}</h3>
          <label className="flex items-center gap-1.5 text-xs text-ink-muted">
            <input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} className="studio-checkbox" />
            {t('prompts.test.compare')}
          </label>
          <span className="text-[10px] text-ink-faint">{t('prompts.test.compareHint')}</span>
        </div>
        <div className="flex items-center gap-2">
          {samples.length > 0 && (
            <>
              <select
                className="studio-select"
                value={sampleId}
                aria-label={t('prompts.test.sample')}
                onChange={(e) => applySample(e.target.value)}
              >
                {samples.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.title[lang] ?? s.title.zh ?? s.id}
                  </option>
                ))}
              </select>
              <Button size="sm" variant="ghost" onClick={() => applySample(sampleId)}>
                {t('prompts.test.fillSample')}
              </Button>
            </>
          )}
          <Button size="sm" variant="primary" loading={running} disabled={retryIn > 0} onClick={runNow}>
            <Icon name="i-lucide:play" className="size-3.5" />
            {retryIn > 0 ? t('prompts.test.retryIn')(retryIn) : t('prompts.test.run')}
          </Button>
          <button type="button" className="studio-icon-btn" aria-label={t('prompts.test.close')} onClick={onClose}>
            <Icon name="i-lucide:x" className="size-4" />
          </button>
        </div>
      </header>

      {draftNotUsed && (
        <div className="studio-alert studio-alert--warning mx-3 mt-2" role="alert">
          <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
          <span className="text-xs leading-5">{draftNotUsed.message}</span>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <div className="w-72 shrink-0 overflow-y-auto border-r border-line p-3">
          <p className="mb-2 text-xs font-medium text-ink-secondary">{t('prompts.test.input')}</p>
          {inputEditor()}
          {testError && (
            <p className="mt-2 text-xs leading-5 text-warning" role="alert">
              {testError}
            </p>
          )}
        </div>

        <div className="flex min-w-0 flex-1 flex-col">
          {runs && ranDraft !== draft && (
            <p className="mx-3 mt-2 rounded-md bg-warning-soft px-2.5 py-1.5 text-[11px] leading-5 text-warning" role="status">
              {t('prompts.test.staleResult')}
            </p>
          )}
          <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
            <Tabs
              items={[
                { key: 'structured', label: str(VIEW_KEY.structured as TKey) },
                { key: 'raw', label: str(VIEW_KEY.raw as TKey) },
                { key: 'contract', label: str(VIEW_KEY.contract as TKey) },
              ]}
              value={view}
              onChange={(k) => setView(k as 'structured' | 'raw' | 'contract')}
              label={t('prompts.test.resultTitle')}
            />
            <div className="flex items-center gap-2">
              {runs && runs.length > 1 && (
                <Tabs
                  items={[
                    { key: 'split', label: t('prompts.diff.mode.split') },
                    { key: 'stacked', label: t('prompts.diff.mode.unified') },
                  ]}
                  value={layout}
                  onChange={(k) => onLayoutChange(k as CompareLayout)}
                  label={t('prompts.test.resultTitle')}
                />
              )}
              <span className="text-[11px] text-ink-faint">{runs ? t('prompts.test.runs')(runs.length) : t('prompts.test.neverRun')}</span>
            </div>
          </div>
          {runs ? (
            split && view === 'contract' ? (
              renderContractCompare(runs)
            ) : (
              <div className={`grid min-h-0 flex-1 gap-3 overflow-y-auto px-3 pb-3 ${columns}`}>
                {runs.map((run) => renderRun(run, split))}
              </div>
            )
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 pb-6 text-center">
              <Icon name="i-lucide:play" className="size-5 text-ink-faint" />
              <p className="text-xs text-ink-faint">{t('prompts.test.empty')}</p>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
