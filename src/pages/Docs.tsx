import { HighlightedCode } from '../components/HighlightedCode'
import { SERVICE_BASE } from '../lib/config'
import { useMemo } from 'react'
import { useT, useLang } from '../lib/store'
import { Panel, PageHeader } from '../components/ui'
import { CopyButton } from '../components/ui/copy-button'
import { Icon } from '../components/Icon'
import { useAsync } from '../lib/useAsync'
import { api } from '../lib/api'
import { buildDocs, type Bi, type DocBlock, type DocSection } from '../lib/docs-content'
import type { Lang } from '../lib/i18n'

/** 状态灯多久自己刷新一次。`/api/meta` 同时是后端的探针端点（日志快照 + 密钥状态 + 上游探测），
 *  不适合高频刷；一分钟一次足够让人看见变化。 */
const HEALTH_POLL_MS = 60_000

/** 上游 `/health` 的应答形如 `{"status":"healthy","service":"memos","version":"1.0.1"}`；
 *  `null` 表示后端没连上上游。字段缺失时按「取不到状态」算不健康。 */
function upstreamOk(health: Record<string, unknown> | null): boolean {
  const status = health?.status
  return typeof status === 'string' && ['ok', 'healthy', 'up'].includes(status.toLowerCase())
}

type StatusTone = 'ok' | 'bad' | 'idle'

const DOT_CLASS: Record<StatusTone, string> = {
  ok: 'bg-success',
  bad: 'bg-danger',
  idle: 'bg-ink-faint',
}

/** 状态灯。`pulse` 只在首次探测时用：一颗会闪的灰点比一行「加载中」文字更像「正在测」。 */
function StatusDot({ tone, pulse = false }: { tone: StatusTone; pulse?: boolean }) {
  return (
    <span className="relative inline-flex size-2.5 shrink-0">
      {pulse && <span className={`absolute inset-0 rounded-full ${DOT_CLASS[tone]} opacity-60 animate-ping`} />}
      <span className={`relative inline-flex size-2.5 rounded-full ${DOT_CLASS[tone]}`} />
    </span>
  )
}

/** 服务状态行：一盏灯代替原来的健康检查地址。
 *
 *  ⚠️ 不给业务方 URL 是有原因的：对外域名的 `/healthz` 是前置零信任网关自己的端点，
 *  恒返回 `{"status":"ok"}`，与记忆服务活不活无关；真正指向记忆服务的路径又会 302 到登录页。
 *  所以这里只认后端探测出来的结论，不把地址摆到页面上。 */
function ServiceStatus() {
  const t = useT()
  const { data, state, error, reload } = useAsync(() => api.meta(), [], {
    key: 'docs.health',
    refetchInterval: HEALTH_POLL_MS,
  })

  // 轮询失败时 TanStack Query 会保留上一次的数据，但那时候「上一次正常」已经不成立了：
  // 有 error 就按「未知」报，别拿旧数据给一颗绿点。
  const view = state === 'error' ? 'unknown' : !data ? 'checking' : upstreamOk(data.health) ? 'ok' : 'bad'
  const tone: StatusTone = view === 'ok' ? 'ok' : view === 'bad' ? 'bad' : 'idle'
  const label =
    view === 'checking'
      ? t('docs.statusChecking')
      : view === 'ok'
        ? t('docs.statusOk')
        : view === 'bad'
          ? t('docs.statusBad')
          : t('docs.statusUnknown')

  const hint =
    state === 'error'
      ? error
      : data
        ? `${t('docs.upstream')}：${upstreamOk(data.health) ? t('docs.checkOk') : t('docs.checkDown')}`
        : undefined

  return (
    <div className="flex items-center gap-3 py-3">
      <span className="w-40 shrink-0 text-xs font-medium text-ink-muted">{t('docs.status')}</span>
      <span className="inline-flex items-center gap-2 text-xs font-medium text-ink" title={hint}>
        <StatusDot tone={tone} pulse={view === 'checking'} />
        {label}
      </span>
      <span className="ml-auto flex items-center gap-2 text-[11px] text-ink-muted">
        <button
          type="button"
          onClick={reload}
          title={t('docs.refresh')}
          aria-label={t('docs.refresh')}
          className="inline-flex size-6 items-center justify-center rounded-md text-ink-faint hover:bg-surface-hover hover:text-ink-secondary transition-colors cursor-pointer"
        >
          <Icon name="i-lucide:rotate-ccw" className="size-3.5" />
        </button>
      </span>
    </div>
  )
}

