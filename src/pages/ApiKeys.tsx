import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, type ApiKey } from '../lib/api'
import { useAsync, usePaged } from '../lib/useAsync'
import { useProject, useT, useToast, useLang } from '../lib/store'
import { Dialog, Pager, Panel, PageHeader, PrimaryButton, StateRow, Table, TD, TD_BREAK, Th, TextLinkButton } from '../components/ui'
import { RowActions } from '../components/ui/row-actions'
import { Icon } from '../components/Icon'
import { copyText } from '../lib/clipboard'
import { useLastValue } from '../lib/useLastValue'
import { errText } from '../lib/i18n'

import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { zodResolver } from '@hookform/resolvers/zod'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'

/**
 * 密钥列：**永远显示掩码**，明文不上屏（投屏、截图、肩窥都不漏）。
 * 复制动作在操作列，按 id 取明文写进剪贴板——列表一次吐出全部凭据的话，
 * 任何能看列表的请求都会拿到完整密钥；按 id 取让明文只在用户点的那一刻传输一次。
 */
function KeyCell({ k, maskHint, copyUnavailable }: { k: ApiKey; maskHint: string; copyUnavailable: string }) {
  return (
    <div className="font-mono text-sm text-ink-muted" title={k.canCopy ? maskHint : copyUnavailable}>
      {k.apiKey || '—'}
    </div>
  )
}

/** 校验消息也走字典：硬编码中文在英文界面里会冒出来（AGENTS 第 3 条）。 */
const makeCreateKeySchema = (t: ReturnType<typeof useT>) =>
  z.object({
    name: z.string().min(1, t('apikeys.nameRequired')).max(64, t('apikeys.nameTooLong')),
  })

type CreateKeyForm = z.infer<ReturnType<typeof makeCreateKeySchema>>

