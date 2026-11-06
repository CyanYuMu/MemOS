import { useEffect, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { usePlatformGuide } from '../lib/onboarding'
import { api } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { useLang, useT } from '../lib/store'
import { errText } from '../lib/i18n'
import { DetailError, DetailSkeleton } from './ui/detail'
import { Tabs } from './ui/tabs'
import { CopyButton } from './ui/copy-button'
import { Markdown } from './chat/Markdown'

function MemoryContent({ text }: { text: string }) {
  return (
    <div className="memory-drawer-content">
      <span>{text}</span>
      <CopyButton text={text} />
    </div>
  )
}

export function MemoryDrawer({
  projectId,
  memoryId,
  memType,
  returnFocus,
  onClose,
}: {
  projectId: string
  memoryId: string
  memType: string
  returnFocus?: HTMLElement | null
  onClose: () => void
}) {
  const t = useT()
  const { lang } = useLang()
  const [pane, setPane] = useState('info')
  const query = useAsync(() => api.memory.detail(projectId, memoryId, memType), [projectId, memoryId, memType], { key: 'memory.drawer' })
  const guide = usePlatformGuide()
  const markSeen = guide?.markDetailsSeen
  /** 指引第 5 步：用户打开详情后，要把他带到“记忆版本”页签，否则不知道该点哪里 */
  const guiding = !!guide?.available && guide.state?.status === 'active' && guide.state.step === 4
  const memory = query.data
  useEffect(() => {
    if (memory && pane === 'versions') markSeen?.()
  }, [memory, pane, markSeen])
  const history = [...(memory?.history ?? [])].sort((a, b) => b.version - a.version)
  const info = memory
    ? [
        [t('memory.col.createdAt'), memory.createTime],
        [t('memory.updatedAt'), memory.updateTime],
        [t('memory.detailId'), memory.memoryId],
        [t('playground.logs.key'), memory.memoryKey || '—'],
        [t('playground.logs.field.status'), memory.status || '—'],
        [
          t('memory.col.memoryType'),
          memory.memoryType
            ? (() => {
                const key = `graph.type.${memory.memoryType}`
                const label = t(key as 'graph.type.Other')
                return label === key ? memory.memoryType : label
              })()
            : t(`memory.tab.${memType}` as 'memory.tab.text_mem'),
        ],
        ...(memory.profileTemplateId ? [[t('memory.col.profileTreeId'), memory.profileTemplateId]] : []),
      ]
    : []
  return (
    <Dialog.Root open onOpenChange={(value) => !value && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="memory-drawer-overlay" />
        <Dialog.Content
          className="memory-drawer"
          onCloseAutoFocus={(event) => {
            if (returnFocus?.isConnected) {
              event.preventDefault()
              returnFocus.focus({ preventScroll: true })
            }
          }}
        >
          <header className="memory-drawer-header">
            <Dialog.Title>{t('memory.detailTitle')}</Dialog.Title>
            <Dialog.Close aria-label={t('common.close')}>
              <X size={20} />
            </Dialog.Close>
          </header>
          <Dialog.Description className="sr-only">{t('memory.drawerDescription')}</Dialog.Description>
          <div className="memory-drawer-tabs" data-guide={guiding && !guide?.detailsSeen ? 'versions' : undefined}>
            <Tabs
              label={t('memory.detailTitle')}
              value={pane}
              onChange={setPane}
              items={[
                { key: 'info', label: t('memory.basicInfo') },
                { key: 'versions', label: t('memory.versions') },
              ]}
            />
          </div>
          {guiding && (
            <p className="memory-drawer-guide" role="status">
              {guide?.detailsSeen ? t('webGuide.memory.drawerDone') : t('webGuide.memory.drawerHint')}
            </p>
          )}
          <div className="memory-drawer-body">
            {query.state === 'loading' && <DetailSkeleton rows={6} />}
            {query.state === 'error' && <DetailError text={errText(lang, query.error)} onRetry={query.reload} />}
            {memory && pane === 'info' && (
              <>
                <section>
                  <h3>{t('memory.col.content')}</h3>
                  <MemoryContent text={memory.memoryContent} />
                </section>
                <section>
                  <h3>{t('memory.info')}</h3>
                  <dl className="memory-drawer-info">
                    {info
                      .filter(([, value]) => value)
                      .map(([label, value]) => (
                        <div key={label}>
                          <dt>{label}</dt>
                          <dd>{value}</dd>
                        </div>
                      ))}
                  </dl>
                  {!!memory.tags?.length && (
                    <div className="memory-drawer-tags">
                      {memory.tags.map((tag) => (
                        <span key={tag}>{tag}</span>
                      ))}
                    </div>
                  )}
                </section>
                <section>
                  <h3>{t('memory.sources')}</h3>
                  {memory.sources?.length ? (
                    <dl className="memory-drawer-info">
                      <div>
                        <dt>{t('memory.sourceType')}</dt>
                        <dd>
                          {memory.sources.every((source) => source.type === 'chat')
                            ? t('memory.sourceText')
                            : [...new Set(memory.sources.map((source) => source.type).filter(Boolean))].join(', ') || '—'}
                        </dd>
                      </div>
                      <div>
                        <dt>{t('memory.sourceContent')}</dt>
                        <dd className="memory-source-json">
                          <Markdown
                            text={
                              '```json\n' +
                              JSON.stringify(
                                memory.sources.map((source) => ({
                                  role: source.role,
                                  type: source.type,
                                  content: source.content,
                                  chat_time: source.time || undefined,
                                  url: source.url,
                                })),
                                null,
                                2,
                              ) +
                              '\n```'
                            }
                          />
                        </dd>
                      </div>
                    </dl>
                  ) : (
                    <p className="memory-drawer-empty">{t('memory.noSources')}</p>
                  )}
                </section>
              </>
            )}
            {memory && pane === 'versions' && (
              <div className="memory-version-timeline">
                <section className="memory-version current">
                  <span className="memory-version-dot" />
                  <div className="memory-version-heading">
                    <h3>{t('memory.currentVersion')}</h3>
                    {memory.version !== undefined && memory.version > 0 && <span className="memory-version-number">V{memory.version}</span>}
                  </div>
                  <time>{memory.updateTime || memory.createTime}</time>
                  <MemoryContent text={memory.memoryContent} />
                </section>
                {history.map((version, i) => (
                  <section className="memory-version" key={`${version.version}:${i}`}>
                    <span className="memory-version-dot" />
                    <div className="memory-version-heading">
                      <h3>V{version.version}</h3>
                      {version.updateType && <span>{t(`memory.change.${version.updateType}` as 'memory.change.update')}</span>}
                    </div>
                    <time>{version.createdAt}</time>
                    <MemoryContent text={version.content} />
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
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
