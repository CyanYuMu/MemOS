import {
  memo,
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { motion, useReducedMotion } from 'motion/react'
import { useQueryClient } from '@tanstack/react-query'
import { API_BASE } from '../../lib/config'
import { api } from '../../lib/api'
import { errText } from '../../lib/i18n'
import { DUR, EASE_OUT_EXPO } from '../../lib/motion'
import { useLang, useT } from '../../lib/store'
import { Icon } from '../Icon'
import { Button } from '../ui/button'
import { ChatFade, ChatSpinner } from './motion'
import { Markdown } from './Markdown'
import { ToolPanel } from './ToolPanel'
import { ChatActivity, type ActivityItem } from './ChatActivity'
import {
  applyStreamAction,
  asRecord,
  asString,
  errorMessageOf,
  eventTypeOf,
  finishTimeline,
  executionTimeline,
  mergeTimelinePages,
  messagePayload,
  messageUpdatePayload,
  parseChatEvent,
  parseHistoryMessages,
  roleOfMessage,
  textDeltaOf,
  textOfMessage,
  thinkingDeltaOf,
  thinkingPhaseOf,
  type ChatMessageItem,
  type ChatTimelineItem,
  type ChatToolItem,
} from './timeline'
import { useMemoryActivity } from './useMemoryActivity'
import { MemorySnapshot } from './MemorySnapshot'
import { ChatComposerFooter } from './ChatComposerFooter'
import { turnSnapshots, type TurnSnapshot } from './turnSnapshot'
import { guideMemoryProgress, type GuideProgress } from './guideProgress'
import { CopyButton } from '../ui/copy-button'
import { AnswerFeedback } from './AnswerFeedback'
import { parseAssistantResponse } from './assistantResponse'
import { memoryCitationText } from './memoryCitations'
import { ArrowUp, ArrowUpRight, Database, PanelRightOpen } from 'lucide-react'

interface Props {
  projectId: string
  onControls?: (id: string, controls: ChatControls | null) => void
  onProgress?: (progress: GuideProgress) => void
  conversationId: string
}

export interface ChatControls {
  ready: boolean
  send: (text: string) => Promise<boolean>
  retry: () => Promise<unknown>
  showLogs: () => void
  focus: () => void
}

/** agent_end 在工具调用前后会重复出现；没有 agent_settled 时用这个时限兜底收尾 */
const SETTLE_FALLBACK_MS = 12_000
/** 首屏与“加载更早”的窗口大小；后端最大 50 */
const INITIAL_LIMIT = 10
/** 距顶部多少像素内触发自动加载更早的消息 */
const TOP_TRIGGER_PX = 80

/** 与项目和对话绑定的独立会话。挂载即拉历史并建流，卸载即关闭 EventSource。 */
export function ChatSession({ projectId, conversationId, onControls, onProgress }: Props) {
  const queryClient = useQueryClient()
  const t = useT()
  const { lang } = useLang()
  const reduce = useReducedMotion()
  const [logsOpen, setLogsOpen] = useState(false)
  const closeLogs = useCallback(() => setLogsOpen(false), [])
  const [timeline, setTimeline] = useState<ChatTimelineItem[]>([])
  const [historyPhase, setHistoryPhase] = useState<'loading' | 'ready' | 'error'>('loading')
  const [historyError, setHistoryError] = useState<unknown>(null)
  const [hasMore, setHasMore] = useState(false)
  const [historyStart, setHistoryStart] = useState<number | null>(null)
  const [historyTotal, setHistoryTotal] = useState<number | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  /** null = 没有错误；空串 = 服务端报了错但没给原因 */
  const [streamError, setStreamError] = useState<string | null>(null)
  const [sendFailed, setSendFailed] = useState(false)
  const [connectionLost, setConnectionLost] = useState(false)
  const [streamReady, setStreamReady] = useState(false)
  const [running, setRunning] = useState(false)
  const [aborting, setAborting] = useState(false)
  /** POST 在途：防止连点重复受理；生成中仍然允许继续发送（服务端排队）。 */
  const [sending, setSending] = useState(false)
  /** 已受理但还没轮到发送的消息条数 */
  const [queued, setQueued] = useState(0)
  const [compacting, setCompacting] = useState(false)
  /** 服务端在准备进程（首次启动或驱逐后恢复），还没有 agent_start */
  const [starting, setStarting] = useState(false)
  /** 进程重启后遗留的未完成消息；不自动重发，等用户点重试 */
  const [needsRetry, setNeedsRetry] = useState<string | null>(null)
  const [retryable, setRetryable] = useState(false)
  const [draft, setDraft] = useState('')
  const [highlightToolId, setHighlightToolId] = useState<string | null>(null)

  const scrollRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const mountedRef = useRef(true)
  const requestSeq = useRef(0)
  const historyStartRef = useRef<number | null>(null)
  const loadingMoreRef = useRef(false)
  const connectionLostRef = useRef(false)
  const failedPhaseRef = useRef<'initial' | 'earlier' | null>(null)
  /** 前插更早消息时记录插入前的滚动位置，插入后按高度差补回来 */
  const prependAnchorRef = useRef<{ scrollHeight: number; scrollTop: number } | null>(null)
  /** 补偿滚动会触发 scroll 事件；这期间不要把它当成用户滚动，也不许自动滚底 */
  const suppressScrollRef = useRef(false)
  /** 自动滚底的 scroll 事件同样不是用户意图，别拿它反过来关掉跟随 */
  const autoScrollRef = useRef(false)
  /** 只有用户主动滚过，滚回顶部才自动加载更早的消息；挂载时的自动滚底不算 */
  const userScrolledRef = useRef(false)
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const tools = useMemo(() => timeline.filter((item): item is ChatToolItem => item.kind === 'tool'), [timeline])
  const activities = useMemoryActivity(projectId, tools)
  const snapshots = useMemo(() => turnSnapshots(timeline, activities), [timeline, activities])
  const conversation = useMemo(() => {
    const groups: (
      { kind: 'message'; item: ChatMessageItem; turnId?: string } | { kind: 'activity'; id: string; items: ActivityItem[]; turnId?: string }
    )[] = []
    let turnId: string | undefined
    for (const item of executionTimeline(timeline)) {
      if (item.kind === 'message' && item.role === 'user') turnId = item.id
      if (item.kind === 'message') groups.push({ kind: 'message', item, turnId })
      else {
        const previous = groups.at(-1)
        if (previous?.kind === 'activity') previous.items.push(item)
        else groups.push({ kind: 'activity', id: item.id, items: [item], turnId })
      }
    }
    return groups
  }, [timeline])
  const loadedCount =
    historyTotal !== null && historyStart !== null
      ? Math.max(0, historyTotal - historyStart)
      : timeline.filter((item) => item.kind === 'message').length

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (highlightTimer.current) clearTimeout(highlightTimer.current)
    }
  }, [])

  /** 拉最后一页并替换时间线。断线重连也走这里。 */
  const fetchInitial = useCallback(async () => {
    const seq = ++requestSeq.current
    // 替换整条时间线时，之前“前插补偿”的抑制状态不再适用
    suppressScrollRef.current = false

    try {
      const res = await api.chat.messages(projectId, INITIAL_LIMIT, undefined, conversationId)
      if (!mountedRef.current || seq !== requestSeq.current) return
      const list = Array.isArray(res?.messages) ? res.messages : []
      const start = typeof res?.start === 'number' ? res.start : 0
      const total = typeof res?.total === 'number' ? res.total : list.length
      prependAnchorRef.current = null
      userScrolledRef.current = false
      setTimeline(parseHistoryMessages(list, start))
      setHasMore(res?.hasMore === true)
      setHistoryStart(start)
      historyStartRef.current = start
      setHistoryTotal(total)
      setHistoryPhase('ready')
      failedPhaseRef.current = null
    } catch (error: unknown) {
      if (!mountedRef.current || seq !== requestSeq.current) return
      setHistoryError(error)
      setHistoryPhase('error')
      failedPhaseRef.current = 'initial'
    }
  }, [projectId, conversationId])

  /** 用 `before=historyStart` 前插一页更早的消息。 */
  const fetchEarlier = useCallback(() => {
    const before = historyStartRef.current
    if (before === null || before <= 0 || loadingMoreRef.current || !hasMore) return

    loadingMoreRef.current = true
    // 加载更早的消息时不要自动跳到最底部
    stickRef.current = false
    suppressScrollRef.current = true
    const el = scrollRef.current
    if (el) prependAnchorRef.current = { scrollHeight: el.scrollHeight, scrollTop: el.scrollTop }
    setLoadingMore(true)

    const seq = ++requestSeq.current
    void (async () => {
      try {
        const res = await api.chat.messages(projectId, INITIAL_LIMIT, before, conversationId)
        if (!mountedRef.current || seq !== requestSeq.current) return
        const list = Array.isArray(res?.messages) ? res.messages : []
        const start = typeof res?.start === 'number' ? res.start : Math.max(0, before - list.length)
        const older = parseHistoryMessages(list, start)
        setTimeline((items) => mergeTimelinePages(older, items))
        setHistoryStart(start)
        historyStartRef.current = start
        if (typeof res?.total === 'number') setHistoryTotal(res.total)
        setHasMore(res?.hasMore === true && list.length > 0)
        setHistoryPhase('ready')
        failedPhaseRef.current = null
      } catch (error: unknown) {
        if (!mountedRef.current || seq !== requestSeq.current) return
        prependAnchorRef.current = null
        suppressScrollRef.current = false
        setHistoryError(error)
        setHistoryPhase('error')
        failedPhaseRef.current = 'earlier'
      } finally {
        loadingMoreRef.current = false
        if (mountedRef.current) setLoadingMore(false)
      }
    })()
  }, [projectId, conversationId, hasMore])

  const retryHistory = useCallback(() => {
    setHistoryError(null)
    if (failedPhaseRef.current === 'earlier' && timeline.length > 0) {
      setHistoryPhase('ready')
      fetchEarlier()
      return
    }
    prependAnchorRef.current = null
    setHistoryPhase('loading')
    void fetchInitial()
  }, [timeline.length, fetchEarlier, fetchInitial])

  useEffect(() => {
    let cancelled = false
    let es: EventSource | null = null
    let currentAssistantId: string | null = null
    let currentThinkingId: string | null = null
    let settleTimer: ReturnType<typeof setTimeout> | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let retryDelay = 1000
    let firstOpen = true

    const clearSettleTimer = () => {
      if (settleTimer !== null) {
        clearTimeout(settleTimer)
        settleTimer = null
      }
    }

    const settle = () => {
      clearSettleTimer()
      currentAssistantId = null
      currentThinkingId = null
      setRunning(false)
      setCompacting(false)
      setStarting(false)
      setQueued(0)
      setTimeline(finishTimeline)
    }

    const closeThinking = (id: string | null) => {
      // id 为 null 时收掉所有还没结束的思考块
      setTimeline((items) => applyStreamAction(items, { kind: 'thinking_end', id }))
    }

    // 一轮里 agent_end 会出现多次。它只作为兜底：若 agent_settled 没来，
    // 等安静下来再收尾；下一次 agent_start / 消息 / 工具事件会取消这个计时器。
    const armSettleFallback = () => {
      clearSettleTimer()
      settleTimer = setTimeout(() => {
        if (!cancelled) settle()
      }, SETTLE_FALLBACK_MS)
    }

    const openStream = () => {
      if (cancelled) return
      // 上一次的 ES 可能还挂着（重连路径），先关掉再建新的，避免双流重复投递
      es?.close()
      es = new EventSource(
        `${API_BASE}/api/chat/stream?projectId=${encodeURIComponent(projectId)}&conversationId=${encodeURIComponent(conversationId)}`,
      )
      es.onopen = () => {
        if (cancelled) return
        retryDelay = 1000
        const wasLost = connectionLostRef.current
        setConnectionLost(false)
        connectionLostRef.current = false
        // 首次建连时历史已经在前面拉过；只有断线重连才补拉，避免重复请求
        if (!firstOpen && wasLost) {
          setHistoryPhase('loading')
          setHistoryError(null)
          setLoadingMore(false)
          loadingMoreRef.current = false
          void fetchInitial()
        }
        firstOpen = false
      }
      es.onmessage = (raw) => {
        if (cancelled) return
        const event = parseChatEvent(String(raw.data))
        if (!event) return

        switch (event.type) {
          case 'chat_ready': {
            setStreamReady(true)
            setConnectionLost(false)
            const data = asRecord(event.data) ?? {}
            const state = typeof data.state === 'string' ? data.state : ''
            if (typeof data.running === 'boolean') setRunning(data.running)
            if (typeof data.queued === 'number') setQueued(data.queued)
            if (typeof data.compacting === 'boolean') setCompacting(data.compacting)
            // 断线重连后把「启动/压缩」也恢复出来，否则只能看到一个空闲会话
            setStarting(state === 'starting')
            if (typeof data.needsRetry === 'number') setNeedsRetry(data.needsRetry > 0 ? '' : null)
            break
          }
          case 'chat_status': {
            const data = asRecord(event.data) ?? {}
            const state = typeof data.state === 'string' ? data.state : ''
            if (typeof data.queued === 'number') setQueued(data.queued)
            if (typeof data.running === 'boolean') setRunning(data.running)
            if (typeof data.compacting === 'boolean') setCompacting(data.compacting)
            if (state === 'starting') setStarting(true)
            if (state === 'running' || state === 'compacting') setStarting(false)
            if (state === 'idle') {
              setRunning(false)
              setCompacting(false)
              setStarting(false)
            }
            if (typeof data.needsRetry === 'number') {
              // 重试列表清空后提示要收起来，否则它会一直盖住后面的运行状态
              const pending = data.needsRetry
              setNeedsRetry((current) => (pending > 0 ? (current ?? '') : null))
            }
            if (state === 'needs_retry') {
              const message = asRecord(data.message)
              if (typeof message?.text === 'string') setNeedsRetry(message.text)
              else setNeedsRetry((current) => current ?? '')
            }
            break
          }
          case 'message_accepted': {
            const data = asRecord(event.data) ?? {}
            if (data.queued === true) setQueued((n) => Math.max(n, 1))
            setNeedsRetry(null)
            // 多标签页：本页发的已经乐观插入，别的页发的按服务端 id 补进来。
            const id = typeof data.id === 'string' ? data.id : ''
            const text = typeof data.text === 'string' ? data.text : ''
            if (id && text) {
              setTimeline((items) => {
                if (items.some((item) => item.id === `accepted_${id}`)) return items
                // 本页刚发的消息已乐观插入、但还没拿到服务端 id（广播常常先于
                // 202 响应到达）：把服务端 id 贴到那条上，而不是丢弃事件——
                // 丢弃会连带吞掉别的标签页发的同文消息。
                const pendingIdx = items.findIndex(
                  (item) => item.kind === 'message' && item.role === 'user' && item.text === text && item.id.startsWith('user_'),
                )
                if (pendingIdx >= 0) {
                  const next = items.slice()
                  const current = next[pendingIdx]
                  if (current.kind !== 'message') return items
                  next[pendingIdx] = { ...current, id: `accepted_${id}`, submissionId: id, origin: 'stream' }
                  return next
                }
                return [
                  ...items,
                  { kind: 'message', id: `accepted_${id}`, role: 'user', text, streaming: false, submissionId: id, origin: 'stream' },
                ]
              })
            }
            break
          }
          case 'compaction_start':
            setCompacting(true)
            break
          case 'compaction_end':
            setCompacting(false)
            break
          case 'chat_error': {
            clearSettleTimer()
            currentAssistantId = null
            currentThinkingId = null
            setStreamError(errorMessageOf(event.data))
            setRetryable(asRecord(event.data)?.retryable === true)
            setRunning(false)
            setCompacting(false)
            setStarting(false)
            setQueued(0)
            setTimeline(finishTimeline)
            break
          }
          case 'conversation_title':
            void queryClient.invalidateQueries({ queryKey: ['chat.conversations', projectId] })
            break
          case 'ping':
            break
          case 'agent_start':
          case 'turn_start':
            clearSettleTimer()
            setStarting(false)
            setRunning(true)
            break
          case 'agent_end':
            // 一轮里会重复出现：只启动兜底计时，真正收尾等 agent_settled
            armSettleFallback()
            break
          case 'agent_settled':
            settle()
            break
          case 'turn_end':
            clearSettleTimer()
            break
          case 'message_start': {
            clearSettleTimer()
            const payload = messagePayload(event.data)
            const role = roleOfMessage(payload)
            if (role && role !== 'assistant') break
            // 重连后可能错过 agent_start，用任何一条助手事件把停止按钮拉起来
            setRunning(true)
            if (currentThinkingId) {
              closeThinking(null)
              currentThinkingId = null
            }
            if (!currentAssistantId) currentAssistantId = `assistant_${event.seq}`
            const id = currentAssistantId
            const text = textOfMessage(payload)
            // 不在这里建空气泡：message_start 之后通常先来 thinking，等第一个
            // text_delta 再插入，思考块才会排在正文前面。
            if (text)
              setTimeline((items) => applyStreamAction(items, { submissionId: event.submissionId, kind: 'assistant_start', id, text }))
            break
          }
          case 'message_update': {
            clearSettleTimer()
            const payload = messageUpdatePayload(event.data)
            setRunning(true)

            const phase = thinkingPhaseOf(payload)
            if (phase === 'start') {
              if (!currentThinkingId) currentThinkingId = `thinking_${event.seq}`
              const id = currentThinkingId
              setTimeline((items) => applyStreamAction(items, { submissionId: event.submissionId, kind: 'thinking_start', id }))
              break
            }
            if (phase === 'delta') {
              if (!currentThinkingId) currentThinkingId = `thinking_${event.seq}`
              const id = currentThinkingId
              const delta = thinkingDeltaOf(payload)
              if (!delta) break
              setTimeline((items) => applyStreamAction(items, { submissionId: event.submissionId, kind: 'thinking_delta', id, delta }))
              break
            }
            if (phase === 'end') {
              const id = currentThinkingId
              currentThinkingId = null
              closeThinking(id)
              break
            }

            // text_start 之前如果 thinking_end 丢了，先把思考块收掉，避免一直转圈
            if (eventTypeOf(payload) === 'text_start' && currentThinkingId) {
              closeThinking(null)
              currentThinkingId = null
            }
            const delta = textDeltaOf(payload)
            if (!delta) break
            if (!currentAssistantId) currentAssistantId = `assistant_${event.seq}`
            const id = currentAssistantId
            setTimeline((items) => applyStreamAction(items, { submissionId: event.submissionId, kind: 'assistant_delta', id, delta }))
            break
          }
          case 'message_end': {
            clearSettleTimer()
            const payload = messagePayload(event.data)
            const role = roleOfMessage(payload)
            // message_end 时把所有没收到 thinking_end 的块一并落定
            closeThinking(null)
            currentThinkingId = null
            if (role && role !== 'assistant') {
              currentAssistantId = null
              break
            }
            const text = textOfMessage(payload)
            const id = currentAssistantId
            currentAssistantId = null
            setTimeline((items) =>
              applyStreamAction(items, {
                submissionId: event.submissionId,
                kind: 'assistant_end',
                id,
                fallbackId: `assistant_${event.seq}`,
                text,
                timestamp:
                  asString(asRecord(payload)?.timestamp) ??
                  (typeof asRecord(payload)?.timestamp === 'number' ? (asRecord(payload)?.timestamp as number) : undefined),
              }),
            )
            break
          }
          case 'tool_execution_start':
          case 'tool_execution_update':
          case 'tool_execution_end': {
            clearSettleTimer()
            setRunning(true)
            // 工具调用开始时，当前助手消息的思考阶段一定已经结束；上游漏发
            // thinking_end 时在这里兜底收尾，别让“正在思考…”一直转
            if (currentThinkingId) {
              closeThinking(null)
              currentThinkingId = null
            }
            const kind =
              event.type === 'tool_execution_start'
                ? ('tool_start' as const)
                : event.type === 'tool_execution_update'
                  ? ('tool_update' as const)
                  : ('tool_end' as const)
            const receivedAt = Date.now()
            setTimeline((items) =>
              applyStreamAction(items, {
                submissionId: event.submissionId,
                kind,
                seed: String(event.seq),
                data: event.data,
                timestamp: receivedAt,
              }),
            )
            break
          }
          default:
            break
        }
      }
      es.onerror = () => {
        if (cancelled) return
        setStreamReady(false)
        connectionLostRef.current = true
        setConnectionLost(true)
        // 浏览器只在网络层面错误时自己重连；服务端在重启窗口里回 502/503（非
        // text/event-stream）会直接把连接判死（readyState=CLOSED），不再重试。
        // 这种情况必须由页面自己按退避重建，否则会永久停在「正在重连」。
        if (es?.readyState === EventSource.CLOSED) {
          es.close()
          if (retryTimer !== null) return
          retryTimer = setTimeout(() => {
            retryTimer = null
            retryDelay = Math.min(retryDelay * 2, 10_000)
            openStream()
          }, retryDelay)
        }
      }
    }

    // 历史先落地再建流：避免用户刚发出一条消息时，晚到的历史把本地时间线覆盖掉。
    // 历史失败只记错误，仍然建流——聊天不该被一段旧记录挡住。
    // fetchInitial 的 setState 都在 await 之后；静态检查识别不了这层异步边界。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchInitial().finally(() => {
      openStream()
    })

    return () => {
      cancelled = true
      clearSettleTimer()
      if (retryTimer !== null) clearTimeout(retryTimer)
      es?.close()
    }
  }, [projectId, conversationId, fetchInitial, queryClient])

  useEffect(() => {
    if (suppressScrollRef.current || !stickRef.current) return
    const el = scrollRef.current
    if (!el) return
    // 滚底后浏览器会回发一个 scroll 事件；内容可能还在做高度动画，
    // 那时算出来的“距底”已经变大，若当成用户上滑就会永久关掉跟随。
    autoScrollRef.current = true
    el.scrollTop = el.scrollHeight
    requestAnimationFrame(() => {
      autoScrollRef.current = false
    })
  }, [timeline, historyPhase, streamError, connectionLost])

  // 前插更早的消息后，用高度差把视口钉在原来的位置，避免内容整体往下跳。
  useLayoutEffect(() => {
    const anchor = prependAnchorRef.current
    if (!anchor) return
    prependAnchorRef.current = null
    const el = scrollRef.current
    if (el) el.scrollTop = anchor.scrollTop + (el.scrollHeight - anchor.scrollHeight)
    // 补偿滚动自身会发 scroll 事件；等这一帧过去再恢复，
    // 否则会被 onScroll 误判成“用户滚到了底部”，紧接着又自动滚底。
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        suppressScrollRef.current = false
      })
    })
  }, [timeline])

  const resizeInput = useCallback(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [])

  useEffect(() => {
    resizeInput()
  }, [draft, resizeInput])

  const markUserScroll = () => {
    userScrolledRef.current = true
  }

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    if (suppressScrollRef.current) return
    if (autoScrollRef.current) {
      autoScrollRef.current = false
      return
    }
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 72
    // 生成过程中不加载更早的历史：避免发送那一刻内容还很短，程序化滚动
    // 误触发前插并把“跟随到底部”的状态关掉。首次挂载的自动滚底也不算
    // 用户滚动，否则 StrictMode 下会多拉一页。
    if (userScrolledRef.current && !running && el.scrollTop < TOP_TRIGGER_PX && historyPhase === 'ready') fetchEarlier()
  }

  /**
   * 受理一条消息。生成中也可以发：服务端会排队并在当前轮结束后自动发送，
   * 返回的 `queued` 决定是否显示排队状态。POST 在途时忽略重复点击。
   */
  const submit = useCallback(
    async (text: string) => {
      const localId = `user_${Date.now()}`
      setDraft('')
      setStreamError(null)
      setSendFailed(false)
      setRetryable(false)
      setNeedsRetry(null)
      setSending(true)
      setRunning(true)
      stickRef.current = true
      setTimeline((items) => [...items, { kind: 'message', id: localId, role: 'user', text, streaming: false, origin: 'stream' }])

      try {
        const result = await api.chat.send(projectId, text, conversationId)
        // 若广播已经先到并把这条乐观消息打了 id，保留；没有则保持本地 id 即可，
        // 下一次事件会按服务端 id 对上。
        setTimeline((items) =>
          items.map((item) =>
            item.id === localId ? { ...item, id: `accepted_${result.id}`, submissionId: result.id, origin: 'stream' as const } : item,
          ),
        )
        if (result.queued) setQueued((n) => Math.max(n, 1))
        return true
      } catch (error) {
        // 没发出去就别在时间线里留下一条会误导人的“已发送”消息
        setTimeline((items) => items.filter((item) => item.id !== localId))
        setDraft(text)
        setRunning(false)
        setSendFailed(true)
        setStreamError(errText(lang, error))
        return false
      } finally {
        setSending(false)
      }
    },
    [lang, projectId, conversationId],
  )

  const send = useCallback(
    async (override?: string) => {
      const text = (override ?? draft).trim()
      if (!text || sending || aborting || loadingMore || historyPhase === 'loading') return false
      return submit(text)
    },
    [aborting, draft, historyPhase, loadingMore, sending, submit],
  )

  /**
   * 重试。
   *
   * - 发送失败（POST 没成功）：草稿已经回填，直接再发一次；
   * - 服务端有遗留的待重试消息：交给服务端按它记下的那一条重投，不能拿时间线里
   *   最后一条用户消息去猜——那可能是一条已经成功回答过的问题。
   */
  const retryLast = useCallback(async () => {
    setStreamError(null)
    setSendFailed(false)
    setRetryable(false)
    if (sendFailed) {
      await send()
      return
    }
    try {
      // 重投的正文在服务端，本页时间线里没有对应条目：让 message_accepted 正常插入。
      const result = await api.chat.retry(projectId, conversationId)
      if (result.queued) setQueued((n) => Math.max(n, 1))
      setRunning(true)
      return true
    } catch (error) {
      setStreamError(errText(lang, error))
    }
  }, [lang, projectId, conversationId, send, sendFailed])

  const abort = useCallback(async () => {
    if (aborting) return
    setAborting(true)
    try {
      await api.chat.abort(projectId, conversationId)
      setRunning(false)
      setTimeline(finishTimeline)
    } catch (error) {
      setStreamError(errText(lang, error))
    } finally {
      setAborting(false)
    }
  }, [aborting, lang, projectId, conversationId])

  // running 时 Esc 停止生成；输入框里的 Esc 也接收，和常见聊天工具一致
  useEffect(() => {
    if (!running) return
    const onKey = (event: KeyboardEvent) => {
      if (
        event.key !== 'Escape' ||
        event.defaultPrevented ||
        document.querySelector('.driver-popover') ||
        document.querySelector('[aria-modal="true"]')
      )
        return
      event.preventDefault()
      void abort()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [running, abort])

  const locateTool = useCallback(
    (toolId: string) => {
      setLogsOpen(false)
      stickRef.current = false
      setHighlightToolId(toolId)
      if (highlightTimer.current) clearTimeout(highlightTimer.current)
      highlightTimer.current = setTimeout(() => {
        if (mountedRef.current) setHighlightToolId((current) => (current === toolId ? null : current))
      }, 2400)
      requestAnimationFrame(() => {
        const container = scrollRef.current
        const card = document.getElementById(`tool-${toolId}`)
        if (container && card) {
          const top =
            card.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop - container.clientHeight / 3
          container.scrollTo({ top, behavior: reduce ? 'auto' : 'smooth' })
        }
      })
    },
    [reduce],
  )

  const onKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    // 输入法组词时的回车不发送
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      void send()
    }
  }

  const guideProgress = useMemo<GuideProgress>(
    () => ({
      conversationId,
      ready: historyPhase === 'ready' && streamReady,
      busy: running || sending || starting || compacting || queued > 0 || needsRetry !== null,
      needsRetry: needsRetry !== null,
      hasAnswer: timeline.some((item) => item.kind === 'message' && item.role === 'assistant' && !!item.text && !item.streaming),
      error: streamError ?? (historyPhase === 'error' ? errText(lang, historyError) : null),
      ...guideMemoryProgress(timeline, activities),
    }),
    [
      conversationId,
      historyPhase,
      streamReady,
      running,
      sending,
      starting,
      compacting,
      queued,
      timeline,
      activities,
      streamError,
      lang,
      historyError,
      needsRetry,
    ],
  )
  useEffect(() => {
    onProgress?.(guideProgress)
  }, [onProgress, guideProgress])
  useEffect(() => {
    onControls?.(conversationId, {
      ready: historyPhase === 'ready' && streamReady,
      send,
      retry: async () => {
        if (historyPhase === 'error' || !streamReady) {
          retryHistory()
          return
        }
        return retryLast()
      },
      showLogs: () => {
        if (!window.matchMedia('(min-width: 1100px)').matches) setLogsOpen(true)
      },
      focus: () => inputRef.current?.focus(),
    })
    return () => onControls?.(conversationId, null)
  }, [onControls, conversationId, historyPhase, streamReady, send, retryLast, retryHistory])

  const canSend = draft.trim().length > 0 && !sending && !aborting && !loadingMore && historyPhase !== 'loading'

  const isEmpty = historyPhase === 'ready' && timeline.length === 0

  return (
    <div className={`memos-session${isEmpty ? ' is-empty' : ''}`}>
      <div className="memos-conversation" id="chat-thread">
        <button
          id="chat-log-toggle"
          className="memos-log-toggle memos-icon-button"
          aria-label={t('playground.logs.open')}
          title={t('playground.logs.open')}
          aria-expanded={logsOpen}
          onClick={() => setLogsOpen((value) => !value)}
        >
          <PanelRightOpen size={20} />
        </button>
        {streamError !== null && (
          <ChatFade
            className="flex shrink-0 items-start gap-2 border-b border-danger/30 bg-danger-soft px-4 py-2 text-xs text-danger"
            y={-4}
          >
            <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
            <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{streamError || t('common.failed')}</span>
            {(sendFailed || retryable) && (
              <button
                type="button"
                className="shrink-0 cursor-pointer text-xs font-medium underline"
                onClick={() => void (sendFailed ? send() : retryLast())}
              >
                {t('common.retry')}
              </button>
            )}
            <button
              type="button"
              aria-label={t('common.close')}
              className="shrink-0 cursor-pointer text-danger/80 transition-colors hover:text-danger"
              onClick={() => {
                setStreamError(null)
                setSendFailed(false)
              }}
            >
              <Icon name="i-lucide:x" className="size-4" />
            </button>
          </ChatFade>
        )}

        {connectionLost && streamError === null && (
          <ChatFade
            className="flex shrink-0 items-center gap-2 border-b border-warning/30 bg-warning-soft px-4 py-2 text-xs text-warning"
            y={-4}
          >
            <ChatSpinner />
            <span className="min-w-0 flex-1">{t('chat.stream.reconnecting')}</span>
          </ChatFade>
        )}

        {historyPhase === 'error' && (
          <ChatFade
            className="flex shrink-0 items-start gap-2 border-b border-warning/30 bg-warning-soft px-4 py-2 text-xs text-warning"
            y={-4}
          >
            <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
            <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{t('chat.history.error')(errText(lang, historyError))}</span>
            <button type="button" className="shrink-0 cursor-pointer font-medium underline" onClick={retryHistory}>
              {t('common.retry')}
            </button>
          </ChatFade>
        )}

        <div id="chat-conversation" className="memos-message-region">
          {loadingMore && (
            <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex justify-center pt-2">
              <span className="flex items-center gap-1.5 rounded-full border border-line bg-surface/95 px-2.5 py-1 text-[11px] text-ink-muted shadow-2xs">
                <ChatSpinner />
                {t('chat.history.loadingEarlier')}
              </span>
            </div>
          )}
          <div
            ref={scrollRef}
            onScroll={onScroll}
            onWheel={markUserScroll}
            onTouchMove={markUserScroll}
            onPointerDown={markUserScroll}
            onKeyDown={(event) => {
              if (event.key === 'ArrowUp' || event.key === 'PageUp' || event.key === 'Home') markUserScroll()
            }}
            className="chat-scroll memos-message-scroll"
          >
            {timeline.length === 0 ? (
              historyPhase === 'loading' ? (
                <HistorySkeleton />
              ) : historyPhase === 'ready' ? (
                <ChatFade className="memos-hello">
                  <h1>{t('playground.hero.title')}</h1>
                </ChatFade>
              ) : null
            ) : (
              <>
                {(hasMore || loadedCount > INITIAL_LIMIT) && (
                  <div className="mb-3 flex flex-col items-center gap-0.5 text-center text-[11px] text-ink-faint">
                    {hasMore ? (
                      <>
                        <span>{t('chat.history.showingRecent')(loadedCount)}</span>
                        <span>{t('chat.history.scrollForMore')}</span>
                      </>
                    ) : (
                      <span>{t('chat.history.showingAll')(loadedCount)}</span>
                    )}
                  </div>
                )}
                <div className="memos-conversation-items">
                  {conversation.map((group) =>
                    group.kind === 'message' ? (
                      <MessageBubble
                        conversationId={conversationId}
                        key={group.item.id}
                        item={group.item}
                        projectId={projectId}
                        snapshot={snapshots.get(group.turnId ?? group.item.id)}
                        showQuestions={
                          group.item.id === timeline.filter((item) => item.kind === 'message' && item.role === 'assistant').at(-1)?.id
                        }
                        onQuestion={send}
                      />
                    ) : (
                      <ChatActivity
                        key={group.id}
                        items={group.items}
                        highlightedId={highlightToolId}
                        memory={{ projectId, snapshot: group.turnId ? snapshots.get(group.turnId) : undefined }}
                      />
                    ),
                  )}
                </div>
              </>
            )}
          </div>
        </div>

        <div className="memos-composer-dock">
          {(compacting || starting || queued > 0 || needsRetry !== null) && (
            <div className="mb-1.5 flex items-center gap-2 px-1 text-[11px] text-ink-muted" role="status">
              <ChatSpinner />
              <span className="min-w-0 flex-1 truncate">
                {compacting
                  ? t('chat.status.compacting')
                  : starting
                    ? t('chat.status.starting')
                    : queued > 0
                      ? t('chat.status.queued')(queued)
                      : t('chat.status.needsRetry')}
              </span>
              {needsRetry !== null && (
                <button type="button" className="shrink-0 cursor-pointer font-medium underline" onClick={() => void retryLast()}>
                  {t('chat.status.retry')}
                </button>
              )}
            </div>
          )}
          <ChatFade className="chat-composer memos-composer" y={4}>
            <div id="chat-composer" className="memos-composer-inner">
              <textarea
                ref={inputRef}
                id="chat-input"
                rows={1}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={onKeyDown}
                placeholder={t('chat.input.placeholder')}
                aria-label={t('chat.input.placeholder')}
                className="memos-chat-input"
              />
              <div className="memos-composer-bottom">
                <a href="/cn/memoryList" className="memos-memory-link">
                  <Database size={15} />
                  {t('playground.composer.memory')}
                  <ArrowUpRight size={13} />
                </a>
                <div className="memos-send-actions">
                  {running && (
                    <Button variant="destructive" size="sm" loading={aborting} className="chat-action-btn" onClick={() => void abort()}>
                      <Icon name="i-lucide:pause" className="size-4" />
                      {t('chat.input.stop')}
                    </Button>
                  )}
                  <button
                    className="memos-send"
                    aria-label={t('chat.input.send')}
                    title={t('chat.input.send')}
                    disabled={!canSend}
                    onClick={() => void send()}
                  >
                    <ArrowUp size={20} />
                  </button>
                </div>
              </div>
            </div>
          </ChatFade>
          {isEmpty && (
            <div id="chat-suggestions" className="memos-suggestions">
              {(['playground.suggestion.remember', 'playground.suggestion.recall', 'playground.suggestion.recommend'] as const).map(
                (key) => (
                  <button
                    key={key}
                    onClick={() => {
                      setDraft(t(key))
                      inputRef.current?.focus()
                    }}
                  >
                    {t(key)}
                    <ArrowUpRight size={13} />
                  </button>
                ),
              )}
            </div>
          )}
          <ChatComposerFooter />
        </div>
      </div>
      <ToolPanel projectId={projectId} items={tools} activities={activities} onLocate={locateTool} open={logsOpen} onClose={closeLogs} />
    </div>
  )
}

