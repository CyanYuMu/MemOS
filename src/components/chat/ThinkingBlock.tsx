import { memo, useDeferredValue } from 'react'
import { Markdown } from './Markdown'
import type { MemoryCitationScope } from './memoryCitations'
import type { ChatThinkingItem } from './timeline'

/** 只呈现模型实际返回的文本和已验证引用，不补写思考。 */
export const ThinkingBlock = memo(function ThinkingBlock({ item, memory }: { item: ChatThinkingItem; memory?: MemoryCitationScope }) {
  const deferredText = useDeferredValue(item.text)
  return (
    <div className="memos-thinking-content">
      <Markdown text={item.streaming ? deferredText : item.text} memory={memory} />
    </div>
  )
})
