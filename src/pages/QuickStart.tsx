import { useMemo, useState, type ReactNode } from 'react'
import { PlatformGuideChecklist } from '../components/onboarding/PlatformGuide'
import { Link } from 'react-router-dom'
import { MessageSquare } from 'lucide-react'
import { Icon } from '../components/Icon'
import { AccessIcon } from '../components/AccessIcon'
import { HighlightedCode } from '../components/HighlightedCode'
import { AccessMethodHint } from '../components/AccessMethodHint'
import { CopyButton } from '../components/ui/copy-button'
import { PageHeader } from '../components/ui'
import { Tabs } from '../components/ui/tabs'
import { useLang, useT, useToast, useMeta, useProject } from '../lib/store'
import { copyText } from '../lib/clipboard'
import { api } from '../lib/api'
import { SERVICE_BASE } from '../lib/config'
import { errText } from '../lib/i18n'
import { useAsync } from '../lib/useAsync'
import { HINT_COPY, buildTabs, buildCodeTabs, tabLabel, type Step } from '../lib/quickstart-data'

const Q = { 'data-v-de9e37d2': '' } as const
const S = { 'data-v-82f9e747': '' } as const

const PILL_OUTLINE =
  'rounded-md font-medium inline-flex items-center disabled:cursor-not-allowed aria-disabled:cursor-not-allowed disabled:opacity-75 aria-disabled:opacity-75 transition-colors px-2.5 py-1.5 text-xs gap-1.5 ring ring-inset ring-primary/50 text-primary hover:bg-primary/10 active:bg-primary/10 disabled:bg-transparent aria-disabled:bg-transparent'

/** 「获取 API Key」这一步：当前空间有密钥就直接给复制，没有才退回「去创建密钥」。
 *
 *  列表接口只回前缀掩码，明文按 id 调 keys/copy 取；没有写权限或历史密钥
 *  （库里没留明文）时 canCopy=false，按钮置灰并说明原因。 */
