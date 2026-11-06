import { memo, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { Icon } from '../Icon'
import { useT } from '../../lib/store'
import { DUR, EASE_IN_FAST, EASE_OUT_EXPO } from '../../lib/motion'
import { cn } from '../../lib/utils'
import { ChatSpinner } from './motion'
import { Markdown } from './Markdown'
import { MemoryCard } from './MemoryCard'
import { isMemoryTool, memoryHitCount, parseMemoryResult, type MemoryEntry } from './memory'
import { isMemoryOperation, memoryLogPresentation } from './memoryLog'
import { asRecord, previewValue, shortToolName, toolQuery, type ChatToolItem } from './timeline'

/** 一次工具调用。默认折叠，摘要行就能看到工具名、查询词与命中条数。 */
export const ToolCallCard = memo(function ToolCallCard({
  item,
  highlighted = false,
  locatable = true,
}: {
  item: ChatToolItem
  /** 从记忆汇总面板点过来时高亮并展开 */
  highlighted?: boolean
  /** 放大视图是副本，不重复注册主时间线的定位锚点。 */
  locatable?: boolean
}) {
  const t = useT()
  const reduce = useReducedMotion()
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const [showAll, setShowAll] = useState(false)
  // 高亮时自动展开；用户手动点过后以手动状态为准
  const open = highlighted || (manualOpen ?? false)

  // MCP 工具名带 `mcp__memory_console__` 前缀，按后缀识别，别写死完整名
  const isSearch = item.toolName.endsWith('search_memory')
  const isProfile = item.toolName.endsWith('get_user_profile')
  const memoryTool = isMemoryTool(item.toolName)
  const query = memoryTool ? toolQuery(item.args) : ''
  const parsed = memoryTool ? parseMemoryResult(item.result) : { parsed: false, entries: [] }
  // 执行中的 partialResult 往往是空壳，别把它当成命中数展示
  const hits = memoryTool ? memoryHitCount(item) : undefined
  const statusKey = item.running ? 'running' : item.isError ? 'failed' : 'done'
  const status = item.running ? t('chat.tool.running') : item.isError ? t('chat.tool.failed') : t('chat.tool.done')
  const statusClass = item.running ? 'text-ink-muted' : item.isError ? 'text-danger' : 'text-success'
  const icon = isSearch ? 'i-lucide:search' : isProfile ? 'i-lucide:users' : 'i-lucide:terminal'
  const displayName = isMemoryOperation(item.toolName) ? t(memoryLogPresentation(item).titleKey) : shortToolName(item.toolName) || 'tool'
  const params = memoryTool ? keyParamsOf(item.args) : []
  const visible = showAll ? parsed.entries : parsed.entries.slice(0, MEMORY_PREVIEW_COUNT)

  const bodyInitial = reduce ? { opacity: 0 } : { opacity: 0, height: 0 }
  const bodyAnimate = reduce ? { opacity: 1 } : { opacity: 1, height: 'auto' }
  const bodyExit = reduce ? { opacity: 0 } : { opacity: 0, height: 0, transition: { duration: DUR.fast, ease: EASE_IN_FAST } }
  const bodyTransition = reduce ? { duration: DUR.fast } : { duration: DUR.base, ease: EASE_OUT_EXPO }

  return (
    <motion.div
      id={locatable ? `tool-${item.id}` : undefined}
      className={cn(
        'chat-tool-card overflow-hidden rounded-xl border bg-surface shadow-xs transition-[box-shadow,border-color]',
        highlighted ? 'border-accent/60 shadow-md ring-2 ring-accent/30' : 'border-line',
      )}
      initial={reduce ? { opacity: 0 } : { opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={reduce ? { duration: DUR.fast } : { duration: DUR.base, ease: EASE_OUT_EXPO }}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setManualOpen(!open)}
        className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs text-ink-secondary transition-colors hover:bg-surface-hover/60"
      >
        <Icon name={icon} className="size-4 shrink-0 text-ink-muted" />
        <span className="shrink-0 font-medium text-ink" title={item.toolName}>
          {displayName}
        </span>
        {query && <span className="min-w-0 truncate text-ink-muted">{query}</span>}
        <AnimatePresence initial={false}>
          {hits !== undefined && (
            <motion.span
              key="hits"
              className="shrink-0 rounded-md bg-surface-hover px-1.5 py-0.5 text-xs text-ink-muted"
              initial={reduce ? { opacity: 0 } : { opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, scale: 0.9 }}
              transition={{ duration: DUR.fast, ease: EASE_OUT_EXPO }}
            >
              {t('chat.tool.hits')(hits)}
            </motion.span>
          )}
        </AnimatePresence>
        <span className="ml-auto flex min-w-16 shrink-0 items-center justify-end">
          <AnimatePresence initial={false} mode="wait">
            <motion.span
              key={statusKey}
              className={`flex items-center gap-1 ${statusClass}`}
              initial={reduce ? { opacity: 0 } : { opacity: 0, y: -2 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduce ? { opacity: 0 } : { opacity: 0, y: 2 }}
              transition={{ duration: DUR.fast, ease: EASE_OUT_EXPO }}
            >
              {item.running ? (
                <ChatSpinner />
              ) : (
                <Icon name={item.isError ? 'i-lucide:alert-circle' : 'i-lucide:check'} className="size-3.5" />
              )}
              <span>{status}</span>
            </motion.span>
          </AnimatePresence>
        </span>
        <motion.span
          className="shrink-0 text-ink-faint"
          animate={{ rotate: open ? 180 : 0 }}
          transition={reduce ? { duration: 0 } : { duration: DUR.fast, ease: EASE_OUT_EXPO }}
        >
          <Icon name="i-lucide:chevron-down" className="size-3.5" />
        </motion.span>
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="body"
            className="overflow-hidden"
            initial={bodyInitial}
            animate={bodyAnimate}
            exit={bodyExit}
            transition={bodyTransition}
          >
            <div className="space-y-2.5 border-t border-line px-3 py-2.5">
              {memoryTool && parsed.parsed ? (
                <>
                  {params.length > 0 && <ParamRow params={params} />}
                  <MemoryList
                    entries={parsed.entries}
                    visible={visible}
                    total={parsed.entries.length}
                    showAll={showAll}
                    onToggle={() => setShowAll((value) => !value)}
                  />
                  <RawData args={item.args} result={item.result} running={item.running} />
                </>
              ) : (
                <>
                  {memoryTool && !item.running && <p className="text-xs text-warning">{t('chat.memory.parseFallback')}</p>}
                  <RawData args={item.args} result={item.result} running={item.running} defaultOpen={!isMemoryOperation(item.toolName)} />
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
})

const MEMORY_PREVIEW_COUNT = 5

function MemoryList({
  entries,
  visible,
  total,
  showAll,
  onToggle,
}: {
  entries: MemoryEntry[]
  visible: MemoryEntry[]
  total: number
  showAll: boolean
  onToggle: () => void
}) {
  const t = useT()
  if (entries.length === 0) {
    return (
      <div className="chat-memory-empty flex items-center gap-1.5 rounded-lg border border-dashed border-line px-2.5 py-2 text-xs text-ink-faint">
        <Icon name="i-lucide:search" className="size-3.5" />
        {t('chat.memory.empty')}
      </div>
    )
  }
  return (
    <div className="space-y-1.5">
      {visible.map((entry, index) => (
        <MemoryCard key={entry.memoryId || `${entry.value}_${index}`} entry={entry} />
      ))}
      {total > MEMORY_PREVIEW_COUNT && (
        <button
          type="button"
          onClick={onToggle}
          className="w-full cursor-pointer rounded-lg border border-line bg-surface py-1.5 text-xs text-ink-muted transition-colors hover:bg-surface-hover hover:text-ink"
        >
          {showAll ? t('chat.memory.collapse') : t('chat.memory.viewAll')(total)}
        </button>
      )}
    </div>
  )
}

function ParamRow({ params }: { params: { label: string; value: string }[] }) {
  const t = useT()
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      <span className="shrink-0 text-ink-faint">{t('chat.tool.params')}</span>
      {params.map((param) => (
        <span key={param.label} className="inline-flex min-w-0 max-w-full items-center gap-1">
          <span className="shrink-0 text-ink-faint">{param.label}</span>
          <span className="min-w-0 truncate font-mono text-ink-secondary">{param.value}</span>
        </span>
      ))}
    </div>
  )
}

/** 原始参数与结果。记忆卡片解析成功时折叠，失败时默认展开作为兜底。 */
function RawData({
  args,
  result,
  running,
  defaultOpen = false,
}: {
  args: unknown
  result: unknown
  running: boolean
  defaultOpen?: boolean
}) {
  const t = useT()
  const reduce = useReducedMotion()
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="overflow-hidden rounded-lg border border-line/70">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full cursor-pointer items-center gap-2 px-2.5 py-1.5 text-left text-xs text-ink-faint transition-colors hover:bg-surface-hover/50"
      >
        <Icon name="i-lucide:braces" className="size-3.5" />
        <span>{t('chat.tool.raw')}</span>
        <motion.span
          className="ml-auto"
          animate={{ rotate: open ? 180 : 0 }}
          transition={reduce ? { duration: 0 } : { duration: DUR.fast, ease: EASE_OUT_EXPO }}
        >
          <Icon name="i-lucide:chevron-down" className="size-3.5" />
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="raw"
            className="overflow-hidden"
            initial={reduce ? { opacity: 0 } : { opacity: 0, height: 0 }}
            animate={reduce ? { opacity: 1 } : { opacity: 1, height: 'auto' }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, height: 0, transition: { duration: DUR.fast, ease: EASE_IN_FAST } }}
            transition={reduce ? { duration: DUR.fast } : { duration: DUR.base, ease: EASE_OUT_EXPO }}
          >
            <div className="space-y-2 border-t border-line/70 px-2.5 py-2">
              <RawBlock label={t('chat.tool.args')} text={previewValue(args) || t('chat.tool.emptyResult')} />
              <RawBlock
                label={t('chat.tool.result')}
                text={running ? t('chat.tool.running') : previewValue(result) || t('chat.tool.emptyResult')}
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function RawBlock({ label, text }: { label: string; text: string }) {
  const t = useT()
  const [expanded, setExpanded] = useState(false)
  // 短结果不显示按钮，避免每条卡片都多一行
  const long = text.length > 1200 || text.split('\n').length > 16
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs font-medium text-ink-faint">
        <span>{label}</span>
        {long && (
          <button
            type="button"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
            className="cursor-pointer text-accent transition-colors hover:text-accent/80"
          >
            {expanded ? t('chat.tools.collapse') : t('chat.tools.expand')}
          </button>
        )}
      </div>
      <div className={`memos-tool-code ${expanded ? 'is-expanded' : ''}`}>
        <Markdown text={'```json\n' + text + '\n```'} />
      </div>
    </div>
  )
}

/** 参数区只挑用户关心的字段，完整参数在“原始数据”里 */
function keyParamsOf(args: unknown): { label: string; value: string }[] {
  const rec = asRecord(args)
  if (!rec) return []
  const fields: { label: string; keys: string[] }[] = [
    { label: 'query', keys: ['query', 'memSearchContent', 'q', 'text', 'keyword'] },
    { label: 'top_k', keys: ['top_k', 'topK', 'limit', 'size'] },
    { label: 'user', keys: ['user_id', 'userId', 'external_user_id', 'externalUserId'] },
    { label: 'include_preference', keys: ['include_preference', 'includePreference'] },
  ]
  const out: { label: string; value: string }[] = []
  for (const field of fields) {
    for (const key of field.keys) {
      const value = rec[key]
      if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
        out.push({ label: field.label, value: String(value) })
        break
      }
    }
  }
  return out
}
