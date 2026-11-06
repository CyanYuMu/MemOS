import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { useProject, useT } from '../lib/store'
import { Button } from '../components/ui/button'
import { EmptyState } from '../components/ui/empty-state'
import { ChatConversations } from '../components/chat/ChatConversations'
import { ChatFade, ChatSpinner } from '../components/chat/motion'
import { ChatWorkspace } from '../components/chat/ChatWorkspace'

type Mode = 'loading' | 'no-project' | 'error' | 'read-only' | 'no-key' | 'no-model' | 'unavailable' | 'chat'

export function Chat() {
  const t = useT()
  const navigate = useNavigate()
  const [guideRequest, setGuideRequest] = useState(0)
  const { currentProjectId, ready } = useProject()
  const stateQuery = useAsync(() => api.chat.state(currentProjectId), [currentProjectId], {
    skip: !ready || !currentProjectId,
    // state 与 config 的 deps 可能同时为 0，不加 key 会共用同一份缓存
    key: 'chat.state',
  })
  const stateData = stateQuery.data
  // 项目不可见/已删除时服务端会把 projectId 置空并回 hasProject=false：这也是
  // 一个有效的结论，不能因为 projectId 对不上就一直停在 loading。
  const stateReady =
    stateQuery.state === 'ready' && stateData !== null && (stateData.projectId === currentProjectId || !stateData.hasProject)

  let mode: Mode = 'loading'
  if (ready) {
    if (!currentProjectId) {
      mode = 'no-project'
    } else if (stateQuery.state === 'error') {
      mode = 'error'
    } else if (stateReady && stateData) {
      if (!stateData.hasProject) mode = 'no-project'
      else if (stateData.readOnly) mode = 'read-only'
      else if (!stateData.hasApiKey) mode = 'no-key'
      else if (!stateData.hasConfig) mode = 'no-model'
      else if (!stateData.canChat || !stateData.piAvailable) mode = 'unavailable'
      else mode = 'chat'
    }
  }

  return (
    <ChatWorkspace onGuide={() => setGuideRequest((value) => value + 1)} guideEnabled={mode === 'chat'}>
      <div className="chat-page memos-chat-page">
        {mode === 'loading' && <LoadingPanel />}

        {mode === 'no-project' && (
          <EmptyPanel
            icon="i-lucide:files"
            title={t('chat.noProject.title')}
            desc={t('chat.noProject.desc')}
            action={
              <Button variant="primary" onClick={() => navigate('/cn/projects')}>
                {t('chat.noProject.action')}
              </Button>
            }
          />
        )}

        {mode === 'error' && (
          <EmptyPanel
            icon="i-lucide:alert-circle"
            title={stateQuery.error || t('common.failed')}
            action={
              <Button variant="secondary" onClick={stateQuery.reload}>
                {t('common.retry')}
              </Button>
            }
          />
        )}

        {mode === 'read-only' && <EmptyPanel icon="i-lucide:shield-off" title={t('chat.readOnly.title')} desc={t('chat.readOnly.desc')} />}

        {mode === 'no-key' && (
          <EmptyPanel
            icon="i-lucide:shield-off"
            title={t('chat.noKey.title')}
            desc={t('chat.noKey.desc')}
            action={
              <Button variant="primary" onClick={() => navigate('/cn/apikeys')}>
                {t('chat.noKey.action')}
              </Button>
            }
          />
        )}

        {mode === 'no-model' && (
          <EmptyPanel
            icon="i-lucide:shield-off"
            title={t('chat.noModel.title')}
            desc={t('chat.noModel.desc')}
            action={
              <Button variant="secondary" onClick={stateQuery.reload}>
                {t('common.retry')}
              </Button>
            }
          />
        )}

        {mode === 'unavailable' && (
          <EmptyPanel
            icon="i-lucide:alert-circle"
            title={t('chat.unavailable.title')}
            desc={t('chat.unavailable.desc')}
            action={
              <Button variant="secondary" onClick={stateQuery.reload}>
                {t('common.retry')}
              </Button>
            }
          />
        )}

        {mode === 'chat' && currentProjectId && (
          <ChatConversations key={currentProjectId} projectId={currentProjectId} guideRequest={guideRequest} />
        )}
      </div>
    </ChatWorkspace>
  )
}

function LoadingPanel() {
  const t = useT()
  return (
    <ChatFade className="flex min-h-0 flex-1 items-center justify-center gap-2 rounded-xl border border-line bg-surface text-sm text-ink-muted shadow-2xs">
      <ChatSpinner />
      {t('common.loading')}
    </ChatFade>
  )
}

function EmptyPanel({ icon, title, desc, action }: { icon: string; title: string; desc?: string; action?: ReactNode }) {
  return (
    <ChatFade className="flex min-h-0 flex-1 items-center justify-center rounded-xl border border-line bg-surface shadow-2xs">
      <EmptyState icon={icon} title={title} desc={desc} action={action} />
    </ChatFade>
  )
}
