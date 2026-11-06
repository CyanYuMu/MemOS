import { useT } from '../../lib/store'
import type { Memory, MemoryLogActivity } from '../../lib/api'
import { entryFromMemory, type MemoryEntry } from './memory'
import { memoryLogPresentation, memoryWriteCallId, type MemoryLogRow } from './memoryLog'
import { shortToolName, type ChatToolItem } from './timeline'

/** 对话记录与记忆实体分开展示，关联只来自实际提取结果。 */
export function MemoryOperationDetail({
  item,
  row,
  activity,
  onMemory,
}: {
  item: ChatToolItem
  row?: MemoryLogRow
  activity?: MemoryLogActivity
  onMemory: (entry: MemoryEntry, snapshot?: Memory) => void
}) {
  const t = useT()
  const presentation = memoryLogPresentation(item)
  const isWrite = shortToolName(item.toolName) === 'add_message'
  const time = item.timestamp === undefined ? undefined : new Date(item.timestamp)
  const status = item.running
    ? t('playground.logs.status.running')
    : presentation.status === 'failed' || activity?.status === 'failed'
      ? t('playground.logs.status.failed')
      : isWrite && activity?.status === 'succeeded'
        ? t('playground.logs.extracted')(activity.total)
        : isWrite && activity?.status === 'pending'
          ? t('playground.logs.extracting')
          : t(`playground.logs.status.${presentation.status}`)
  const fields = [
    ...(row?.message
      ? [
          [
            t('playground.logs.role'),
            t(
              row.message.role === 'user'
                ? 'playground.logs.role.user'
                : row.message.role === 'assistant'
                  ? 'playground.logs.role.assistant'
                  : 'playground.logs.role.message',
            ),
          ],
        ]
      : []),
    ...(row?.text ? [[t('playground.logs.content'), row.text]] : []),
    [t('playground.logs.processingStatus'), status],
    ...(time && Number.isFinite(time.getTime()) ? [[t('playground.logs.submittedAt'), time.toLocaleString('sv-SE')]] : []),
    ...(!isWrite && presentation.count !== undefined ? [[t('playground.logs.resultCount'), String(presentation.count)]] : []),
  ]
  return (
    <>
      <table className="memos-detail-table">
        <thead>
          <tr>
            <th>{t('playground.logs.field')}</th>
            <th>{t('playground.logs.value')}</th>
          </tr>
        </thead>
        <tbody>
          {fields.map(([label, value]) => (
            <tr key={label}>
              <th scope="row">{label}</th>
              <td>
                <span>{value}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {isWrite && (
        <section className="memos-linked-memories" aria-label={t('playground.logs.linkedMemories')}>
          <h3>{t('playground.logs.linkedMemories')}</h3>
          {activity?.status === 'succeeded' ? (
            <>
              {activity.memories.map((memory) => (
                <button className="memos-library-row" key={memory.memoryId} onClick={() => onMemory(entryFromMemory(memory), memory)}>
                  <span>{memory.memoryKey || memory.memoryContent}</span>
                  {memory.memoryKey && <span>{memory.memoryContent}</span>}
                </button>
              ))}
              {activity.total === 0 && <p>{t('playground.logs.noExtraction')}</p>}
              {activity.total > activity.memories.length && !activity.truncated && <p>{t('playground.logs.unavailableMemories')}</p>}
              {activity.truncated && <p>{t('playground.logs.partial')}</p>}
            </>
          ) : (
            <p role={activity?.status === 'pending' ? 'status' : undefined}>
              {!activity && memoryWriteCallId(item)
                ? t('common.loading')
                : activity?.status === 'pending'
                  ? t('playground.logs.extracting')
                  : activity?.status === 'failed'
                    ? t('playground.logs.extractionFailed')
                    : t('playground.logs.noAssociation')}
            </p>
          )}
          <a className="memos-memory-library-link" href="/cn/memoryList">
            {t('playground.logs.viewLibrary')}
          </a>
        </section>
      )}
    </>
  )
}
