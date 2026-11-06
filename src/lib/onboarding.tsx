import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, type OnboardingProgress, type OnboardingCheckpoint, type OnboardingAction } from './api'
import { useLang, useMeta, useProject } from './store'
import { errText } from './i18n'

export const GUIDE_ROUTES = ['/cn/quickstart', '/cn/quickstart', '/cn/apikeys', '/cn/chat', '/cn/memoryList', '/cn/requests'] as const

interface GuideContext {
  state: OnboardingProgress | null
  busy: boolean
  error: string
  available: boolean
  change: (action: OnboardingAction, checkpoint?: OnboardingCheckpoint) => Promise<void>
  reload: () => void
  detailsSeen: boolean
  markDetailsSeen: () => void
}

const Context = createContext<GuideContext | null>(null)
export const usePlatformGuide = () => useContext(Context)

export function PlatformGuideProvider({ children }: { children: ReactNode }) {
  const { account } = useMeta()
  const { currentProjectId, ready } = useProject()
  const { lang } = useLang()
  const navigate = useNavigate()
  const client = useQueryClient()
  const [failure, setFailure] = useState({ scope: '', text: '' })
  const [seenScope, setSeenScope] = useState('')
  const [busy, setBusy] = useState(false)
  const queue = useRef<Promise<unknown>>(Promise.resolve())
  const queued = useRef(0)
  const scope = JSON.stringify([account?.accountId, currentProjectId])
  const currentScope = useRef(scope)
  useLayoutEffect(() => {
    currentScope.current = scope
  }, [scope])
  const available = !!account?.accountId && ready && !!currentProjectId
  const queryKey = ['platform.guide', account?.accountId, currentProjectId]
  const query = useQuery({
    queryKey,
    queryFn: () => api.onboarding.get(currentProjectId),
    enabled: available,
    staleTime: 0,
    refetchOnWindowFocus: true,
  })
  const state = available ? (query.data ?? null) : null
  const change = (action: OnboardingAction, checkpoint?: OnboardingCheckpoint): Promise<void> => {
    if (!state || !available) return Promise.reject(new Error('Guide not ready'))
    queued.current++
    setBusy(true)
    const execute = async () => {
      if (currentScope.current !== scope) return
      await client.cancelQueries({ queryKey, exact: true })
      if (currentScope.current !== scope) return
      const latest = client.getQueryData<OnboardingProgress>(queryKey) ?? state
      try {
        const next = await api.onboarding.update(currentProjectId, action, latest.revision, checkpoint)
        client.setQueryData(queryKey, next)
        if (currentScope.current !== scope) return
        setFailure({ scope, text: '' })
        if (action === 'replay') {
          setSeenScope('')
          try {
            localStorage.removeItem(`console.chat.guide.v2:${account?.accountId}:${currentProjectId}:progress`)
          } catch {
            /* Browser storage is optional. */
          }
        }
        if (next.status === 'completed') navigate('/cn/quickstart')
        if (action !== 'dismiss' && action !== 'checkpoint' && next.status === 'active') navigate(GUIDE_ROUTES[Math.min(next.step, 5)])
      } catch (e) {
        if (currentScope.current === scope) setFailure({ scope, text: errText(lang, e) })
        void client.invalidateQueries({ queryKey })
        throw e
      }
    }
    const task = queue.current
      .catch(() => {})
      .then(execute)
      .finally(() => {
        queued.current--
        if (queued.current === 0) setBusy(false)
      })
    queue.current = task
    return task
  }
  const markDetailsSeen = useCallback(() => setSeenScope(scope), [scope])
  return (
    <Context.Provider
      value={{
        state,
        busy,
        error: failure.scope === scope ? failure.text : query.error ? errText(lang, query.error) : '',
        available,
        change,
        reload: () => {
          void query.refetch()
        },
        detailsSeen: seenScope === scope,
        markDetailsSeen,
      }}
    >
      {children}
    </Context.Provider>
  )
}
