import { memo, useEffect, useMemo, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { CircleHelp, Database, LoaderCircle, X } from 'lucide-react'
import { motion, useReducedMotion } from 'motion/react'
import { useT } from '../../lib/store'
import { Tooltip } from '../ui/tooltip'
import { entryFromMemory, type MemoryEntry } from './memory'
import {
  isMemoryOperation,
  memoryLogPresentation,
  memoryOperationTitle,
  memoryWriteCallId,
  memoryCallId,
  memoryActivityForCall,
  type MemoryLogRow,
} from './memoryLog'
import { useMemoryActivity } from './useMemoryActivity'
import { MemoryDetailFields } from './MemoryDetailFields'
import { MemoryOperationDetail } from './MemoryOperationDetail'
import type { Memory, MemoryLogActivity } from '../../lib/api'
import { shortToolName, type ChatToolItem } from './timeline'

interface Props {
  projectId: string
  items: ChatToolItem[]
  activities?: Record<string, MemoryLogActivity>
  onLocate: (toolId: string) => void
  open: boolean
  onClose: () => void
}

type Detail =
  | { kind: 'detail'; entry: MemoryEntry; snapshot?: Memory; previous?: Memory }
  | { kind: 'operation'; item: ChatToolItem; row?: MemoryLogRow }

/** 参考原站 LogList：摘要留在紧凑时间线，完整内容及字段放进详情弹窗。 */
export const ToolPanel = memo(
  function ToolPanel({ projectId, items, onLocate, open, onClose, activities: providedActivities }: Props) {
    const t = useT()
    const panelRef = useRef<HTMLElement>(null)
    const openerRef = useRef<HTMLElement | null>(null)
    const [selection, setSelection] = useState<{ projectId: string; detail: Detail } | null>(null)
    const modal = selection?.projectId === projectId ? selection.detail : null
    function setModal(next: Detail | null) {
      setSelection(next ? { projectId, detail: next } : null)
    }
    const ownActivities = useMemoryActivity(projectId, items, !providedActivities)
    const activities = providedActivities ?? ownActivities
    const operation = modal?.kind === 'operation' ? (items.find((item) => item.id === modal.item.id) ?? modal.item) : null
    const [clearVersion, setClearVersion] = useState(0)
    const logItems = useMemo(() => items.filter((item) => isMemoryOperation(item.toolName)), [items])
    const logEvents = useMemo(() => {
      const events: (
        | { kind: 'tool'; key: string; item: ChatToolItem; time?: string | number }
        | { kind: 'memory'; key: string; activity: MemoryLogActivity; time: string; item?: ChatToolItem }
      )[] = []
      for (const item of [...logItems].reverse()) {
        const linked = memoryActivityForCall(activities, memoryCallId(item))
        if (linked && 'eventId' in linked && shortToolName(item.toolName) !== 'add_message') continue
        events.push({ kind: 'tool', key: item.id, item, time: item.timestamp })
      }
      for (const activity of Object.values(activities)) {
        if (activity.status === 'succeeded' || ('eventId' in activity && ['failed', 'unavailable'].includes(activity.status)))
          events.push({
            kind: 'memory',
            key: `memory:${'eventId' in activity ? activity.eventId : activity.callId}`,
            activity,
            time: activity.completedAt,
            item: logItems.find((item) => memoryCallId(item) === activity.callId),
          })
      }
      const time = (value: string | number | undefined) =>
        value === undefined ? Number.NEGATIVE_INFINITY : typeof value === 'number' ? value : Date.parse(value)
      return events.sort((a, b) => time(b.time) - time(a.time) || 0)
    }, [logItems, activities])

    function showModal(next: Detail) {
      if (!modal) openerRef.current = document.activeElement as HTMLElement | null
      setClearVersion((version) => version + 1)
      setModal(next)
    }

    useEffect(() => {
      if (!open || window.matchMedia('(min-width: 1100px)').matches) return
      const previous = document.activeElement as HTMLElement | null
      panelRef.current?.querySelector<HTMLButtonElement>('.memos-log-close')?.focus()
      const onKey = (event: KeyboardEvent) => {
        // 详情由 Radix 管理焦点；不让侧栏的 Escape/Tab 处理器干扰 portal 内的弹窗。
        if (event.defaultPrevented || document.querySelector('.memos-detail-dialog')) return
        if (event.key === 'Escape') {
          event.preventDefault()
          onClose()
          return
        }
        if (!panelRef.current?.contains(event.target as Node)) return
        if (event.key !== 'Tab') return
        const buttons = Array.from(panelRef.current.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'))
        const first = buttons[0],
          last = buttons[buttons.length - 1]
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last?.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first?.focus()
        }
      }
      const media = window.matchMedia('(min-width: 1100px)')
      const onWide = () => {
        if (media.matches) onClose()
      }
      media.addEventListener('change', onWide)
      document.addEventListener('keydown', onKey)
      return () => {
        media.removeEventListener('change', onWide)
        document.removeEventListener('keydown', onKey)
        previous?.focus()
      }
    }, [open, onClose])

    return (
      <>
        {open && <button className="memos-log-scrim" aria-label={t('common.close')} onClick={onClose} />}
        <aside
          ref={panelRef}
          className={`memos-log-panel${open ? ' is-open' : ''}`}
          id="chat-log-panel"
          aria-label={t('playground.logs.title')}
          role={open ? 'dialog' : undefined}
          aria-modal={open || undefined}
        >
          <div className="memos-log-header">
            <div>
              <h2>{t('playground.logs.title')}</h2>
              <Tooltip label={t('playground.logs.help')}>
                <button className="memos-log-help" aria-label={t('playground.logs.help')}>
                  <CircleHelp size={20} />
                </button>
              </Tooltip>
            </div>
            {
              <Tooltip label={t('playground.logs.libraryScope')}>
                <a href="/cn/memoryList" className="memos-log-library" aria-label={t('playground.logs.library')}>
                  <Database size={16} />
                </a>
              </Tooltip>
            }
            <button className="memos-icon-button memos-log-close" aria-label={t('common.close')} onClick={onClose}>
              <X size={18} />
            </button>
          </div>
          <div className="memos-log-scroll" onScroll={() => setClearVersion((version) => version + 1)}>
            {logEvents.length ? (
              <ol className="memos-log-timeline">
                {logEvents.map((event) =>
                  event.kind === 'memory' ? (
                    <MemoryActivityItem key={event.key} activity={event.activity} item={event.item} onDetail={showModal} />
                  ) : (
                    <ToolResultItem
                      key={event.key}
                      item={event.item}
                      clearVersion={clearVersion}
                      onDetail={showModal}
                      onLocate={onLocate}
                      processing={memoryActivityForCall(activities, memoryWriteCallId(event.item))?.status === 'pending'}
                    />
                  ),
                )}
              </ol>
            ) : (
              <div className="memos-log-empty">
                <div className="memos-log-placeholder" aria-hidden="true">
                  <div>
                    <i />
                    <i />
                  </div>
                  <div>
                    <span />
                    <span />
                    <span />
                    <span />
                  </div>
                </div>
                <p>{t('playground.logs.noLogs')}</p>
              </div>
            )}
          </div>
        </aside>
        <Dialog.Root open={modal !== null} onOpenChange={(value) => !value && setModal(null)}>
          <Dialog.Portal>
            <Dialog.Overlay className="memos-detail-overlay" />
            <Dialog.Content
              className="memos-detail-dialog"
              onCloseAutoFocus={(event) => {
                event.preventDefault()
                const target = openerRef.current?.isConnected ? openerRef.current : document.getElementById('chat-input')
                target?.focus({ preventScroll: true })
              }}
            >
              <div className="memos-detail-header">
                <Dialog.Title>{t(operation ? memoryOperationTitle(operation) : 'memory.detailTitle')}</Dialog.Title>
                <Dialog.Close className="memos-detail-close" aria-label={t('common.close')}>
                  <X size={20} />
                </Dialog.Close>
              </div>
              <Dialog.Description className="sr-only">{t('playground.logs.detailDescription')}</Dialog.Description>
              <div className="memos-detail-body">
                {modal?.kind === 'detail' ? (
                  <MemoryDetailFields
                    key={`${projectId}:${modal.entry.memoryId}`}
                    projectId={projectId}
                    entry={modal.entry}
                    snapshot={modal.snapshot}
                    previous={modal.previous}
                  />
                ) : modal?.kind === 'operation' && operation ? (
                  <MemoryOperationDetail
                    item={operation}
                    row={modal.row}
                    activity={memoryActivityForCall(activities, memoryWriteCallId(operation))}
                    onMemory={(entry, snapshot) => showModal({ kind: 'detail', entry, snapshot })}
                  />
                ) : null}
              </div>
              <div className="memos-detail-footer">
                {operation && (
                  <button
                    onClick={() => {
                      onLocate(operation.id)
                      setModal(null)
                    }}
                  >
                    {t('playground.logs.locate')}
                  </button>
                )}
                <Dialog.Close>{t('common.close')}</Dialog.Close>
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </>
    )
  },
  (prev, next) =>
    prev.projectId === next.projectId &&
    prev.activities === next.activities &&
    prev.onLocate === next.onLocate &&
    prev.open === next.open &&
    prev.onClose === next.onClose &&
    prev.items.length === next.items.length &&
    prev.items.every((item, index) => item === next.items[index]),
)

function ToolResultItem({
  item,
  clearVersion,
  onDetail,
  onLocate,
  processing,
}: {
  item: ChatToolItem
  clearVersion: number
  onDetail: (detail: Detail) => void
  onLocate: (id: string) => void
  processing: boolean
}) {
  const t = useT()
  const reduce = useReducedMotion()
  const [firstVersion] = useState(() => (item.running ? clearVersion : -1))
  const presentation = memoryLogPresentation(item)
  const title =
    presentation.count === undefined
      ? t(presentation.titleKey)
      : presentation.titleKey === 'playground.logs.updateCall'
        ? t('playground.logs.updated')(presentation.count)
        : presentation.titleKey === 'playground.logs.deleteCall'
          ? t('playground.logs.deleted')(presentation.count)
          : t('playground.logs.found')(presentation.count)
  const timestamp = item.timestamp === undefined ? null : new Date(item.timestamp)
  const time = timestamp && Number.isFinite(timestamp.getTime()) ? timestamp.toLocaleTimeString([], { hour12: false }) : ''
  return (
    <motion.li
      layout="position"
      initial={reduce || !item.running ? false : { opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: reduce ? 0 : 0.6, ease: 'easeOut' }}
      className={`memos-log-event${firstVersion === clearVersion ? ' is-new' : ''}${item.isError ? ' failed' : ''}`}
    >
      <div className="memos-log-node" aria-hidden="true">
        <span className="memos-log-dot" />
      </div>
      <div className="memos-log-card">
        <div className="memos-log-card-header">
          <button className="memos-log-title" onClick={() => onLocate(item.id)} title={t('playground.logs.locate')}>
            {time && <time>{time}</time>}
            <span>{title}</span>
          </button>
          {item.running && (
            <span className="memos-log-status">
              <LoaderCircle size={12} className="memos-spin" />
              {t('chat.tool.running')}
            </span>
          )}
          {item.isError && <span className="memos-log-status failed">{t('chat.tool.failed')}</span>}
          {processing && (
            <span className="memos-log-status">
              <LoaderCircle size={12} className="memos-spin" />
              {t('playground.logs.extracting')}
            </span>
          )}
        </div>
        {presentation.rows.length ? (
          presentation.rows.map((row, index) => (
            <div className="memos-log-summary-row" key={index}>
              <span>{row.message ? `[${roleLabel(row.message.role, t)}] ${row.text}` : row.text}</span>
              <button
                onClick={() => onDetail(row.entry?.memoryId ? { kind: 'detail', entry: row.entry } : { kind: 'operation', item, row })}
              >
                {t('chat.tools.expand')}
              </button>
            </div>
          ))
        ) : (
          <div className="memos-log-summary-row">
            <span>{t(`playground.logs.status.${presentation.status}`)}</span>
            <button onClick={() => onDetail({ kind: 'operation', item })}>{t('chat.tools.expand')}</button>
          </div>
        )}
      </div>
    </motion.li>
  )
}

function roleLabel(role: string, t: ReturnType<typeof useT>): string {
  return t(
    role === 'user'
      ? 'playground.logs.role.user'
      : role === 'assistant'
        ? 'playground.logs.role.assistant'
        : 'playground.logs.role.message',
  )
}

function MemoryActivityItem({
  activity,
  item,
  onDetail,
}: {
  activity: MemoryLogActivity
  item?: ChatToolItem
  onDetail: (detail: Detail) => void
}) {
  const t = useT()
  const reduce = useReducedMotion()
  const kind = 'kind' in activity ? activity.kind : 'add'
  const memories = kind === 'delete' && 'before' in activity ? activity.before : activity.memories
  const label =
    kind === 'recall'
      ? t('playground.logs.found')(activity.total)
      : kind === 'update'
        ? t('playground.logs.updated')(activity.total)
        : kind === 'delete'
          ? t('playground.logs.deleted')(activity.total)
          : t('playground.logs.added')(activity.total)
  return (
    <motion.li
      className="memos-log-event"
      initial={reduce ? false : { opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: reduce ? 0 : 0.6 }}
    >
      <div className="memos-log-node" aria-hidden="true">
        <span className="memos-log-dot" />
      </div>
      <div className="memos-log-card">
        <div className="memos-log-card-header">
          <time>{new Date(activity.completedAt).toLocaleTimeString([], { hour12: false })}</time>
          <span>
            {activity.status === 'failed'
              ? t('playground.logs.status.failed')
              : activity.status === 'unavailable'
                ? t('playground.logs.memoryUnavailable')
                : label}
          </span>
        </div>
        {memories.map((memory) => (
          <div className="memos-log-summary-row" key={memory.memoryId}>
            <span>{memory.memoryKey ? `${memory.memoryKey}: ${memory.memoryContent}` : memory.memoryContent}</span>
            <button
              onClick={() =>
                onDetail({
                  kind: 'detail',
                  entry: entryFromMemory(memory),
                  snapshot: memory,
                  previous:
                    'before' in activity && activity.kind === 'update'
                      ? activity.before.find((row) => row.memoryId === memory.memoryId)
                      : undefined,
                })
              }
            >
              {t('chat.tools.expand')}
            </button>
          </div>
        ))}
        {item && ['failed', 'unavailable'].includes(activity.status) && (
          <div className="memos-log-summary-row">
            <span>{t(memoryOperationTitle(item))}</span>
            <button onClick={() => onDetail({ kind: 'operation', item })}>{t('chat.tools.expand')}</button>
          </div>
        )}
        {activity.truncated && <p className="memos-log-partial">{t('playground.logs.partial')}</p>}
      </div>
    </motion.li>
  )
}
