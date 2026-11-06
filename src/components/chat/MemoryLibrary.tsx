import { api } from '../../lib/api'
import { useAsync } from '../../lib/useAsync'
import { useState } from 'react'
import { useLang, useT } from '../../lib/store'
import { errText } from '../../lib/i18n'
import { entryFromMemory } from './memory'
import type { MemoryEntry } from './memory'

const TYPES = ['profile_mem', 'text_mem', 'pref_mem', 'event_mem', 'tool_mem'] as const

export function MemoryLibrary({ projectId, onSelect }: { projectId: string; onSelect: (entry: MemoryEntry) => void }) {
  const t = useT()
  const { lang } = useLang()
  const [page, setPage] = useState(1)
  const [type, setType] = useState<(typeof TYPES)[number]>('profile_mem')
  const query = useAsync(
    () => api.memory.list({ projectId, memoryDetailType: type, pageNum: page, pageSize: 30 }),
    [projectId, type, page],
    { key: 'chat.memory.library' },
  )
  return (
    <div className="memos-memory-library">
      <p>{t('playground.logs.libraryScope')}</p>
      <div className="memos-memory-types" role="group" aria-label={t('memory.detailTitle')}>
        {TYPES.map((value) => (
          <button
            key={value}
            aria-pressed={type === value}
            onClick={() => {
              setType(value)
              setPage(1)
            }}
          >
            {t(`memory.tab.${value}`)}
          </button>
        ))}
      </div>
      {query.state === 'loading' && <p role="status">{t('common.loading')}</p>}
      {query.state === 'error' && (
        <div className="memos-memory-error" role="alert">
          <span>{errText(lang, query.error)}</span>
          <button onClick={query.reload}>{t('common.retry')}</button>
        </div>
      )}
      {query.data?.memListItemResponseList.map((memory) => (
        <button
          className="memos-library-row"
          key={memory.memoryId}
          onClick={() => onSelect({ ...entryFromMemory(memory), detailType: type })}
        >
          <span>{memory.memoryKey || memory.memoryContent}</span>
          {memory.memoryKey && <span>{memory.memoryContent}</span>}
        </button>
      ))}
      {query.state === 'ready' && query.data?.total === 0 && <p>{t('chat.memory.empty')}</p>}
      {query.data && query.data.total > 30 && (
        <div className="memos-memory-pagination">
          <button disabled={page === 1} onClick={() => setPage((value) => value - 1)}>
            {t('playground.logs.prevPage')}
          </button>
          <span>{t('common.total')(query.data.total)}</span>
          <button disabled={page * 30 >= query.data.total} onClick={() => setPage((value) => value + 1)}>
            {t('playground.logs.nextPage')}
          </button>
        </div>
      )}
    </div>
  )
}
