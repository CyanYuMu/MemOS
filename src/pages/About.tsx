import { Link } from 'react-router-dom'
import { useT } from '../lib/store'
import { Panel, PageHeader } from '../components/ui'
import { Icon } from '../components/Icon'

/** 关于我们。
 *
 *  ⚠️ 这一页**只写能当场核对的事**，而且是关于**这个控制台**的事。
 *  2026-10-19 的第一版列过「记忆沉淀 / 语义检索 / 画像与偏好 / 多租户隔离 / 调用可观测」，
 *  那是**记忆服务**的能力，不是控制台的：控制台自己不抽取、不检索、不生成画像。
 *  列在这里等于替别人的能力背书，业务点进来一条都验不了（用户的原话：
 *  「要是没得写，你就别写什么实质性的东西」）。
 *  现在改成「这里能做什么」：只写能力，不写页面名。页面名在侧边栏里已经有了，
 *  每条再以「记忆列表：」「调用日志：」开头，等于把同一个词在屏幕上说两遍。
 *
 *  ⚠️ 这里也不要写「接口路径见 openapi.json / 前端接口在 src/lib/api.ts」这类内部实现细节，
 *  那是给维护同学看的。维护向的排障入口在业务操作手册里。
 *
 *  ⚠️ 外层**不要**加 `max-w-[…]`：AppShell 的内容区是 `flex flex-col h-full`，
 *  加了宽度上限会把页面卡成一条窄栏、右边整片留白（2026-10-19 用户报的布局 bug）。
 *  两栏都 `flex-1`：页面清单按内容排，右栏用真实入口补足高度，
 *  否则窗口一高，下半截就是一整块空面板。 */
export function About() {
  const t = useT()
  const pages = [
    'about.page.memories',
    'about.page.requests',
    'about.page.graph',
    'about.page.usage',
    'about.page.keys',
    'about.page.extraction',
  ] as const
  const starts = [
    { to: '/cn/quickstart', label: t('quickstart.title'), desc: t('about.start.quickstart') },
    { to: '/cn/apikeys', label: t('apikeys.title'), desc: t('about.start.apikeys') },
    { to: '/cn/docs', label: t('docs.title'), desc: t('about.start.docs') },
  ]

  return (
    <>
      <PageHeader title={t('about.title')} />
      <div className="flex flex-col gap-4 flex-1 min-h-0">
        <Panel className="shrink-0 px-5 py-4 text-sm leading-6 text-ink-secondary">{t('about.body')}</Panel>
        <div className="flex gap-4 flex-1 min-h-0 flex-col lg:flex-row">
          <Panel className="flex-1 min-h-0 px-5 py-4">
            <h3 className="text-sm font-semibold text-ink">{t('about.pages')}</h3>
            <ul className="mt-3 space-y-2">
              {pages.map((k, i) => (
                <li key={k} className="flex items-start gap-3 rounded-lg border border-line bg-surface-muted/40 px-3.5 py-2.5">
                  <span className="mt-px w-5 shrink-0 text-xs font-mono tabular-nums text-ink-muted">{String(i + 1).padStart(2, '0')}</span>
                  <span className="text-sm leading-6 text-ink-secondary">{t(k)}</span>
                </li>
              ))}
            </ul>
          </Panel>

          {/* 右栏是真实入口，不是装饰：读完「有哪几页」之后，下一步就是去其中一页。 */}
          <Panel className="flex-1 min-h-0 px-5 py-4 lg:max-w-md">
            <h3 className="text-sm font-semibold text-ink">{t('about.start')}</h3>
            <p className="mt-1 text-xs leading-5 text-ink-muted">{t('about.startDesc')}</p>
            <ul className="mt-3 space-y-2">
              {starts.map((s) => (
                <li key={s.to}>
                  <Link
                    to={s.to}
                    className="group flex items-start gap-3 rounded-lg border border-line px-3.5 py-2.5 transition-colors hover:border-line-strong hover:bg-surface-muted"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium text-ink">{s.label}</span>
                      <span className="mt-0.5 block text-xs leading-5 text-ink-muted">{s.desc}</span>
                    </span>
                    <Icon
                      name="i-lucide:chevron-right"
                      className="mt-1 size-4 shrink-0 text-ink-faint transition-colors group-hover:text-ink-secondary"
                    />
                  </Link>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </>
  )
}
