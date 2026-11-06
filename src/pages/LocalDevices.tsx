import { useMemo, useState } from 'react'
import { api, type LocalDevice } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { Badge } from '../components/ui/badge'
import { Button } from '../components/ui/button'
import { Dialog, Panel, StateRow, Table, TD, Th } from '../components/ui'
import { EmptyState } from '../components/ui/empty-state'
import { RowActions } from '../components/ui/row-actions'
import { Icon } from '../components/Icon'
import { copyText } from '../lib/clipboard'
import { formatBytes, formatDateTime } from '../lib/format'
import { errText } from '../lib/i18n'
import { useLang, useToast, useT } from '../lib/store'

/** 一条配额进度条。超限用 danger，接近上限用 warning，其余用强调色。 */
function QuotaBar({ label, used, limit, format }: { label: string; used: number; limit: number; format: (n: number) => string }) {
  const percent = limit > 0 ? Math.min(100, (used / limit) * 100) : 0
  const tone = percent >= 100 ? 'bg-danger' : percent >= 80 ? 'bg-warning' : 'bg-accent'
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-3 text-xs">
        <span className="text-ink-muted">{label}</span>
        <span className="font-medium tabular-nums text-ink-secondary">
          {format(used)} / {format(limit)}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-surface-hover">
        <div className={`h-full rounded-full transition-all ${tone}`} style={{ width: `${percent}%` }} />
      </div>
    </div>
  )
}

const STATUS_KEY = {
  active: 'devices.status.active',
  revoked: 'devices.status.revoked',
} as const

