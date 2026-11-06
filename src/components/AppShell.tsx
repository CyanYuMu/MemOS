import type { ReactNode } from 'react'
import { useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import * as Dialog from '@radix-ui/react-dialog'
import { Menu, X } from 'lucide-react'
import { PlatformGuide } from './onboarding/PlatformGuide'
import { Sidebar } from './Sidebar'
import { Logo } from './Logo'
import { Icon } from './Icon'
import { useMeta, useProject, useT } from '../lib/store'

/** 全站共用导航。聊天仅切换内容区的排版，不另建一套项目和账号入口。 */
export function AppShell({ children }: { children: ReactNode }) {
  const t = useT()
  const { pathname } = useLocation()
  const { account, previewUser } = useMeta()
  const { ready, currentProjectId } = useProject()
  const isChat = pathname.replace(/\/$/, '') === '/cn/chat'
  const showOnboarding = ready && !currentProjectId && !pathname.startsWith('/cn/projects')
  const scrollRef = useRef<HTMLDivElement>(null)
  const [mobileOpen, setMobileOpen] = useState(false)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0 })
  }, [pathname])

  useEffect(() => {
    if (!mobileOpen) return
    const media = window.matchMedia('(min-width: 720px)')
    const onWide = () => {
      if (media.matches) setMobileOpen(false)
    }
    media.addEventListener('change', onWide)
    return () => media.removeEventListener('change', onWide)
  }, [mobileOpen])

  return (
    <Dialog.Root open={mobileOpen} onOpenChange={setMobileOpen}>
      <div className="console-shell">
        <Sidebar />
        <main className="console-main">
          <div className="console-mobile-bar">
            <Dialog.Trigger className="console-menu-trigger" aria-label={t('nav.openMenu')}>
              <Menu size={21} />
            </Dialog.Trigger>
            <Link to="/cn/quickstart" className="console-mobile-brand">
              <Logo />
            </Link>
          </div>
          <PlatformGuide />
          {previewUser && <div className="console-preview-banner">{t('preview.banner')}</div>}
          <div ref={scrollRef} className={isChat ? 'console-chat-content' : 'console-content'}>
            {showOnboarding && (
              <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warning/30 bg-warning-soft px-4 py-3">
                <div className="flex min-w-0 items-start gap-3">
                  <Icon name="i-lucide:folder-plus" className="mt-0.5 size-5 shrink-0 text-warning" />
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-ink">{t('onboarding.title')}</div>
                    <div className="mt-0.5 text-xs leading-5 text-ink-secondary">
                      {t(account?.platformAdmin ? 'onboarding.descAdmin' : 'onboarding.desc')}
                    </div>
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Link
                    to="/cn/projects"
                    className="inline-flex h-8 items-center rounded-lg border border-line bg-surface px-3 text-xs font-medium text-ink-secondary transition-colors hover:bg-surface-hover hover:text-ink"
                  >
                    {t('onboarding.go')}
                  </Link>
                  {account?.platformAdmin && (
                    <Link
                      to="/cn/projects?create=1"
                      className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-accent px-3 text-xs font-medium text-on-accent transition-colors hover:bg-accent-hover"
                    >
                      <Icon name="i-lucide:plus" className="size-3.5" />
                      {t('onboarding.create')}
                    </Link>
                  )}
                </div>
              </div>
            )}
            {children}
          </div>
        </main>
      </div>
      <Dialog.Portal>
        <Dialog.Overlay className="console-nav-overlay" />
        <Dialog.Content className="console-nav-drawer">
          <Dialog.Title className="sr-only">{t('nav.navigation')}</Dialog.Title>
          <Dialog.Description className="sr-only">{t('nav.navigationHint')}</Dialog.Description>
          <Sidebar mobile onNavigate={() => setMobileOpen(false)} />
          <Dialog.Close className="console-nav-close" aria-label={t('common.close')}>
            <X size={20} />
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
