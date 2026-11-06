import { useState } from 'react'
import { api, type Memory } from '../../lib/api'
import { useAsync } from '../../lib/useAsync'
import { useLang, useT } from '../../lib/store'
import { errText } from '../../lib/i18n'
import { entryFromMemory, type MemoryEntry } from './memory'
import { Tabs } from '../ui/tabs'

/** 事件展示持久化实体，记忆库读取当前详情；两种入口不相互覆盖。 */
export function MemoryDetailFields({
  projectId,
  entry,
  snapshot,
  previous,
}: {
  projectId: string
  entry: MemoryEntry
  snapshot?: Memory
  previous?: Memory
}) {
  const t = useT()
  const { lang } = useLang()
  const [pane, setPane] = useState('info')
  const query = useAsync(
    () => api.memory.detail(projectId, entry.memoryId, entry.detailType || 'text_mem'),
    [projectId, entry.memoryId, entry.detailType],
    {
      key: 'chat.memory.detail',
      skip: !entry.memoryId || !!snapshot,
    },
  )
  if (!snapshot && query.state === 'loading')
    return (
      <p className="memos-memory-loading" role="status">
        {t('common.loading')}
      </p>
    )
  if (!snapshot && (query.state === 'error' || !query.data || query.data.memoryId !== entry.memoryId))
    return (
      <div className="memos-memory-error" role="alert">
        <span>{query.error ? errText(lang, query.error) : t('playground.logs.memoryUnavailable')}</span>
        <button onClick={query.reload}>{t('common.retry')}</button>
      </div>
    )
  const record = snapshot ? (pane === 'before' && previous ? previous : snapshot) : query.data
  if (!record) return null
  const memory = entryFromMemory(record)
  const history = [...(record.history ?? [])].sort((a, b) => b.version - a.version)
  const fields: [string, string | string[] | undefined][] = [
    [t('playground.logs.field.id'), memory.memoryId],
    [t('playground.logs.key'), memory.key],
    [t('playground.logs.field.value'), memory.value],
    [t('playground.logs.field.type'), memory.memoryType],
    [t('playground.logs.field.status'), memory.status],
    [t('playground.logs.field.tags'), memory.tags],
    [t('playground.logs.field.updatedAt'), memory.updateTime],
  ]
  const table = (
    <table className="memos-detail-table memos-memory-fields">
      {snapshot && record.version !== undefined && record.version > 0 && (
        <caption className="memos-memory-fields-caption">
          <span>
            {t('memory.eventVersion')} V{record.version}
          </span>
        </caption>
      )}
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
              {Array.isArray(value) ? (
                value.length ? (
                  value.map((tag) => (
                    <span className="memos-detail-tag" key={tag}>
                      {tag}
                    </span>
                  ))
                ) : (
                  <span>—</span>
                )
              ) : (
                <span>{value || '—'}</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
  if (snapshot)
    return (
      <>
        {previous && (
          <Tabs
            label={t('memory.detailTitle')}
            value={pane}
            onChange={setPane}
            items={[
              { key: 'info', label: t('memory.change.after') },
              { key: 'before', label: t('memory.change.before') },
            ]}
          />
        )}
        {table}
      </>
    )
  return (
    <>
      <Tabs
        label={t('memory.detailTitle')}
        value={pane}
        onChange={setPane}
        items={[
          { key: 'info', label: t('memory.info') },
          { key: 'versions', label: t('memory.versions') },
        ]}
      />
      {pane === 'info' ? (
        <>
          {table}
          <section className="memos-linked-memories">
            <h3>{t('memory.sources')}</h3>
            {record.sources?.length ? (
              record.sources.map((source, index) => (
                <div className="memos-memory-source" key={index}>
                  <div>
                    {source.role || source.type || t('memory.sourceText')}
                    {source.time && <time>{source.time}</time>}
                  </div>
                  <p>{source.content || '—'}</p>
                  {source.url && <p>{source.url}</p>}
                </div>
              ))
            ) : (
              <p>{t('memory.noSources')}</p>
            )}
          </section>
        </>
      ) : (
        <div className="memory-version-timeline memos-memory-versions">
          <section className="memory-version current">
            <span className="memory-version-dot" />
            <div className="memory-version-heading">
              <h3>{t(snapshot ? 'memory.eventVersion' : 'memory.currentVersion')}</h3>
              {record.version !== undefined && record.version > 0 && <span className="memory-version-number">V{record.version}</span>}
            </div>
            <time>{record.updateTime || record.createTime}</time>
            <div className="memory-drawer-content">{record.memoryContent}</div>
          </section>
          {history.map((version, i) => (
            <section className="memory-version" key={`${version.version}:${i}`}>
              <span className="memory-version-dot" />
              <div className="memory-version-heading">
                <h3>V{version.version}</h3>
              </div>
              <time>{version.createdAt}</time>
              <div className="memory-drawer-content">{version.content}</div>
              {(version.validFrom || version.validTo) && (
                <p className="memory-version-validity">
                  {t('memory.validPeriod')} {version.validFrom || '—'} → {version.validTo || '—'}
                </p>
              )}
            </section>
          ))}
          {!history.length && <p className="memory-drawer-empty">{t('memory.noHistory')}</p>}
        </div>
      )}
    </>
  )
}
