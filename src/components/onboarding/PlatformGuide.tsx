import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import * as Dialog from '@radix-ui/react-dialog'
import { ArrowRight, BookOpen, Check, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import type { Driver } from 'driver.js'
import 'driver.js/dist/driver.css'
import { usePlatformGuide, GUIDE_ROUTES } from '../../lib/onboarding'
import { errText } from '../../lib/i18n'
import { stageShape } from '../../lib/stageShape'
import { api } from '../../lib/api'
import { useLang, useMeta, useProject, useT, useToast } from '../../lib/store'
import { Button } from '../ui/button'

const titles = [
  'webGuide.space.title',
  'webGuide.quickstart.title',
  'webGuide.key.title',
  'webGuide.chat.title',
  'webGuide.memory.title',
  'webGuide.logs.title',
] as const
const descriptions = [
  'webGuide.space.body',
  'webGuide.quickstart.body',
  'webGuide.key.body',
  'webGuide.chat.body',
  'webGuide.memory.body',
  'webGuide.logs.body',
] as const
const anchors = [
  '[data-guide="project"]',
  '[data-guide="quickstart"]',
  '[data-guide="create-key"]',
  '',
  '[data-guide="memory-row"] td:last-child button',
  '[data-guide="requests"]',
]

/** 弹窗打开时让出遮罩；路由或目标变化后重新定位，缺失目标始终保留可操作的面板。 */
function Spotlight({ selector, title, children }: { selector: string; title: string; children: ReactNode }) {
  const [target, setTarget] = useState<HTMLElement | null>(null)
  /** 弹窗盖在目标上时，卡片先收起：不然它会掉回页面顶部，把整页内容往下顶。 */
  const [paused, setPaused] = useState(false)
  const tourRef = useRef<Driver | null>(null)
  useLayoutEffect(() => {
    if (target) tourRef.current?.refresh()
  }, [target, children])
  useEffect(() => {
    if (!selector) return
    let tour: Driver | undefined
    let createDriver: typeof import('driver.js').driver | undefined
    let active = true
    let last: Element | null = null
    const sync = () => {
      if (!createDriver || !active) return
      const modal = document.querySelector('[role="dialog"][data-state="open"]')
      const element = Array.from(document.querySelectorAll(selector)).find((x) => x.getBoundingClientRect().width > 0) ?? null
      setPaused(Boolean(modal && element))
      if (modal || !element) {
        if (last) {
          tour?.destroy()
          last = null
          setTarget(null)
        }
        return
      }
      if (element === last) return
      last = element
      tour = createDriver({
        animate: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        overlayOpacity: 0.3,
        ...stageShape(element, 8),
        popoverClass: 'platform-guide-popover',
        showButtons: [],
        allowClose: false,
        allowKeyboardControl: false,
        onPopoverRender: (popover) => setTarget(popover.description),
      })
      tourRef.current = tour
      const nearRightEdge = element.getBoundingClientRect().right > window.innerWidth - 420
      tour.highlight({
        element,
        popover: {
          title,
          description: ' ',
          side: nearRightEdge || window.innerWidth < 720 ? 'bottom' : 'right',
          align: nearRightEdge ? 'end' : 'start',
        },
      })
    }
    const observer = new MutationObserver(sync)
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-state'] })
    void import('driver.js')
      .then((module) => {
        if (active) {
          createDriver = module.driver
          sync()
        }
      })
      .catch(() => {})
    window.addEventListener('resize', sync)
    return () => {
      active = false
      observer.disconnect()
      window.removeEventListener('resize', sync)
      tour?.destroy()
      tourRef.current = null
    }
  }, [selector, title])
  if (paused) return null
  return target ? createPortal(children, target) : children
}

export function PlatformGuideEntry({ compact = false }: { compact?: boolean }) {
  const guide = usePlatformGuide()
  const t = useT()
  if (!guide?.available) return null
  const resume = () => {
    if (!guide.state) {
      guide.reload()
      return
    }
    void guide.change(guide.state.status === 'completed' ? 'replay' : 'start').catch(() => {})
  }
  return (
    <button
      type="button"
      className={`platform-guide-entry${compact ? ' platform-guide-entry--compact' : ''}`}
      aria-label={t('webGuide.title')}
      onClick={resume}
      disabled={guide.busy || (!guide.state && !guide.error)}
    >
      <BookOpen size={16} />
      {!compact && <span>{t(guide.state?.status === 'completed' ? 'webGuide.replay' : 'webGuide.title')}</span>}
    </button>
  )
}

/** 与卡片右上角同一口径：进行中显示「当前第几步」，未开始为 0，完成为 6。 */
function guideCount(state: { status: string; step: number }) {
  if (state.status === 'completed') return 6
  if (state.status === 'pending') return 0
  return Math.min(state.step, 5) + 1
}

export function PlatformGuideChecklist() {
  const guide = usePlatformGuide()
  const t = useT()
  const navigate = useNavigate()
  if (!guide?.available || !guide.state) return null
  const state = guide.state
  const resume = () => {
    if (state.status === 'active') navigate(GUIDE_ROUTES[Math.min(state.step, 5)])
    else void guide.change(state.status === 'completed' ? 'replay' : 'start').catch(() => {})
  }
  return (
    <section className="platform-guide-checklist" aria-label={t('webGuide.title')}>
      <header>
        <h2>{t('webGuide.title')}</h2>
        <span>{guideCount(state)} / 6</span>
        <Button size="sm" variant="outline" disabled={guide.busy} onClick={resume}>
          {t(state.status === 'completed' ? 'webGuide.replay' : state.step ? 'webGuide.continue' : 'webGuide.begin')}
        </Button>
      </header>
      <ol>
        {titles.map((key, index) => (
          <li key={key} data-done={index < state.step} aria-current={state.step === index ? 'step' : undefined}>
            <span>{index < state.step ? <Check size={13} /> : index + 1}</span>
            {t(key)}
          </li>
        ))}
      </ol>
      {guide.error && <p role="alert">{guide.error}</p>}
    </section>
  )
}

export function PlatformGuide() {
  const guide = usePlatformGuide()
  const t = useT()
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const { currentProjectId, currentProjectName, setCurrent } = useProject()
  const { lang } = useLang()
  const toast = useToast()
  const { account } = useMeta()
  const state = guide?.state
  const step = Math.min(state?.step ?? 0, 5)
  const onRoute = pathname.replace(/\/$/, '') === GUIDE_ROUTES[step]
  const chat = useQuery({
    queryKey: ['useAsync', 'chat.state', currentProjectId, account?.accountId],
    queryFn: () => api.chat.state(currentProjectId),
    enabled: guide?.available && state?.status === 'active' && step === 2,
    staleTime: 0,
    refetchInterval: state?.status === 'active' && step === 2 ? 4000 : false,
  })
  useEffect(() => {
    if (state?.status !== 'active' || step === 3) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented && !document.querySelector('[role="dialog"][data-state="open"]')) {
        event.preventDefault()
        void guide?.change('dismiss').catch(() => {})
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [guide, state?.status, step])
  if (!guide?.available) return null
  if (!state && guide.error)
    return (
      <section className="platform-guide" role="alert">
        <p>{guide.error}</p>
        <Button size="sm" onClick={guide.reload}>
          {t('common.retry')}
        </Button>
      </section>
    )
  const personalSpace = account?.projects.find((p) => p.kind === 'personal' && p.role === 'owner' && p.member !== false)
  const personal = personalSpace?.id === currentProjectId
  const welcome = state?.status === 'pending' && pathname.replace(/\/$/, '') === '/cn/quickstart' && personal
  const action = (name: 'start' | 'advance' | 'back' | 'dismiss') => {
    void guide.change(name).catch(() => {})
  }
  /** 对话页自己有分步提示，这里收成一行，不挤占对话区。 */
  const bar = onRoute && step === 3
  const blocked = onRoute && ((step === 2 && !chat.data?.canChat) || (step === 4 && !guide.detailsSeen))
  const card = (
    <section className={`platform-guide${bar ? ' platform-guide--bar' : ''}`} aria-label={t('webGuide.title')}>
      <header>
        <span>{t('webGuide.title')}</span>
        <span className="platform-guide-count">{step + 1} / 6</span>
        <button type="button" aria-label={t('webGuide.later')} disabled={guide.busy} onClick={() => action('dismiss')}>
          <X size={16} />
        </button>
      </header>
      <ol aria-label={t('webGuide.progress')} className="platform-guide-track">
        {titles.map((key, i) => (
          <li key={key} aria-current={step === i ? 'step' : undefined} data-done={step > i}>
            <span>{step > i ? <Check size={12} /> : i + 1}</span>
            <small>{t(key)}</small>
          </li>
        ))}
      </ol>
      <h2>{t(titles[step])}</h2>
      <p>{t(descriptions[step])}</p>
      {step === 0 && <p className="platform-guide-space">{currentProjectName}</p>}
      {step === 2 && onRoute && !chat.data?.canChat && (
        <p role="status">
          {chat.isError
            ? t('webGuide.checkFailed')
            : chat.data?.readOnly
              ? t('webGuide.key.readOnly')
              : chat.data?.hasApiKey
                ? t('webGuide.chatUnavailable')
                : t('webGuide.key.wait')}
        </p>
      )}
      {step === 3 && onRoute && <p className="platform-guide-follow">{t('webGuide.chat.follow')}</p>}
      {step === 4 && onRoute && !guide.detailsSeen && <p role="status">{t('webGuide.memory.wait')}</p>}
      {guide.error && <p role="alert">{guide.error}</p>}
      <footer>
        {step > 0 && (
          <Button size="sm" variant="outline" disabled={guide.busy} onClick={() => action('back')}>
            {t('playground.tour.previous')}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => action('dismiss')} disabled={guide.busy || (!guide.state && !guide.error)}>
          {t('webGuide.later')}
        </Button>
        {step === 2 && onRoute && chat.data?.readOnly && personalSpace && (
          <Button
            size="sm"
            onClick={() => {
              void setCurrent(personalSpace.id)
                .then(() => navigate('/cn/quickstart'))
                .catch((e: unknown) => toast(errText(lang, e), 'err'))
            }}
          >
            {t('webGuide.key.switchPersonal')}
          </Button>
        )}
        {step === 2 && onRoute && !chat.data?.canChat && (
          <Button
            size="sm"
            onClick={() => {
              void chat.refetch()
            }}
          >
            {t('common.retry')}
          </Button>
        )}
        {!(step === 3 && onRoute) && (
          <Button
            size="sm"
            variant="primary"
            disabled={guide.busy || blocked}
            onClick={() => (onRoute ? action('advance') : navigate(GUIDE_ROUTES[step]))}
          >
            {!onRoute
              ? t('webGuide.continue')
              : step === 5
                ? t('webGuide.finish')
                : step === 1
                  ? t('webGuide.createKey')
                  : step === 2
                    ? t('webGuide.goChat')
                    : t('webGuide.next')}
            <ArrowRight size={14} />
          </Button>
        )}
      </footer>
    </section>
  )
  return (
    <>
      <Dialog.Root
        open={!!welcome}
        onOpenChange={(open) => {
          if (!open) action('dismiss')
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="memos-welcome-overlay" />
          <Dialog.Content className="platform-guide-welcome">
            <Dialog.Title>{t('webGuide.welcome')}</Dialog.Title>
            <Dialog.Description>{t('webGuide.welcomeBody')}</Dialog.Description>
            <p className="platform-guide-space">{currentProjectName}</p>
            {guide.error && <p role="alert">{guide.error}</p>}
            <footer>
              <Button variant="ghost" disabled={guide.busy} onClick={() => action('dismiss')}>
                {t('webGuide.later')}
              </Button>
              <Button variant="primary" loading={guide.busy} onClick={() => action('start')}>
                {t('webGuide.begin')}
                <ArrowRight size={16} />
              </Button>
            </footer>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {state?.status === 'active' &&
        state.step < 6 &&
        (onRoute && step !== 3 ? (
          <Spotlight selector={step === 2 && chat.data?.readOnly ? '[data-guide="project"]' : anchors[step]} title={t(titles[step])}>
            {card}
          </Spotlight>
        ) : (
          card
        ))}
      {state?.status === 'completed' && pathname === '/cn/quickstart' && (
        <div className="platform-guide-completed">
          <Check size={18} />
          <span>{t('webGuide.completed')}</span>
          <Link to="/cn/chat">{t('webGuide.goChat')}</Link>
        </div>
      )}
    </>
  )
}