/** 项目设置里的「本地设备」页。 */
export function LocalDevices({
  projectId,
  projectName,
  ready,
  canManage,
}: {
  projectId: string
  projectName: string
  ready: boolean
  canManage: boolean
}) {
  const t = useT()
  const { lang } = useLang()
  const toast = useToast()
  const [tick, setTick] = useState(0)
  const [pendingRevoke, setPendingRevoke] = useState<LocalDevice | null>(null)
  const [revoking, setRevoking] = useState(false)

  const { data, state, error } = useAsync(() => api.localSync.devices(projectId), [projectId, tick], {
    skip: !ready || !projectId,
    key: 'localSync.devices',
  })
  const devices = useMemo(() => data?.devices ?? [], [data])
  const quota = data?.quota
  const activeCount = devices.filter((d) => d.status === 'active').length

  const copyProjectId = async () => {
    try {
      await copyText(projectId)
      toast(t('common.copied'))
    } catch {
      toast(t('common.copyFailed'), 'err')
    }
  }

  const doRevoke = async () => {
    if (!pendingRevoke) return
    const target = pendingRevoke
    setRevoking(true)
    try {
      await api.localSync.revokeDevice(projectId, target.id)
      toast(t('devices.revokedToast'))
      setPendingRevoke(null)
      setTick((x) => x + 1)
    } catch (e) {
      toast(errText(lang, e), 'err')
    } finally {
      setRevoking(false)
    }
  }

  const statusBadge = (device: LocalDevice) => {
    if (device.status === 'revoked') return <Badge variant="destructive">{t(STATUS_KEY.revoked)}</Badge>
    if (device.status === 'active') return <Badge variant="success">{t(STATUS_KEY.active)}</Badge>
    return <Badge variant="secondary">{device.status}</Badge>
  }

  if (!projectId) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <Panel className="min-h-72 flex-1">
          <EmptyState icon="i-lucide:folder-plus" title={t('devices.noProject.title')} desc={t('devices.noProject.desc')} />
        </Panel>
      </div>
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {quota && (
        <Panel className="shrink-0">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-ink">{t('devices.quota.title')}</div>
              <div className="mt-0.5 text-xs text-ink-muted">{t('devices.quota.desc')}</div>
              {(quota.pendingBytes ?? 0) > 0 || (quota.pendingRows ?? 0) > 0 ? (
                <div className="mt-0.5 text-xs text-warning">
                  {t('devices.quota.pending')(
                    formatBytes(quota.pendingBytes ?? 0),
                    (quota.pendingRows ?? 0).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US'),
                  )}
                </div>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Badge variant="outline">{t('devices.activeCount')(activeCount, devices.length)}</Badge>
              {(quota.limitBytes > 0 && quota.usedBytes + (quota.pendingBytes ?? 0) >= quota.limitBytes) ||
              (quota.limitRows > 0 && quota.usedRows + (quota.pendingRows ?? 0) >= quota.limitRows) ? (
                <Badge variant="destructive">{t('devices.quota.exceeded')}</Badge>
              ) : null}
            </div>
          </div>
          <div className="grid gap-4 px-4 py-4 md:grid-cols-2">
            <QuotaBar
              label={t('devices.quota.bytes')}
              used={quota.usedBytes + (quota.pendingBytes ?? 0)}
              limit={quota.limitBytes}
              format={formatBytes}
            />
            <QuotaBar
              label={t('devices.quota.rows')}
              used={quota.usedRows + (quota.pendingRows ?? 0)}
              limit={quota.limitRows}
              format={(n) => n.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US')}
            />
          </div>
        </Panel>
      )}

      <Panel className="min-h-72 flex-1">
        <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <div className="min-w-0">
            <div className="text-sm font-medium text-ink">{t('devices.title')}</div>
            <div className="mt-0.5 text-xs text-ink-muted">{t('devices.desc')}</div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {!canManage && <span className="text-xs text-ink-muted">{t('devices.readOnly')}</span>}
            <Button variant="secondary" size="sm" onClick={() => setTick((x) => x + 1)}>
              <Icon name="i-lucide:rotate-ccw" className="size-3.5" />
              {t('common.refresh')}
            </Button>
          </div>
        </div>

        {state === 'ready' && devices.length === 0 ? (
          <EmptyState icon="i-lucide:monitor" title={t('devices.empty.title')} desc={t('devices.empty.desc')} />
        ) : (
          <Table
            head={
              <>
                <Th>{t('devices.col.device')}</Th>
                <Th>{t('devices.col.agent')}</Th>
                <Th>{t('devices.col.status')}</Th>
                <Th>{t('devices.col.lastSync')}</Th>
                <Th>{t('devices.col.createdAt')}</Th>
                <Th>{t('devices.col.actions')}</Th>
              </>
            }
          >
            {state !== 'ready' && <StateRow colSpan={6} state={state} error={error} />}
            {state === 'ready' &&
              devices.map((device) => (
                <tr key={device.id}>
                  <td data-pinned="false" className={TD}>
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-ink">{device.label || device.deviceId}</div>
                      <div className="truncate font-mono text-[11px] text-ink-faint">{device.deviceId}</div>
                    </div>
                  </td>
                  <td data-pinned="false" className={TD}>
                    {device.agentKind || <span className="text-ink-faint">—</span>}
                  </td>
                  <td data-pinned="false" className={TD}>
                    {statusBadge(device)}
                  </td>
                  <td data-pinned="false" className={TD}>
                    {formatDateTime(device.lastSeenAt, lang, t('devices.neverSynced'))}
                  </td>
                  <td data-pinned="false" className={TD}>
                    {formatDateTime(device.createdAt, lang)}
                  </td>
                  <td data-pinned="false" className={TD}>
                    {canManage && device.status !== 'revoked' ? (
                      <RowActions
                        actions={[
                          {
                            label: t('devices.action.revoke'),
                            icon: 'i-lucide:ban',
                            tone: 'danger',
                            onClick: () => setPendingRevoke(device),
                          },
                        ]}
                      />
                    ) : (
                      <span className="text-ink-faint">—</span>
                    )}
                  </td>
                </tr>
              ))}
          </Table>
        )}
      </Panel>

      <Panel className="shrink-0">
        <div className="border-b border-line px-4 py-3">
          <div className="text-sm font-medium text-ink">{t('devices.guide.title')}</div>
          <div className="mt-0.5 text-xs text-ink-muted">{t('devices.guide.desc')}</div>
        </div>
        <div className="space-y-3 px-4 py-4">
          <ol className="list-decimal space-y-1.5 pl-5 text-xs leading-5 text-ink-secondary">
            <li>{t('devices.guide.step1')}</li>
            <li>{t('devices.guide.step2')}</li>
            <li>{t('devices.guide.step3')}</li>
            <li>{t('devices.guide.step4')}</li>
          </ol>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-muted/60 px-3 py-2">
            <span className="text-[11px] font-medium text-ink-muted">{t('devices.guide.projectId')}</span>
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-secondary">
              {projectName ? `${projectName} · ${projectId}` : projectId}
            </span>
            <Button variant="secondary" size="sm" onClick={() => void copyProjectId()} disabled={!projectId}>
              <Icon name="i-lucide:copy" className="size-3.5" />
              {t('devices.guide.copy')}
            </Button>
          </div>
          <p className="text-[11px] leading-4 text-ink-muted">{t('devices.guide.note')}</p>
        </div>
      </Panel>

      <Dialog
        open={!!pendingRevoke}
        title={t('devices.revokeTitle')}
        onClose={() => !revoking && setPendingRevoke(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setPendingRevoke(null)} disabled={revoking}>
              {t('common.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => void doRevoke()} disabled={revoking}>
              {revoking ? t('common.loading') : t('common.confirm')}
            </Button>
          </>
        }
      >
        <p>{t('devices.revokeConfirm')(pendingRevoke?.label || pendingRevoke?.deviceId || '')}</p>
        <p className="mt-2 text-xs leading-5 text-ink-muted">{t('devices.revokeNote')}</p>
      </Dialog>
    </div>
  )
}