export function ApiKeys() {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const pg = usePaged(10)
  const { currentProjectId, ready } = useProject()
  const queryClient = useQueryClient()
  const refreshChatState = () => {
    void queryClient.invalidateQueries({ queryKey: ['useAsync', 'chat.state', currentProjectId] })
  }
  const { data, state, error, reload } = useAsync(
    () => api.keys.list(currentProjectId, pg.page, pg.pageSize),
    [currentProjectId, pg.page, pg.pageSize],
    { skip: !ready },
  )

  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<ApiKey | null>(null)
  /** 这一把密钥的明文是否已经复制过——没复制就关弹窗，只能重新生成，所以显眼提示 */
  const [createdCopied, setCreatedCopied] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<ApiKey | null>(null)
  const [renaming, setRenaming] = useState<ApiKey | null>(null)
  const [renameValue, setRenameValue] = useState('')

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<CreateKeyForm>({
    resolver: zodResolver(useMemo(() => makeCreateKeySchema(t), [t])),
    defaultValues: { name: '' },
  })

  /** 列表里的「复制」：列表只有掩码，明文按 id 取回来再写剪贴板。 */
  const copyKey = async (k: ApiKey) => {
    try {
      const res = await api.keys.copy(k.id)
      await copyText(res.apiKey)
      toast(t('common.copied'))
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  const copy = async (text: string) => {
    try {
      await copyText(text)
      toast(t('common.copied'))
    } catch {
      toast(t('common.copyFailed'), 'err')
    }
  }

  const submitCreate = handleSubmit(async (values) => {
    try {
      const k = await api.keys.create(currentProjectId, values.name.trim())
      refreshChatState()
      setCreating(false)
      reset()
      setCreatedCopied(false)
      setCreated(k)
      reload()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  })

  const doDelete = async () => {
    if (!pendingDelete) return
    const target = pendingDelete
    setPendingDelete(null)
    try {
      await api.keys.revoke(target.id)
      refreshChatState()
      toast(t('apikeys.deleted')(target.keyName))
      reload()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  /**
   * 重新生成密钥：签发新的 → 吊销旧的。顺序是有意的 —— 先吊销再签发时，
   * 中间任何一步失败（网络、配额）都会让人手里一个可用凭据都没有；反过来最多
   * 多出一条密钥，列表里点删除即可。
   */
  const [regeneratingId, setRegeneratingId] = useState('')
  /** 重新生成会立刻吊销旧密钥，必须先确认——误点一次就是一次线上凭据失效 */
  const [confirmRegenerate, setConfirmRegenerate] = useState<ApiKey | null>(null)
  const regenerate = async (k: ApiKey) => {
    setRegeneratingId(k.id)
    setConfirmRegenerate(null)
    try {
      const fresh = await api.keys.create(currentProjectId, k.keyName)
      try {
        await api.keys.revoke(k.id)
      } catch {
        // 旧的没吊销成功不影响新的使用，列表里还能手动删；不打断「新密钥已就绪」。
      }
      refreshChatState()
      setCreatedCopied(false)
      setCreated(fresh)
      reload()
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setRegeneratingId('')
    }
  }

  const doRename = async () => {
    if (!renaming) return
    const name = renameValue.trim()
    if (!name) {
      toast(t('apikeys.nameRequired'), 'err')
      return
    }
    try {
      await api.keys.rename(renaming.id, name)
      setRenaming(null)
      toast(t('apikeys.renamed'))
      reload()
    } catch (e) {
      toast(errText(lang, e), 'err')
    }
  }

  // 关闭动画期间对象已被置空，文案会变成「确定重新生成『』？」；用最后的值兜住。
  const regenShown = useLastValue(confirmRegenerate)
  const deleteShown = useLastValue(pendingDelete)

  const noProject = ready && !currentProjectId

  return (
    <>
      <PageHeader
        title={t('apikeys.title')}
        actions={
          <span data-guide="create-key">
            <PrimaryButton icon="i-lucide:plus" disabled={noProject} onClick={() => setCreating(true)}>
              {t('apikeys.new')}
            </PrimaryButton>
          </span>
        }
      />

      <div className="flex min-h-0 flex-1 flex-col gap-4">
        <div className="flex-1">
          <div className="flex h-full min-h-0 flex-col gap-3">
            <Panel className="flex-1 min-h-0">
              <Table
                head={
                  <>
                    <Th>{t('apikeys.col.name')}</Th>
                    <Th>{t('apikeys.col.key')}</Th>
                    <Th>{t('apikeys.col.lastUsed')}</Th>
                    <Th>{t('apikeys.col.createdAt')}</Th>
                    <Th>{t('apikeys.col.action')}</Th>
                  </>
                }
              >
                {noProject && <StateRow colSpan={5} state="error" error={t('apikeys.noProject')} />}
                {!noProject && state !== 'ready' && <StateRow colSpan={5} state={state} error={error} />}
                {!noProject && state === 'ready' && data?.records.length === 0 && (
                  <StateRow
                    colSpan={4}
                    state="ready"
                    emptyTitle={t('apikeys.empty')}
                    emptyAction={
                      <PrimaryButton icon="i-lucide:plus" onClick={() => setCreating(true)}>
                        {t('apikeys.new')}
                      </PrimaryButton>
                    }
                  />
                )}
                {!noProject &&
                  state === 'ready' &&
                  data?.records.map((k) => (
                    <tr key={k.id} data-selectable="true" className="data-[selected=true]:bg-elevated/50">
                      <td data-pinned="false" className={TD_BREAK}>
                        {k.keyName}
                      </td>
                      <td data-pinned="false" className={TD_BREAK}>
                        <KeyCell k={k} maskHint={t('apikeys.maskHint')} copyUnavailable={t('apikeys.copyUnavailable')} />
                      </td>
                      <td data-pinned="false" className={`${TD}`}>
                        {k.lastUsedAt ?? t('apikeys.neverUsed')}
                      </td>
                      <td data-pinned="false" className={`${TD}`}>
                        {k.createTime}
                      </td>
                      <td data-pinned="false" className={`${TD}`}>
                        <RowActions
                          actions={[
                            // 复制是行内最常用的动作，和改名/删除放在一起；
                            // 没有写权限的人不显示这一项（前端只做显隐，后端 keys/copy 仍会拒）。
                            ...(k.canCopy
                              ? [
                                  {
                                    label: t('apikeys.copy'),
                                    icon: 'i-lucide:copy',
                                    onClick: () => copyKey(k),
                                  },
                                ]
                              : []),
                            {
                              label: t('apikeys.regenerate'),
                              icon: 'i-lucide:rotate-ccw',
                              disabled: regeneratingId === k.id,
                              onClick: () => setConfirmRegenerate(k),
                            },
                            {
                              label: t('apikeys.rename'),
                              icon: 'i-lucide:pencil',
                              onClick: () => {
                                setRenameValue(k.keyName)
                                setRenaming(k)
                              },
                            },
                            { label: t('apikeys.delete'), icon: 'i-lucide:trash-2', tone: 'danger', onClick: () => setPendingDelete(k) },
                          ]}
                        />
                      </td>
                    </tr>
                  ))}
              </Table>
            </Panel>

            <Pager page={pg.page} pageSize={pg.pageSize} total={data?.total ?? 0} onPage={pg.setPage} />
          </div>
        </div>
      </div>

      <Dialog open={creating} title={t('apikeys.addApikey')} onClose={() => setCreating(false)}>
        <form className="space-y-4" onSubmit={submitCreate}>
          <div className="space-y-1.5">
            <label className="block text-sm font-medium text-ink-secondary">{t('apikeys.dialog.name')}</label>
            <Input autoFocus {...register('name')} error={!!errors.name} placeholder={t('apikeys.dialog.namePlaceholder')} />
            {errors.name && <p className="text-xs text-danger font-medium">{errors.name.message}</p>}
          </div>
          <div className="flex justify-end gap-2.5 pt-2">
            <Button type="button" variant="secondary" onClick={() => setCreating(false)}>
              {t('common.cancel')}
            </Button>
            <Button type="submit" variant="primary" loading={isSubmitting}>
              {t('common.create')}
            </Button>
          </div>
        </form>
      </Dialog>

      <Dialog
        open={!!created}
        title={t('apikeys.addApikey')}
        onClose={() => setCreated(null)}
        footer={<PrimaryButton onClick={() => setCreated(null)}>{t('apikeys.dialog.done')}</PrimaryButton>}
      >
        <p className="mb-3 text-ink-muted">{t('apikeys.dialog.created')}</p>
        <div className="mb-3 rounded-lg border border-line bg-surface-muted px-3 py-2 text-xs text-ink-secondary">
          {t('apikeys.onlyOnce')}
        </div>
        {/* 控制面新架构里密钥落 SQLite、每次请求即时校验，不再有「重启网关才生效」这一步。 */}
        <p className="mb-3 text-ink-muted">{t('apikeys.createdNotice')}</p>
        <div className="flex items-center gap-2 rounded-[8px] bg-surface-muted border border-line px-3 py-2">
          <code className="flex-1 font-mono text-xs break-all text-ink">{created?.apiKey}</code>
          <TextLinkButton
            onClick={async () => {
              await copy(created?.apiKey ?? '')
              setCreatedCopied(true)
            }}
          >
            <Icon name={createdCopied ? 'i-lucide:check' : 'i-lucide:copy'} className="shrink-0 size-4" />
            {createdCopied ? t('apikeys.dialog.copied') : t('apikeys.dialog.copy')}
          </TextLinkButton>
        </div>
      </Dialog>

      <Dialog
        open={!!renaming}
        title={t('apikeys.renameTitle')}
        onClose={() => setRenaming(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRenaming(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="primary" onClick={doRename}>
              {t('common.save')}
            </Button>
          </>
        }
      >
        <div className="space-y-1.5">
          <label className="block text-sm font-medium text-ink-secondary">{t('apikeys.dialog.name')}</label>
          <Input
            autoFocus
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            placeholder={t('apikeys.dialog.namePlaceholder')}
          />
          <p className="text-xs text-ink-muted">{t('apikeys.renameHint')}</p>
        </div>
      </Dialog>

      <Dialog
        open={!!confirmRegenerate}
        title={t('apikeys.regenerateTitle')}
        onClose={() => setConfirmRegenerate(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirmRegenerate(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => confirmRegenerate && regenerate(confirmRegenerate)}>
              {t('apikeys.regenerateConfirm')}
            </Button>
          </>
        }
      >
        {t('apikeys.confirmRegenerate')(regenShown?.keyName ?? '')}
      </Dialog>

      <Dialog
        open={!!pendingDelete}
        title={t('apikeys.deleteApikey')}
        onClose={() => setPendingDelete(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingDelete(null)}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={doDelete}>
              {t('common.delete')}
            </Button>
          </>
        }
      >
        {t('apikeys.confirmDelete')(deleteShown?.keyName ?? '')}
      </Dialog>
    </>
  )
}
