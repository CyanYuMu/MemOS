import { useId, useState, type ReactNode } from 'react'
import { ChevronDown, Clock3, Fingerprint, History, LoaderCircle, MessageSquareMore, Network } from 'lucide-react'
import { useT } from '../../lib/store'
import { MemorySnapshotDetail } from './MemorySnapshotDetail'
import { isSearchSnapshotEntry } from './turnSnapshot'
import type { TurnSnapshot } from './turnSnapshot'

/** 轮次快照只读当时结果；没有历史实体时不再请求当前记忆来补齐。 */
export function MemorySnapshot({
  projectId,
  snapshot,
  children,
}: {
  projectId: string
  snapshot: TurnSnapshot
  children?: (trigger: ReactNode) => ReactNode
}) {
  const t = useT()
  const id = useId()
  const [open, setOpen] = useState(false)
  const [visibleCount, setVisibleCount] = useState(6)
  const searchCount = snapshot.entries.filter(isSearchSnapshotEntry).length
  const profileCount = snapshot.entries.length - searchCount
  const trigger = (
    <button
      type="button"
      className="memos-turn-snapshot"
      aria-expanded={open}
      aria-controls={id}
      onClick={() => {
        setOpen(!open)
        if (open) setVisibleCount(6)
      }}
    >
      {snapshot.status === 'pending' ? <LoaderCircle size={15} className="animate-spin" /> : <Network size={15} />}
      <span>{t('chat.snapshot.title')}</span>
      <small>
        {snapshot.status === 'pending'
          ? t('common.loading')
          : snapshot.status === 'unavailable'
            ? t('chat.snapshot.missing')
            : snapshot.partial
              ? t('chat.snapshot.partial')
              : t('chat.snapshot.count')(snapshot.entries.length)}
      </small>
      <ChevronDown size={14} className={open ? 'is-expanded' : undefined} />
    </button>
  )
  return (
    <>
      {children ? children(trigger) : trigger}
      {open && (
        <section id={id} className="memos-turn-reference" aria-label={t('chat.snapshot.title')}>
          {snapshot.status === 'pending' && <p role="status">{t('common.loading')}</p>}
          {snapshot.status === 'unavailable' && <p>{t('chat.snapshot.unavailable')}</p>}
          {snapshot.partial && <p>{t('chat.snapshot.partialBody')}</p>}
          {snapshot.status === 'available' && !snapshot.entries.length && <p>{t('chat.snapshot.empty')}</p>}
          {snapshot.entries.length > 0 && (
            <div className="memos-snapshot-sources">
              <span>
                {t('chat.snapshot.searchResults')} {searchCount}
              </span>
              <span>
                {t('chat.snapshot.profileContext')} {profileCount}
              </span>
            </div>
          )}
          <div className="memos-snapshot-grid">
            {snapshot.entries.slice(0, visibleCount).map((row, index) => {
              const entry = row.entry
              const score = entry.relativity
              const type = entry.memoryType
              return (
                <article className="memos-snapshot-card" key={JSON.stringify([entry.memoryId, entry.value, index])}>
                  <header>
                    <span>
                      {isSearchSnapshotEntry(row) ? (
                        <>
                          {typeof score === 'number' && Number.isFinite(score) ? (
                            <>
                              relativity <span>{(score > 1 ? score / 100 : score).toFixed(2)}</span>
                            </>
                          ) : (
                            t('chat.snapshot.unscoredSearch')
                          )}
                        </>
                      ) : (
                        t('chat.snapshot.profileContext')
                      )}
                    </span>
                    <b>{index + 1}</b>
                  </header>
                  <div className="memos-snapshot-content">
                    <h3 title={entry.key}>{entry.key || t('chat.snapshot.title')}</h3>
                    <div className="memos-snapshot-types">
                      <span
                        className={`memos-snapshot-type ${type === 'UserMemory' ? 'is-user' : type === 'WorkingMemory' ? 'is-working' : type === 'LongTermMemory' ? 'is-longterm' : ''}`}
                      >
                        {type || '—'}
                      </span>
                    </div>
                    <div className="memos-snapshot-tags">
                      {entry.tags?.map((tag, i) => (
                        <span key={`${tag}:${i}`}>{tag}</span>
                      ))}
                    </div>
                    <p className="memos-snapshot-line">
                      <Clock3 size={13} />
                      <time>{entry.createTime?.slice(0, 10) || '—'}</time>
                    </p>
                    <p className="memos-snapshot-line" title={entry.value}>
                      <MessageSquareMore size={13} />
                      <span>{entry.value}</span>
                    </p>
                    <div className="memos-snapshot-meta">
                      <span className={`memos-snapshot-status${entry.status === 'activated' ? ' is-active' : ''}`}>
                        <Network size={12} />
                        {entry.status || '—'}
                      </span>
                      <p className="memos-snapshot-line" title={entry.memoryId}>
                        <Fingerprint size={13} />
                        <span>{entry.memoryId || '—'}</span>
                      </p>
                      <p className="memos-snapshot-line">
                        <History size={13} />
                        <time>{entry.updateTime?.slice(0, 10) || '—'}</time>
                      </p>
                      {row.record ? (
                        <MemorySnapshotDetail projectId={projectId} row={row}>
                          <button type="button">{t('chat.tools.expand')}</button>
                        </MemorySnapshotDetail>
                      ) : (
                        <small>{t('chat.snapshot.fieldsMissing')}</small>
                      )}
                    </div>
                  </div>
                </article>
              )
            })}
          </div>
          {snapshot.entries.length > 6 && (
            <div className="memos-snapshot-pagination">
              <span>
                {Math.min(visibleCount, snapshot.entries.length)} / {snapshot.entries.length}
              </span>
              {visibleCount < snapshot.entries.length && (
                <button type="button" onClick={() => setVisibleCount((n) => n + 6)}>
                  {t('chat.snapshot.loadMore')}
                </button>
              )}
            </div>
          )}
        </section>
      )}
    </>
  )
}
