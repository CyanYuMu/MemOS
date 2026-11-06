import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useInfiniteQuery } from '@tanstack/react-query'
import * as Popover from '@radix-ui/react-popover'
import { Check, History, LoaderCircle, MessageSquarePlus } from 'lucide-react'
import { api } from '../../lib/api'
import { errText } from '../../lib/i18n'
import { useLang, useT } from '../../lib/store'
import { ChatSession, type ChatControls } from './ChatSession'
import { ChatOnboarding } from './ChatOnboarding'
import type { GuideProgress } from './guideProgress'
import { usePlatformGuide } from '../../lib/onboarding'

/** URL保留独立对话选择，切换项目时不复用前一项目的会话ID。 */
export function ChatConversations({ projectId, guideRequest }: { projectId: string; guideRequest: number }) {
  const platform = usePlatformGuide()
  const managed = platform?.state?.status === 'active'
  const embedded = managed && platform.state?.step === 3
  const t = useT()
  const { lang } = useLang()
  const [params, setParams] = useSearchParams()
  const conversationId = params.get('chatProject') === projectId ? params.get('conversationId') || '' : ''
  const [initialGuideRequest] = useState(guideRequest)
  const [guideActive, setGuideActive] = useState(false)
  const [guideProgress, setGuideProgress] = useState<GuideProgress | null>(null)
  const readySession = useRef<{ id: string; controls: ChatControls } | null>(null)
  const waiter = useRef<{
    id: string
    resolve: (controls: ChatControls) => void
    reject: (error: Error) => void
    timer: ReturnType<typeof setTimeout>
  } | null>(null)
  const flowEpoch = useRef(0)
  const guidedRooms = useRef(new Map<string, { id: string; submitted: boolean }>())
  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)
  const mounted = useRef(true)
  const selectionEpoch = useRef(0)
  useEffect(() => {
    selectionEpoch.current += 1
  }, [conversationId])
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      flowEpoch.current += 1
      if (waiter.current) {
        clearTimeout(waiter.current.timer)
        waiter.current.reject(new Error('cancelled'))
        waiter.current = null
      }
    }
  }, [])
  const history = useInfiniteQuery({
    queryKey: ['chat.conversations', projectId],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.chat.conversations(projectId, pageParam),
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, page) => n + page.records.length, 0)
      return last.records.length > 0 && loaded < last.total ? loaded : undefined
    },
    retry: false,
  })
  const rows = history.data?.pages.flatMap((page) => page.records) ?? []
  const select = useCallback(
    (id: string) => {
      selectionEpoch.current += 1
      setParams((previous) => {
        const next = new URLSearchParams(previous)
        if (id) {
          next.set('chatProject', projectId)
          next.set('conversationId', id)
        } else {
          next.delete('chatProject')
          next.delete('conversationId')
        }
        return next
      })
      setOpen(false)
    },
    [projectId, setParams],
  )
  async function create() {
    if (pending.current) return
    pending.current = true
    setCreating(true)
    setError('')
    const requestedAt = selectionEpoch.current
    try {
      const room = await api.chat.newConversation(projectId)
      if (!mounted.current) return
      if (selectionEpoch.current === requestedAt) select(room.id)
      void history.refetch()
    } catch (e) {
      if (mounted.current) setError(errText(lang, e))
    } finally {
      pending.current = false
      if (mounted.current) setCreating(false)
    }
  }
  const onControls = useCallback((id: string, controls: ChatControls | null) => {
    if (controls) readySession.current = { id, controls }
    else if (readySession.current?.id === id) readySession.current = null
    const waiting = waiter.current
    if (controls?.ready && waiting?.id === id) {
      clearTimeout(waiting.timer)
      waiter.current = null
      waiting.resolve(controls)
    }
  }, [])
  const onGuideActive = useCallback((active: boolean) => {
    setGuideActive(active)
    if (!active) {
      flowEpoch.current += 1
      if (waiter.current) {
        clearTimeout(waiter.current.timer)
        waiter.current.reject(new Error('cancelled'))
        waiter.current = null
      }
    }
  }, [])
  const onLogs = useCallback(() => readySession.current?.controls.showLogs(), [])
  const onReturn = useCallback(() => readySession.current?.controls.focus(), [])
  const onRetry = useCallback(() => readySession.current?.controls.retry(), [])
  const onRetryWrite = useCallback(async () => {
    const session = readySession.current
    if (!session?.controls.ready || !(await session.controls.send(t('chat.guide.retryWriteMessage')))) {
      throw new Error(t('chat.guide.retryNotReady'))
    }
  }, [t])
  async function beginGuide(text: string, title: string, purpose: 'intro' | 'recall') {
    if (pending.current) throw new Error(t('common.loading'))
    pending.current = true
    setCreating(true)
    const epoch = flowEpoch.current
    const key = JSON.stringify([guideRequest, purpose, text])
    try {
      let room = guidedRooms.current.get(key)
      if (!room) {
        const created = await api.chat.newConversation(projectId, title)
        room = { id: created.id, submitted: false }
        guidedRooms.current.set(key, room)
        void history.refetch()
      }
      if (!mounted.current || epoch !== flowEpoch.current) throw new Error('cancelled')
      select(room.id)
      if (room.submitted) return room.id
      const id = room.id
      const controls =
        readySession.current?.id === id && readySession.current.controls.ready
          ? readySession.current.controls
          : await new Promise<ChatControls>((resolve, reject) => {
              const timer = setTimeout(() => {
                if (waiter.current?.id === id) waiter.current = null
                reject(new Error(t('chat.guide.prepareFailed')))
              }, 20000)
              waiter.current = { id, resolve, reject, timer }
            })
      if (!mounted.current || epoch !== flowEpoch.current) throw new Error('cancelled')
      if (!(await controls.send(text))) throw new Error(t('chat.guide.sendFailed'))
      room.submitted = true
      return id
    } finally {
      pending.current = false
      if (mounted.current) setCreating(false)
    }
  }
  return (
    <div className="memos-conversation-workspace">
      <div className="memos-conversation-toolbar">
        <button
          id="chat-new-conversation"
          className="memos-conversation-control"
          onClick={() => void create()}
          disabled={creating || guideActive}
        >
          {creating ? <LoaderCircle size={16} className="animate-spin" /> : <MessageSquarePlus size={16} />}
          {t('chat.conversations.new')}
        </button>
        <Popover.Root
          open={open}
          onOpenChange={(value) => {
            setOpen(value)
            if (value) void history.refetch()
          }}
        >
          <Popover.Trigger asChild>
            <button
              id="chat-current-conversation"
              className="memos-conversation-control"
              disabled={guideActive || creating}
              aria-label={t('chat.conversations.history')}
            >
              <History size={16} />
              {rows.find((row) => row.id === conversationId)?.title ||
                (conversationId ? t('chat.conversations.untitled') : t('chat.conversations.history'))}
            </button>
          </Popover.Trigger>
          <Popover.Portal>
            <Popover.Content className="memos-history-menu" sideOffset={8} align="start" aria-label={t('chat.conversations.history')}>
              <h3>{t('chat.conversations.history')}</h3>
              <button className="memos-history-row" onClick={() => select('')} aria-current={!conversationId ? 'true' : undefined}>
                <span>{t('chat.conversations.previous')}</span>
                {!conversationId && <Check size={15} />}
              </button>
              {rows.map((row) => (
                <button
                  key={row.id}
                  className="memos-history-row"
                  onClick={() => select(row.id)}
                  aria-current={conversationId === row.id ? 'true' : undefined}
                >
                  <span>
                    {row.title || t('chat.conversations.untitled')}
                    <time>{new Date(row.createdAt).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US')}</time>
                  </span>
                  {conversationId === row.id && <Check size={15} />}
                </button>
              ))}
              {history.isPending && <p role="status">{t('common.loading')}</p>}
              {history.isError && (
                <div role="alert">
                  <p>{errText(lang, history.error)}</p>
                  <button onClick={() => void (history.isFetchNextPageError ? history.fetchNextPage() : history.refetch())}>
                    {t('common.retry')}
                  </button>
                </div>
              )}
              {history.hasNextPage && (
                <button className="memos-history-more" disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>
                  {history.isFetchingNextPage ? t('common.loading') : t('chat.conversations.more')}
                </button>
              )}
            </Popover.Content>
          </Popover.Portal>
        </Popover.Root>
        {error && <span role="alert">{error}</span>}
      </div>
      <ChatSession
        key={`${projectId}:${conversationId}`}
        projectId={projectId}
        conversationId={conversationId}
        onControls={onControls}
        onProgress={setGuideProgress}
      />
      {(!managed || embedded) && (
        <ChatOnboarding
          key={`${projectId}:${guideRequest}:${embedded ? 'platform' : 'chat'}`}
          embedded={embedded}
          savedCheckpoint={embedded ? platform.state?.chat : undefined}
          externalBusy={!!embedded && platform.busy}
          onCheckpoint={embedded ? (checkpoint) => platform.change('checkpoint', checkpoint) : undefined}
          onComplete={embedded ? () => platform.change('advance') : undefined}
          onDismiss={embedded ? () => platform.change('dismiss') : undefined}
          projectId={projectId}
          replay={!!embedded || guideRequest > initialGuideRequest}
          conversationId={conversationId}
          progress={guideProgress}
          onBegin={beginGuide}
          onSelect={select}
          onActive={onGuideActive}
          onLogs={onLogs}
          onReturn={onReturn}
          onRetry={onRetry}
          onRetryWrite={onRetryWrite}
          onBack={embedded ? () => platform.change('back') : undefined}
        />
      )}
    </div>
  )
}
