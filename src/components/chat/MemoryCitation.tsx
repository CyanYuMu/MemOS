import { useContext } from 'react'
import { useT } from '../../lib/store'
import { MemoryCitationContext } from './memoryCitationContext'
import { resolveMemoryCitation } from './memoryCitations'
import { MemorySnapshotDetail } from './MemorySnapshotDetail'

export function MemoryCitation({ token }: { token: string }) {
  const t = useT()
  const scope = useContext(MemoryCitationContext)
  const match = resolveMemoryCitation(token, scope?.snapshot)
  if (!scope || !match) return null
  const number = match.index + 1
  if (!match.row.record)
    return (
      <span className="memos-memory-citation is-unavailable" title={t('chat.snapshot.fieldsMissing')}>
        {number}
      </span>
    )
  return (
    <MemorySnapshotDetail projectId={scope.projectId} row={match.row}>
      <button
        type="button"
        className="memos-memory-citation"
        title={match.row.entry.key}
        aria-label={`${t('chat.memory.citation')} ${number}: ${match.row.entry.key ?? ''}`}
      >
        {number}
      </button>
    </MemorySnapshotDetail>
  )
}
