/** 版本历史抽屉：分页加载，每个版本可展开、与当前比较、回滚。 */

import { useEffect, useRef, useState } from 'react'
import type { PromptVersion } from '../../lib/api'
import { useStr, useT } from '../../lib/store'
import { formatBeijingTime } from '../../lib/datetime'
import { versionLabel } from '../../lib/prompts'
import type { TKey } from '../../lib/i18n'
import { Button } from '../ui/button'
import { Badge } from '../ui/badge'
import { Icon } from '../Icon'
import { Skeleton } from '../ui/skeleton'

const KIND_LABEL: Record<string, string> = {
  save: 'prompts.versions.kind.save',
  reset: 'prompts.versions.kind.reset',
  rollback: 'prompts.versions.kind.rollback',
  migrated: 'prompts.versions.kind.migrated',
}

const KIND_VARIANT: Record<string, 'default' | 'secondary' | 'warning' | 'success'> = {
  save: 'default',
  reset: 'secondary',
  rollback: 'warning',
  migrated: 'secondary',
}

export function VersionDrawer({
  open,
  onClose,
  versions,
  hasMore,
  loading,
  error,
  canEdit,
  rollingBackId,
  onLoadMore,
  onCompare,
  onRollback,
}: {
  open: boolean
  onClose: () => void
  versions: PromptVersion[]
  hasMore: boolean
  loading: boolean
  error: string
  canEdit: boolean
  rollingBackId: number | null
  onLoadMore: () => void
  onCompare: (v: PromptVersion) => void
  onRollback: (v: PromptVersion) => void
}) {
  const t = useT()
  const str = useStr()
  const panelRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState<number | null>(null)

  useEffect(() => {
    if (!open) return
    const prev = document.activeElement as HTMLElement | null
    panelRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (e.key !== 'Tab' || !panelRef.current) return
      // 焦点留在抽屉里：首尾循环，别让 Tab 跑到背后的页面上
      const nodes = panelRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], select, textarea, input, [tabindex]:not([tabindex="-1"])',
      )
      if (!nodes.length) return
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      prev?.focus()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="studio-drawer-layer" role="presentation">
      <div className="studio-drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <div ref={panelRef} className="studio-drawer" role="dialog" aria-modal="true" aria-label={t('prompts.versions.title')} tabIndex={-1}>
        <div className="flex items-center justify-between border-b border-line px-5 py-4">
          <h3 className="text-sm font-semibold text-ink">{t('prompts.versions.title')}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('common.close')}
            className="rounded-lg p-1 text-ink-faint transition-colors hover:bg-surface-hover hover:text-ink-secondary"
          >
            <Icon name="i-lucide:x" className="shrink-0 size-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading && versions.length === 0 && (
            <div className="space-y-3">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-5/6" />
            </div>
          )}
          {!loading && error && (
            <p className="text-xs leading-5 text-warning" role="alert">
              {error}
            </p>
          )}
          {!loading && !error && versions.length === 0 && <p className="text-xs text-ink-muted">{t('prompts.versions.empty')}</p>}

          <ul className="space-y-3">
            {versions.map((v) => {
              const isReset = v.kind === 'reset'
              return (
                <li key={v.versionId} className="rounded-xl border border-line bg-surface p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={KIND_VARIANT[v.kind] ?? 'secondary'}>
                      {str((KIND_LABEL[v.kind] ?? 'prompts.versions.kind.save') as TKey)}
                    </Badge>
                    <span className="font-mono text-xs text-ink-secondary">{versionLabel(v.versionNo, 0)}</span>
                    <span className="text-xs text-ink-faint">{formatBeijingTime(v.createdAt)}</span>
                    {v.author && <span className="text-xs text-ink-faint">{v.author}</span>}
                  </div>
                  {v.note && <p className="mt-1.5 text-xs leading-5 text-ink-muted">{v.note}</p>}

                  <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    <Button size="sm" variant="ghost" onClick={() => setExpanded((cur) => (cur === v.versionId ? null : v.versionId))}>
                      {expanded === v.versionId ? t('prompts.versions.hide') : t('prompts.versions.view')}
                    </Button>
                    {!isReset && (
                      <Button size="sm" variant="ghost" onClick={() => onCompare(v)}>
                        {t('prompts.versions.compare')}
                      </Button>
                    )}
                    {canEdit && !isReset && (
                      <Button size="sm" variant="secondary" loading={rollingBackId === v.versionId} onClick={() => onRollback(v)}>
                        {t('prompts.versions.rollback')}
                      </Button>
                    )}
                  </div>

                  {expanded === v.versionId && (
                    <pre className="studio-version-body mt-2">{isReset ? t('prompts.versions.resetBody') : v.body}</pre>
                  )}
                </li>
              )
            })}
          </ul>

          {hasMore && (
            <div className="mt-4 flex justify-center">
              <Button size="sm" variant="secondary" loading={loading} onClick={onLoadMore}>
                {t('prompts.versions.loadMore')}
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
