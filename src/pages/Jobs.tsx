import { useState } from 'react'
import { api, type Job } from '../lib/api'
import { ListPage } from '../components/ListPage'
import { Dialog, Tag } from '../components/ui'
import { RowActions } from '../components/ui/row-actions'
import { FilterField, FilterPopover, FilterSelect } from '../components/FilterPopover'
import { useLang, useT, useToast } from '../lib/store'
import { errText } from '../lib/i18n'
import { useLastValue } from '../lib/useLastValue'

/**
 * 异步任务（死信）页。只给平台管理员：接口是 `auth.Platform`。
 *
 * 这里出现的 `failed` 就是「重试用尽、没人处理就一直躺着」的任务。写记忆走队列
 * 是为了不阻塞对话，代价是失败必须有人看见 —— 调用日志里那条 `addMessage` 会
 * 一直显示 FAILED，但真正能补救的地方是这一页。
 */

/** 失败在前、时间倒序由后端给；这里只决定颜色 */
const statusTone = (s: string) => (s === 'succeeded' ? 'ok' : s === 'failed' ? 'warn' : 'default')

/** 任务 ID 太长，表格里留前后各 8 位；完整值在重放弹窗里 */
const shortId = (id: string) => (id.length > 20 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id)

export function Jobs() {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const [status, setStatus] = useState('failed')
  const [draft, setDraft] = useState('failed')
  const [target, setTarget] = useState<Job | null>(null)
  const [force, setForce] = useState(false)
  const [busy, setBusy] = useState(false)
  const [retryError, setRetryError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  // 关闭动画期间 target 已置空，内容区会整个消失；用最后的值兜住。
  const shownTarget = useLastValue(target)

  const openRetry = (job: Job) => {
    setTarget(job)
    setForce(false)
    setRetryError('')
  }

  const closeRetry = () => {
    if (busy) return
    setTarget(null)
  }

  const doRetry = async () => {
    if (!target) return
    setBusy(true)
    setRetryError('')
    try {
      await api.jobs.retry(target.id, force)
      setTarget(null)
      setReloadKey((k) => k + 1)
      toast(t('jobs.retry.done'), 'ok')
    } catch (e) {
      // 409 是「这条失败在可能已写入上游的位置」，不是系统故障：留在弹窗里，
      // 让人看到原因后决定要不要勾强制重放，而不是弹个 toast 就没了。
      setRetryError(errText(lang, e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <ListPage<Job>
        title={t('jobs.title')}
        desc={t('jobs.desc')}
        rowKey={(r) => r.id}
        deps={[status, reloadKey]}
        filter={(close) => (
          <FilterPopover
            open
            onFilter={() => {
              setStatus(draft)
              close()
            }}
            onReset={() => {
              setDraft('')
              setStatus('')
            }}
          >
            <FilterField label={t('jobs.filterStatus')}>
              <FilterSelect
                value={draft}
                onChange={setDraft}
                placeholder={t('filter.all')}
                options={[
                  { value: 'failed', label: 'failed' },
                  { value: 'pending', label: 'pending' },
                  { value: 'running', label: 'running' },
                  { value: 'succeeded', label: 'succeeded' },
                ]}
              />
            </FilterField>
          </FilterPopover>
        )}
        load={async (page, pageSize) => {
          const res = await api.jobs.list({
            status,
            limit: pageSize,
            offset: (page - 1) * pageSize,
          })
          return { items: res.records, total: res.total }
        }}
        cols={[
          {
            key: 'status',
            label: t('jobs.col.status'),
            render: (r) => <Tag text={r.status} tone={statusTone(r.status)} />,
          },
          {
            key: 'id',
            label: t('jobs.col.id'),
            render: (r) => (
              <div className="font-mono text-sm" title={r.id}>
                {shortId(r.id)}
              </div>
            ),
          },
          {
            key: 'attempts',
            label: t('jobs.col.attempts'),
            render: (r) => r.attempts,
          },
          {
            key: 'tenant_id',
            label: t('jobs.col.tenant'),
            render: (r) => r.tenant_id || '—',
          },
          {
            key: 'last_error',
            label: t('jobs.col.lastError'),
            wrap: true,
            render: (r) => <span className="text-ink-secondary">{r.last_error || '—'}</span>,
          },
          {
            key: 'updated_at',
            label: t('jobs.col.updatedAt'),
            render: (r) => r.updated_at,
          },
          {
            key: 'action',
            label: t('jobs.col.action'),
            render: (r) => (
              <RowActions
                actions={[
                  {
                    label: t('jobs.retry'),
                    icon: 'i-lucide:rotate-cw',
                    // 只有 failed 能重放：pending/running 再拉一次会重复写，
                    // succeeded 拉起来会写第二遍（后端也会 409）。
                    disabled: r.status !== 'failed',
                    onClick: () => openRetry(r),
                  },
                ]}
              />
            ),
          },
        ]}
      />

      <Dialog
        open={!!target}
        size="sm"
        title={t('jobs.retry.title')}
        onClose={closeRetry}
        footer={
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="cursor-pointer rounded-lg border border-line px-3 py-1.5 text-sm text-ink-secondary hover:bg-surface-hover"
              onClick={closeRetry}
              disabled={busy}
            >
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="cursor-pointer rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              onClick={doRetry}
              disabled={busy}
            >
              {busy ? t('common.loading') : t('jobs.retry.confirm')}
            </button>
          </div>
        }
      >
        {shownTarget && (
          <div className="space-y-3 text-sm">
            <div className="flex gap-2">
              <span className="w-[96px] shrink-0 text-ink-muted">{t('jobs.col.id')}</span>
              <span className="min-w-0 break-all font-mono text-ink-secondary">{shownTarget.id}</span>
            </div>
            <div className="flex gap-2">
              <span className="w-[96px] shrink-0 text-ink-muted">{t('jobs.col.lastError')}</span>
              <span className="min-w-0 break-all text-ink-secondary">{shownTarget.last_error || '—'}</span>
            </div>
            <p className="text-ink-muted">{t('jobs.retry.hint')}</p>
            <label className="flex cursor-pointer items-start gap-2 text-ink-secondary">
              <input type="checkbox" className="mt-0.5" checked={force} onChange={(e) => setForce(e.target.checked)} />
              <span>{t('jobs.retry.force')}</span>
            </label>
            {retryError && <p className="break-all text-danger">{retryError}</p>}
          </div>
        )}
      </Dialog>
    </>
  )
}
