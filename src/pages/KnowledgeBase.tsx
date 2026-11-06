import { useRef, useState } from 'react'
import { api, type KnowledgeBase as KnowledgeBaseRow, type KnowledgeFile } from '../lib/api'
import { useAsync, usePaged } from '../lib/useAsync'
import { useLang, useProject, useT, useToast } from '../lib/store'
import { errText } from '../lib/i18n'
import { useLastValue } from '../lib/useLastValue'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Dialog, Pager, PageHeader, Panel, PrimaryButton, SearchBox, StateRow, Table, TD, Th } from '../components/ui'
import { EmptyState } from '../components/ui/empty-state'
import { RowActions } from '../components/ui/row-actions'

const ACCEPT = '.pdf,.doc,.docx,.txt,.json,.md,.xml'

function formatBytes(size = 0) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function readDataURL(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('read failed'))
    reader.readAsDataURL(file)
  })
}

type FormState = { id?: string; name: string; description: string }

export function KnowledgeBase() {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const { currentProjectId, ready } = useProject()
  const pg = usePaged(10)
  const [tick, setTick] = useState(0)
  const [form, setForm] = useState<FormState | null>(null)
  const [saving, setSaving] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<KnowledgeBaseRow | null>(null)
  const [fileTarget, setFileTarget] = useState<KnowledgeBaseRow | null>(null)

  const noProject = ready && !currentProjectId
  const { data, state, error, reload } = useAsync(
    () => api.knowledgeBase.list(currentProjectId, pg.page, pg.pageSize, pg.q),
    [currentProjectId, pg.page, pg.pageSize, pg.q, tick],
    { skip: !ready || !currentProjectId, key: 'knowledgebase.list' },
  )
  const rows = data?.records ?? []

  const refresh = () => {
    reload()
    setTick((x) => x + 1)
  }

  const save = async () => {
    if (!form) return
    const name = form.name.trim()
    if (!name) {
      toast(t('kb.nameRequired'), 'err')
      return
    }
    setSaving(true)
    try {
      if (form.id) {
        await api.knowledgeBase.update(currentProjectId, form.id, {
          knowledgebaseName: name,
          knowledgebaseDescription: form.description.trim(),
        })
        toast(t('kb.updated'))
      } else {
        await api.knowledgeBase.create(currentProjectId, name, form.description.trim())
        toast(t('kb.created'))
      }
      setForm(null)
      refresh()
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!pendingDelete) return
    const target = pendingDelete
    setPendingDelete(null)
    try {
      await api.knowledgeBase.remove(currentProjectId, target.kbId)
      toast(t('kb.deleted'))
      refresh()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  // 关闭动画期间对象已置空，文案会缺名字；用最后的值兜住。
  const kbDeleteShown = useLastValue(pendingDelete)

  return (
    <>
      <PageHeader
        title={t('kb.title')}
        desc={t('kb.desc')}
        actions={
          <PrimaryButton icon="i-lucide:plus" disabled={noProject} onClick={() => setForm({ name: '', description: '' })}>
            {t('kb.new')}
          </PrimaryButton>
        }
      />

      {noProject ? (
        <Panel>
          <EmptyState icon="i-lucide:folder" title={t('kb.noProject')} />
        </Panel>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-4">
          <div className="flex justify-end">
            <SearchBox value={pg.draft} onChange={pg.setDraft} onSubmit={pg.submit} placeholder={t('kb.searchPlaceholder')} />
          </div>
          <Panel className="flex-1 min-h-[320px]">
            <Table
              head={
                <>
                  <Th>{t('kb.col.name')}</Th>
                  <Th>{t('kb.col.files')}</Th>
                  <Th>{t('kb.col.size')}</Th>
                  <Th>{t('kb.col.updatedAt')}</Th>
                  <Th>{t('kb.col.actions')}</Th>
                </>
              }
            >
              {state !== 'ready' && <StateRow colSpan={5} state={state} error={error} />}
              {state === 'ready' && rows.length === 0 && (
                <tr>
                  <td colSpan={5}>
                    <EmptyState
                      icon="i-lucide:book-open"
                      title={t('kb.empty')}
                      action={
                        <PrimaryButton icon="i-lucide:plus" onClick={() => setForm({ name: '', description: '' })}>
                          {t('kb.new')}
                        </PrimaryButton>
                      }
                    />
                  </td>
                </tr>
              )}
              {state === 'ready' &&
                rows.map((kb) => (
                  <tr key={kb.kbId}>
                    <td className={TD}>
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-ink">{kb.knowledgebaseName}</div>
                        {kb.knowledgebaseDescription && (
                          <div className="mt-0.5 truncate text-xs text-ink-muted">{kb.knowledgebaseDescription}</div>
                        )}
                      </div>
                    </td>
                    <td className={TD}>
                      <Badge variant="secondary">{kb.fileCount ?? 0}</Badge>
                    </td>
                    <td className={TD}>{formatBytes(kb.totalSize)}</td>
                    <td className={TD}>{kb.updateTime || '—'}</td>
                    <td className={TD}>
                      <RowActions
                        actions={[
                          { label: t('kb.files'), icon: 'i-lucide:files', onClick: () => setFileTarget(kb) },
                          {
                            label: t('common.edit'),
                            icon: 'i-lucide:pencil',
                            onClick: () =>
                              setForm({ id: kb.kbId, name: kb.knowledgebaseName, description: kb.knowledgebaseDescription ?? '' }),
                          },
                          { label: t('common.delete'), icon: 'i-lucide:trash-2', tone: 'danger', onClick: () => setPendingDelete(kb) },
                        ]}
                      />
                    </td>
                  </tr>
                ))}
            </Table>
          </Panel>
          <Pager page={pg.page} pageSize={pg.pageSize} total={data?.total ?? 0} onPage={pg.setPage} />
        </div>
      )}

      <Dialog
        open={!!form}
        title={form?.id ? t('kb.editTitle') : t('kb.createTitle')}
        onClose={() => setForm(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setForm(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="primary" loading={saving} onClick={save}>
              {t('common.save')}
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <div className="space-y-1.5">
            <label className="block text-sm font-medium text-ink-secondary">{t('kb.form.name')}</label>
            <Input
              autoFocus
              value={form?.name ?? ''}
              onChange={(e) => setForm((prev) => (prev ? { ...prev, name: e.target.value } : prev))}
              placeholder={t('kb.form.namePlaceholder')}
            />
          </div>
          <div className="space-y-1.5">
            <label className="block text-sm font-medium text-ink-secondary">{t('kb.form.description')}</label>
            <Input
              value={form?.description ?? ''}
              onChange={(e) => setForm((prev) => (prev ? { ...prev, description: e.target.value } : prev))}
              placeholder={t('kb.form.descriptionPlaceholder')}
            />
          </div>
        </div>
      </Dialog>

      <Dialog
        open={!!pendingDelete}
        title={t('kb.deleteTitle')}
        onClose={() => setPendingDelete(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={remove}>
              {t('common.delete')}
            </Button>
          </>
        }
      >
        {t('kb.deleteConfirm')(kbDeleteShown?.knowledgebaseName ?? '')}
      </Dialog>

      {fileTarget && (
        <KnowledgeFilesDialog
          projectId={currentProjectId}
          kb={fileTarget}
          onClose={() => {
            setFileTarget(null)
            refresh()
          }}
        />
      )}
    </>
  )
}

function KnowledgeFilesDialog({ projectId, kb, onClose }: { projectId: string; kb: KnowledgeBaseRow; onClose: () => void }) {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const [page, setPage] = useState(1)
  const [tick, setTick] = useState(0)
  const [uploading, setUploading] = useState(false)
  /** 删文件是破坏性操作：点了先问一句，确定后再发请求 */
  const [pendingFile, setPendingFile] = useState<KnowledgeFile | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const { data, state, error, reload } = useAsync(
    () => api.knowledgeBase.fileList(projectId, kb.kbId, page, 10),
    [projectId, kb.kbId, page, tick],
    { key: 'knowledgebase.files' },
  )
  const files = data?.records ?? []

  const upload = async (file: File) => {
    setUploading(true)
    try {
      const content = await readDataURL(file)
      await api.knowledgeBase.fileAdd(projectId, kb.kbId, { name: file.name, content })
      toast(t('kb.fileAdded'))
      setTick((x) => x + 1)
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setUploading(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  const fileDeleteShown = useLastValue(pendingFile)

  const remove = async (f: KnowledgeFile) => {
    setPendingFile(null)
    try {
      await api.knowledgeBase.fileRemove(projectId, [f.fileId])
      toast(t('kb.fileDeleted'))
      reload()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  return (
    <Dialog open size="lg" title={`${t('kb.filesTitle')} · ${kb.knowledgebaseName}`} onClose={onClose}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-xs text-ink-muted">{t('kb.filesHint')}</p>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) void upload(file)
          }}
        />
        <Button variant="secondary" loading={uploading} onClick={() => inputRef.current?.click()}>
          {t('kb.upload')}
        </Button>
      </div>

      <div className="overflow-hidden rounded-xl border border-line">
        <Table
          head={
            <>
              <Th>{t('kb.file.name')}</Th>
              <Th>{t('kb.col.size')}</Th>
              <Th>{t('kb.file.createdAt')}</Th>
              <Th>{t('kb.col.actions')}</Th>
            </>
          }
        >
          {state !== 'ready' && <StateRow colSpan={4} state={state} error={error} />}
          {state === 'ready' && files.length === 0 && (
            <tr>
              <td colSpan={4}>
                <EmptyState compact icon="i-lucide:file-text" title={t('kb.fileEmpty')} />
              </td>
            </tr>
          )}
          {state === 'ready' &&
            files.map((f) => (
              <tr key={f.fileId}>
                <td className={TD}>{f.name}</td>
                <td className={TD}>{formatBytes(f.size)}</td>
                <td className={TD}>{f.createTime || '—'}</td>
                <td className={TD}>
                  <RowActions
                    actions={[
                      {
                        label: t('kb.download'),
                        icon: 'i-lucide:download',
                        onClick: () => window.open(api.knowledgeBase.fileDownloadUrl(projectId, f.fileId), '_blank'),
                      },
                      { label: t('common.delete'), icon: 'i-lucide:trash-2', tone: 'danger', onClick: () => setPendingFile(f) },
                    ]}
                  />
                </td>
              </tr>
            ))}
        </Table>
      </div>
      <Pager page={page} pageSize={10} total={data?.total ?? 0} onPage={setPage} />

      <Dialog
        open={!!pendingFile}
        size="sm"
        title={t('kb.deleteFileTitle')}
        onClose={() => setPendingFile(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingFile(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => pendingFile && remove(pendingFile)}>
              {t('common.delete')}
            </Button>
          </>
        }
      >
        {t('kb.deleteFileConfirm')(fileDeleteShown?.name ?? '')}
      </Dialog>
    </Dialog>
  )
}
