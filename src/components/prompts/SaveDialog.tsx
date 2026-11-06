/** 保存对话框：先看校验结果，再决定是否带着「没试跑过」的确认保存。 */

import { useState } from 'react'
import type { PromptValidation } from '../../lib/api'
import { useT } from '../../lib/store'
import { versionLabel } from '../../lib/prompts'
import { Dialog } from '../ui'
import { Button } from '../ui/button'
import { Icon } from '../Icon'

export function SaveDialog({
  open,
  onClose,
  validation,
  confirmRequired,
  saving,
  error,
  conflict,
  maxNote,
  onSubmit,
  onReloadLatest,
  onCompareConflict,
}: {
  open: boolean
  onClose: () => void
  validation: PromptValidation | null
  /** 草稿没试跑过、或最近一次试跑没通过：必须勾选确认 */
  confirmRequired: boolean
  saving: boolean
  error: string
  conflict: { currentVersionId: number | null; currentVersionNo?: number | null } | null
  maxNote: number
  onSubmit: (note: string) => void
  onReloadLatest: () => void
  onCompareConflict: () => void
}) {
  const t = useT()
  // 每次打开都重新挂载（父组件条件渲染），备注与勾选状态自然清空
  const [note, setNote] = useState('')
  const [confirmed, setConfirmed] = useState(false)

  const hasErrors = Boolean(validation && !validation.ok)
  const canSubmit = !hasErrors && (!confirmRequired || confirmed) && !saving

  return (
    <Dialog
      open={open}
      title={conflict ? t('prompts.conflict.title') : t('prompts.save.title')}
      onClose={onClose}
      footer={
        conflict ? (
          <>
            <Button variant="secondary" onClick={onCompareConflict}>
              {t('prompts.conflict.compare')}
            </Button>
            <Button variant="primary" onClick={onReloadLatest}>
              {t('prompts.conflict.reload')}
            </Button>
          </>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>
              {t('common.cancel')}
            </Button>
            <Button variant="primary" loading={saving} disabled={!canSubmit} onClick={() => onSubmit(note.trim())}>
              {t('prompts.save.submit')}
            </Button>
          </>
        )
      }
    >
      {conflict ? (
        <div className="space-y-3 text-sm leading-6 text-ink-secondary">
          <p>
            {t('prompts.conflict.desc')(
              (conflict.currentVersionNo ?? 0) > 0
                ? versionLabel(conflict.currentVersionNo ?? undefined, 0)
                : t('prompts.conflict.defaultVersion'),
            )}
          </p>
          <p className="text-xs leading-5 text-ink-muted">{t('prompts.conflict.hint')}</p>
        </div>
      ) : (
        <div className="space-y-4">
          {validation && (
            <div className="space-y-2">
              {validation.errors.map((e) => (
                <div key={e.code + e.message} className="studio-alert studio-alert--danger" role="alert">
                  <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
                  <span className="text-xs leading-5">{e.message}</span>
                </div>
              ))}
              {validation.warnings.map((w) => (
                <div key={w.code + w.message} className="studio-alert studio-alert--warning">
                  <Icon name="i-lucide:alert-circle" className="mt-0.5 size-4 shrink-0" />
                  <span className="text-xs leading-5">{w.message}</span>
                </div>
              ))}
              {validation.ok && validation.warnings.length === 0 && (
                <p className="flex items-center gap-1.5 text-xs text-success">
                  <Icon name="i-lucide:check-circle-2" className="size-4" />
                  {t('prompts.validation.ok')}
                </p>
              )}
            </div>
          )}

          {confirmRequired && (
            <label className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning-soft px-3 py-2 text-xs leading-5 text-warning">
              <input
                type="checkbox"
                className="studio-checkbox mt-0.5"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              <span>{t('prompts.save.confirmCheckbox')}</span>
            </label>
          )}

          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-ink-secondary">{t('prompts.save.note')}</span>
            <input
              value={note}
              maxLength={maxNote}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t('prompts.save.notePlaceholder')}
              className="studio-input"
            />
            <span className="mt-1 block text-right text-[11px] text-ink-faint">
              {note.length}/{maxNote}
            </span>
          </label>

          {error && (
            <p className="text-xs leading-5 text-warning" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </Dialog>
  )
}