function CodeBlock({ text }: { text: string }) {
  return (
    <div className="relative my-3">
      <pre className="rounded-xl border border-line bg-surface-muted p-4 pr-11 text-xs font-mono overflow-auto text-ink leading-relaxed">
        <HighlightedCode text={text} />
      </pre>
      <CopyButton text={text} className="absolute right-2 top-2" />
    </div>
  )
}

function Table({ head, rows, lang }: { head: Bi[]; rows: Bi[][]; lang: Lang }) {
  return (
    <div className="my-3 overflow-x-auto rounded-lg border border-line">
      <table className="min-w-full text-left text-xs">
        <thead className="bg-surface-muted text-ink-muted">
          <tr>
            {head.map((h) => (
              <th key={h.zh} className="px-3 py-2 font-medium whitespace-nowrap">
                {h[lang]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-line align-top">
              {row.map((cell, j) => (
                <td key={j} className={j === 0 ? 'px-3 py-2 whitespace-nowrap text-ink' : 'px-3 py-2 text-ink-secondary'}>
                  {j === 0 ? <code className="font-mono">{cell[lang]}</code> : cell[lang]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Block({ block, lang }: { block: DocBlock; lang: Lang }) {
  switch (block.kind) {
    case 'p':
      return <p className="my-2 text-sm text-ink-secondary leading-relaxed">{block.text[lang]}</p>
    case 'note':
      return <p className="my-3 border-l-2 border-line pl-3 text-xs text-ink-muted leading-relaxed">{block.text[lang]}</p>
    case 'code':
      return <CodeBlock text={block.text} />
    case 'list':
      return (
        <ul className="my-2 list-disc space-y-1.5 pl-5 text-sm text-ink-secondary leading-relaxed">
          {block.items.map((item) => (
            <li key={item.zh}>{item[lang]}</li>
          ))}
        </ul>
      )
    case 'table':
      return <Table head={block.head} rows={block.rows} lang={lang} />
  }
}

function Section({ section, lang }: { section: DocSection; lang: Lang }) {
  return (
    <section id={section.id} className="scroll-mt-4">
      <h3 className="text-base font-semibold text-ink">{section.title[lang]}</h3>
      {section.blocks.map((block, i) => (
        <Block key={i} block={block} lang={lang} />
      ))}
      {section.subsections?.map((sub) => (
        <div key={sub.id} id={sub.id} className="mt-5 scroll-mt-4">
          <h4 className="text-sm font-semibold text-ink">{sub.title[lang]}</h4>
          {sub.blocks.map((block, i) => (
            <Block key={i} block={block} lang={lang} />
          ))}
        </div>
      ))}
    </section>
  )
}

export function Docs() {
  const t = useT()
  const { lang } = useLang()
  const sections = useMemo(() => buildDocs().filter((section) => !!SERVICE_BASE || !['access', 'auth', 'rest'].includes(section.id)), [])

  return (
    <>
      <PageHeader title={t('docs.title')} desc={t('docs.subtitle')} />
      {!SERVICE_BASE && <p role="status">{t('quickstart.addressUnavailable')}</p>}

      <div className="flex flex-col gap-4 pb-10">
        <Panel className="px-5 py-2">
          <ServiceStatus />
        </Panel>

        <Panel className="px-6 py-5">
          <div className="flex flex-col gap-7">
            {sections.map((section) => (
              <Section key={section.id} section={section} lang={lang} />
            ))}
          </div>
        </Panel>
      </div>
    </>
  )
}