const MessageBubble = memo(function MessageBubble({
  item,
  conversationId,
  projectId,
  onQuestion,
  showQuestions,
  snapshot,
}: {
  item: ChatMessageItem
  snapshot?: TurnSnapshot
  projectId: string
  conversationId: string
  showQuestions: boolean
  onQuestion: (question: string) => Promise<boolean>
}) {
  const t = useT()
  const reduce = useReducedMotion()
  const isUser = item.role === 'user'
  // 流式期间把 Markdown 解析放到低优先级；结束那一帧直接用完整文本，避免落后半拍
  const deferredText = useDeferredValue(item.text)
  const parsed = parseAssistantResponse(item.streaming ? deferredText : item.text)
  const renderedText = parsed.text
  const copiedText = memoryCitationText(parsed.text, snapshot)

  return (
    <motion.div
      className={`memos-message ${isUser ? 'is-user' : 'is-assistant'}`}
      initial={reduce ? { opacity: 0 } : { opacity: 0, y: isUser ? 10 : 6, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={reduce ? { duration: DUR.fast } : { duration: DUR.base, ease: EASE_OUT_EXPO }}
    >
      {isUser ? (
        snapshot ? (
          <MemorySnapshot projectId={projectId} snapshot={snapshot}>
            {(trigger) => (
              <div className="chat-bubble memos-bubble">
                <div className="whitespace-pre-wrap break-words text-sm leading-6">{item.text}</div>
                {trigger}
              </div>
            )}
          </MemorySnapshot>
        ) : (
          <div className="chat-bubble memos-bubble">
            <div className="whitespace-pre-wrap break-words text-sm leading-6">{item.text}</div>
          </div>
        )
      ) : (
        <div className="chat-bubble memos-bubble">
          <div className="text-sm leading-6">
            <Markdown text={renderedText} memory={{ projectId, snapshot }} />
            {item.streaming && (
              <div className="mt-1.5">
                <TypingDots label={t('common.loading')} />
              </div>
            )}
          </div>
        </div>
      )}
      {!isUser && !item.streaming && item.text && (
        <div className="memos-message-actions">
          <AnswerFeedback
            conversationId={conversationId}
            projectId={projectId}
            identity={String(item.timestamp ?? item.id)}
            text={parsed.text}
            displayText={copiedText}
            onCorrect={onQuestion}
          />
          <CopyButton text={copiedText} />
        </div>
      )}
      {!isUser && !item.streaming && showQuestions && parsed.questions.length > 0 && (
        <div className="memos-reference-questions">
          <h3>{t('chat.referenceQuestions')}</h3>
          <div>
            {parsed.questions.map((question) => (
              <button type="button" key={question} onClick={() => void onQuestion(question)}>
                {question}
              </button>
            ))}
          </div>
        </div>
      )}
    </motion.div>
  )
})

function TypingDots({ label }: { label: string }) {
  const reduce = useReducedMotion()
  return (
    <span className="inline-flex items-center gap-1" role="status" aria-label={label}>
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          className={`size-1.5 rounded-full bg-ink-faint ${reduce ? 'opacity-60' : 'animate-pulse'}`}
          style={reduce ? undefined : { animationDelay: `${index * 120}ms` }}
        />
      ))}
    </span>
  )
}

function HistorySkeleton() {
  const t = useT()
  const reduce = useReducedMotion()
  const pulse = reduce ? 'opacity-60' : 'animate-pulse'
  return (
    <div className="space-y-4" aria-busy="true" aria-label={t('common.loading')}>
      <div className="flex justify-end">
        <div className={`h-16 w-2/5 rounded-2xl bg-surface-muted ${pulse}`} />
      </div>
      <div className={`h-20 w-3/5 rounded-2xl bg-surface-muted ${pulse}`} />
      <div className="flex justify-end">
        <div className={`h-12 w-1/3 rounded-2xl bg-surface-muted ${pulse}`} />
      </div>
    </div>
  )
}
