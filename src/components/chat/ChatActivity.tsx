import { memo, useId, useState } from 'react'
import { LoaderCircle, Maximize2, Minimize2, Sparkles } from 'lucide-react'
import { useT } from '../../lib/store'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCard } from './ToolCallCard'
import type { MemoryCitationScope } from './memoryCitations'
import type { ChatThinkingItem, ChatToolItem } from './timeline'

export type ActivityItem = ChatThinkingItem | ChatToolItem

/** 标题和放大按钮只切换同一个原位置面板，不创建过程副本。 */
export const ChatActivity = memo(function ChatActivity({
  items,
  highlightedId,
  memory,
}: {
  items: ActivityItem[]
  highlightedId: string | null
  memory?: MemoryCitationScope
}) {
  const t = useT()
  const bodyId = useId()
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const running = items.some((item) => (item.kind === 'thinking' ? item.streaming : item.running))
  const highlighted = items.some((item) => item.id === highlightedId)
  const open = highlighted || (manualOpen ?? running)
  const thinking = items.some((item) => item.kind === 'thinking')
  const failed = items.some((item) => item.kind === 'tool' && item.isError)
  const toggle = () => setManualOpen(!open)
  return (
    <section className={`memos-activity${open ? ' is-open' : ''}`}>
      <div className="memos-activity-header">
        <div className="memos-activity-label">
          {running ? <LoaderCircle size={16} className="memos-spin" /> : <Sparkles size={16} />}
          <span>
            {t(
              running
                ? 'chat.activity.running'
                : failed
                  ? 'chat.activity.failed'
                  : thinking
                    ? 'chat.activity.thought'
                    : 'chat.activity.done',
            )}
          </span>
        </div>
        <button
          type="button"
          className="memos-activity-zoom"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={toggle}
          aria-label={t(open ? 'chat.activity.collapseView' : 'chat.activity.expandView')}
        >
          {open ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
        </button>
      </div>
      {open && (
        <div id={bodyId} className="memos-activity-body" role="region" aria-label={t('chat.activity.description')}>
          {items.map((item) =>
            item.kind === 'thinking' ? (
              <ThinkingBlock key={item.id} item={item} memory={memory} />
            ) : (
              <ToolCallCard key={item.id} item={item} highlighted={item.id === highlightedId} />
            ),
          )}
        </div>
      )}
    </section>
  )
})