function ApiKeyStep({ step }: { step: Step }) {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const { currentProjectId, ready } = useProject()
  const { data, state, error } = useAsync(() => api.keys.list(currentProjectId, 1, 50), [currentProjectId], { skip: !ready })
  const keys = data?.records ?? []
  const [picked, setPicked] = useState('')
  const current = keys.find((k) => k.id === picked) ?? keys[0]

  const copy = async () => {
    if (!current?.canCopy) return
    try {
      const res = await api.keys.copy(current.id)
      await copyText(res.apiKey)
      toast(t('common.copied'))
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const shell = (children: ReactNode) => (
    <div {...S} className="setup-timeline-step__code-shell rounded-lg overflow-hidden" style={{ border: '1px solid rgb(226, 232, 240)' }}>
      <div {...S} className="code-area">
        <div {...S} className="code-area__body items-center gap-2">
          {children}
        </div>
      </div>
    </div>
  )

  if (state === 'error') {
    return shell(<span className="text-sm text-ink-muted">{error}</span>)
  }
  if (state === 'loading' || !ready) {
    return shell(<span className="text-sm text-ink-muted">…</span>)
  }
  if (!keys.length) {
    return shell(
      step.link ? (
        <Link to={step.link.to} className="api-key-code text-primary hover:underline">
          {step.link.label[lang]}
        </Link>
      ) : null,
    )
  }
  return shell(
    <>
      {keys.length > 1 ? (
        <select
          value={current?.id ?? ''}
          onChange={(e) => setPicked(e.target.value)}
          className="api-key-code bg-transparent text-sm text-ink outline-none"
        >
          {keys.map((k) => (
            <option key={k.id} value={k.id}>
              {k.keyName}（{k.apiKey}）
            </option>
          ))}
        </select>
      ) : (
        <span className="api-key-code text-sm text-ink">
          {current.keyName}（{current.apiKey}）
        </span>
      )}
      <button
        type="button"
        onClick={copy}
        disabled={!current?.canCopy}
        title={current?.canCopy ? undefined : t('apikeys.copyUnavailable')}
        className="ml-auto rounded-md px-2.5 py-1.5 text-xs text-primary ring ring-inset ring-primary/50 hover:bg-primary/10 disabled:opacity-50 disabled:hover:bg-transparent"
      >
        {t('apikeys.copy')}
      </button>
    </>,
  )
}

function CodeBlock({ step, language }: { step: Step; language?: string }) {
  const t = useT()
  const { lang } = useLang()
  if (step.kind === 'apikey') {
    return <ApiKeyStep step={step} />
  }
  // 「获取 API Key」这类步骤在控制台里有对应页面，渲染成一个跳转按钮：
  // 放一段代码（哪怕是占位符）也帮不上忙，人需要的是那把钥匙本身。
  if (step.link) {
    return (
      <div {...S} className="setup-timeline-step__code-shell rounded-lg overflow-hidden" style={{ border: '1px solid rgb(226, 232, 240)' }}>
        <div {...S} className="code-area">
          <div {...S} className="code-area__body items-center">
            <Link to={step.link.to} className="api-key-code text-primary hover:underline">
              {step.link.label[lang]}
            </Link>
          </div>
        </div>
      </div>
    )
  }
  return (
    <div {...S} className="setup-timeline-step__code-shell rounded-lg overflow-hidden" style={{ border: '1px solid rgb(226, 232, 240)' }}>
      <div {...S} className={`code-area${step.multiline ? ' code-area--multiline' : ''}`}>
        <div {...S} className="code-area__body">
          {step.multiline ? (
            <pre {...S} className="normal-code">
              <HighlightedCode text={step.code} language={language} />
            </pre>
          ) : (
            <HighlightedCode {...S} text={step.code} language={language} className="api-key-code" />
          )}
        </div>
        <CopyButton text={step.code} label={t('quickstart.copyStep')} className="absolute right-2 top-2" />
      </div>
    </div>
  )
}

/** 一个步骤卡片。标题那行是折叠开关，折叠时卡片多一个 --collapsed 类、箭头换成向下 */
function TimelineStep({ step, lang, language }: { step: Step; lang: 'zh' | 'en'; language?: string }) {
  const [collapsed, setCollapsed] = useState(false)
  const bodyId = `setup-step-body-${step.n}`
  const descId = `setup-step-desc-${step.n}`
  return (
    <div {...S} className="setup-timeline-step">
      <div
        {...S}
        className={`setup-timeline-step__card${step.desc ? ' setup-timeline-step__card--with-desc' : ''}${
          collapsed ? ' setup-timeline-step__card--collapsed' : ''
        }`}
      >
        <div {...S} className="step-index-badge">
          <span {...S} className="step-index-badge__num">
            {step.n}
          </span>
        </div>
        <button
          {...S}
          type="button"
          className="step-fold-header min-h-6 min-w-0"
          aria-expanded={!collapsed}
          aria-controls={bodyId}
          aria-describedby={step.desc ? descId : undefined}
          onClick={() => setCollapsed((c) => !c)}
        >
          <h3 {...S} className="step-fold-title text-sm font-semibold text-ink mb-0 min-w-0 text-left">
            {step.title[lang]}
          </h3>
          <Icon {...S} name={collapsed ? 'i-lucide:chevron-down' : 'i-lucide:chevron-up'} className="step-fold-chevron shrink-0" />
        </button>
        {step.desc && (
          <span {...S} id={descId} className="step-fold-desc text-xs text-ink-muted leading-relaxed whitespace-pre-line">
            {step.desc[lang]}
          </span>
        )}
        <div
          {...S}
          id={bodyId}
          className={`step-fold-body${step.desc ? ' step-fold-body--after-desc' : ''}`}
          style={collapsed ? { display: 'none' } : undefined}
        >
          <CodeBlock step={step} language={language} />
        </div>
      </div>
    </div>
  )
}

export function QuickStart() {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const [selectedTabs, setSelectedTabs] = useState({ agent: 'hermes', code: 'python-http' })
  const [accessGroup, setAccessGroup] = useState<'agent' | 'code'>('agent')
  const [promptOpen, setPromptOpen] = useState(false)
  const [copiedPrompt, setCopiedPrompt] = useState('')

  // ⚠️ 这里原来会把当前项目的第一把密钥查出来、嵌进所有示例代码里。
  //    现在示例里只有占位符，页面不需要也不应该拿到任何密钥（见 quickstart-data.ts）。
  const { tools } = useMeta()

  const tabs = useMemo(() => (accessGroup === 'agent' ? buildTabs({ tools }) : buildCodeTabs()), [tools, accessGroup])
  const tab = tabs.find((x) => x.id === selectedTabs[accessGroup]) ?? tabs[0]
  const integrationPrompt = t('quickstart.agentCode.prompt')(
    tabLabel(tab, lang),
    tab.steps
      .filter((step) => step.kind !== 'apikey')
      .map((step) => `${step.title[lang]}\n${step.code}`)
      .join('\n\n'),
  )

  /** 「选择哪种接入方式？」浮层里的行。
   *
   *  ⚠️ 方式名取自**页签自己的 label**，行数也就是页签数——不在这里另写一份。
   *  这里原来写死了 `Plugin / CLI + Skill / MCP` 三行，而页签里一个都没有。 */
  const hintRows = useMemo(
    () => tabs.map((x) => [tabLabel(x, lang), t(HINT_COPY[x.id].scene), t(HINT_COPY[x.id].feature)] as [string, string, string]),
    [tabs, lang, t],
  )

  if (!SERVICE_BASE)
    return (
      <div className="flex-1">
        <PageHeader title={t('quickstart.title')} desc={t('quickstart.subtitle')} />
        <p role="status">{t('quickstart.addressUnavailable')}</p>
        <Link to="/cn/chat" className="console-chat-shortcut">
          {t('quickstart.tryChat')}
        </Link>
      </div>
    )

  return (
    <>
      <div {...Q} className="quickstart-focus-column">
        <PageHeader
          title={t('quickstart.title')}
          desc={t('quickstart.subtitle')}
          actions={
            <Link to="/cn/chat" className="console-chat-shortcut">
              <MessageSquare size={16} />
              {t('quickstart.tryChat')}
            </Link>
          }
        />

        <PlatformGuideChecklist />

        <div
          className="quickstart-access-choices"
          data-guide="quickstart"
          data-stage-radius="12"
          role="group"
          aria-label={t('quickstart.chooseAccess')}
        >
          <button type="button" aria-pressed={accessGroup === 'agent'} onClick={() => setAccessGroup('agent')}>
            <span className="quickstart-access-icon" aria-hidden="true">
              <AccessIcon name="/logo.svg?v=hugegraph" />
            </span>
            <span>
              <strong>{t('quickstart.agentAccess')}</strong>
              <small>Plugin / MCP</small>
            </span>
          </button>
          <button type="button" aria-pressed={accessGroup === 'code'} onClick={() => setAccessGroup('code')}>
            <span className="quickstart-access-icon" aria-hidden="true">
              <AccessIcon name="i-lucide:code" />
            </span>
            <span>
              <strong>{t('quickstart.codeAccess')}</strong>
              <small>Python / TypeScript / cURL</small>
            </span>
          </button>
        </div>

        {accessGroup === 'code' && (
          <div {...Q} className="quick-access-block quickstart-code-assistant">
            <div {...Q} className="quick-access-header">
              <h4 {...Q} className="quick-access-title">
                {t('quickstart.agentCode.title')}
              </h4>
              <p {...Q} className="quick-access-desc">
                {t('quickstart.agentCode.desc')}
              </p>
            </div>
            <div {...Q} className="quick-access-actions">
              <button
                type="button"
                className={PILL_OUTLINE}
                onClick={async () => {
                  try {
                    await copyText(integrationPrompt)
                    setCopiedPrompt(integrationPrompt)
                  } catch {
                    toast(t('common.copyFailed'), 'err')
                  }
                }}
              >
                <Icon name={copiedPrompt === integrationPrompt ? 'i-lucide:check' : 'i-lucide:copy'} className="shrink-0 size-4" />
                {t(copiedPrompt === integrationPrompt ? 'common.copied' : 'quickstart.copyPrompt')}
              </button>
              <button
                type="button"
                className={PILL_OUTLINE}
                aria-expanded={promptOpen}
                aria-controls="code-integration-prompt"
                onClick={() => setPromptOpen((previous) => !previous)}
              >
                {t(promptOpen ? 'quickstart.agentCode.hide' : 'quickstart.agentCode.view')}
                <Icon name={promptOpen ? 'i-lucide:chevron-up' : 'i-lucide:chevron-down'} className="shrink-0 size-4" />
              </button>
            </div>
            {promptOpen && (
              <div {...Q} className="quick-access-prompt-preview" id="code-integration-prompt">
                <pre {...Q}>{integrationPrompt}</pre>
              </div>
            )}
          </div>
        )}

        <div {...Q} className="quickstart-subtabs-row">
          <Tabs
            label={t('quickstart.hint')}
            value={tab.id}
            onChange={(id) => setSelectedTabs((previous) => ({ ...previous, [accessGroup]: id }))}
            items={tabs.map((x) => ({
              key: x.id,
              label: (
                <span className="console-access-tab">
                  <AccessIcon name={x.icon} className="size-4 shrink-0" />
                  {tabLabel(x, lang)}
                </span>
              ),
            }))}
          />
          <AccessMethodHint text={t('quickstart.hint')} rows={hintRows} />
        </div>
      </div>

      <div {...Q} className="setup-steps-block">
        {tab.prompt ? (
          <div {...Q} className="quick-access-block quick-access-block--static">
            <div {...Q} className="quick-access-header">
              <h4 {...Q} className="quick-access-title">
                {t('quickstart.copyPrompt')}
              </h4>
              <p {...Q} className="quick-access-desc">
                {t('quickstart.subtitle')}
              </p>
            </div>
            <div {...Q} className="quick-access-actions">
              <button
                type="button"
                className={PILL_OUTLINE}
                onClick={async () => {
                  try {
                    await copyText(tab.prompt![lang])
                    toast(t('common.copied'))
                  } catch {
                    toast(t('common.copyFailed'), 'err')
                  }
                }}
              >
                <Icon name="i-lucide:copy" className="shrink-0 size-4" />
                {t('quickstart.copyPrompt')}
              </button>
            </div>
            <div {...Q} className="quick-access-prompt-preview">
              <pre {...Q}>{tab.prompt[lang]}</pre>
            </div>
          </div>
        ) : (
          <>
            <div {...Q} className="setup-section-head">
              <h3 {...Q} className="setup-section-head__title">
                {tab.head[lang]}
              </h3>
              <div {...Q} className="setup-section-head__actions">
                <Link {...Q} to={tab.docs} className="setup-docs-link">
                  {t('quickstart.docs')}
                  <Icon {...Q} name="i-lucide:arrow-up-right" className="setup-docs-link__icon" />
                </Link>
              </div>
            </div>

            {tab.note ? (
              <p {...S} className="text-sm text-ink-muted leading-relaxed">
                {tab.note[lang]}
              </p>
            ) : (
              <div {...S} className="flex flex-col space-y-8 mb-6">
                <div {...S} className="flex-1 w-full">
                  <div {...S}>
                    <div {...S} className="setup-timeline-steps setup-timeline-steps--quickstart">
                      {tab.steps.map((step) => (
                        <TimelineStep
                          key={step.n}
                          step={step}
                          lang={lang}
                          language={
                            tab.id === 'python-http'
                              ? 'python'
                              : tab.id === 'typescript-http'
                                ? 'typescript'
                                : step.code.trimStart().startsWith('{')
                                  ? 'json'
                                  : 'bash'
                          }
                        />
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </>
  )
}
