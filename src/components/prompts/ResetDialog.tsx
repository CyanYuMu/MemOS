/** 还原默认：先看「当前内容与默认」的差异，再确认还原范围。 */

import { useT } from '../../lib/store'
import { Dialog } from '../ui'
import { Button } from '../ui/button'
import { DiffView } from './DiffView'

export type ResetScope = 'lang' | 'item' | 'project'

export function ResetDialog({
  open,
  onClose,
  scope,
  onScopeChange,
  currentBody,
  defaultBody,
  languageCount,
  customizedCount,
  resetting,
  error,
  onConfirm,
}: {
  open: boolean
  onClose: () => void
  scope: ResetScope
  onScopeChange: (s: ResetScope) => void
  currentBody: string
  defaultBody: string
  languageCount: number
  customizedCount: number
  resetting: boolean
  error: string
  onConfirm: () => void
}) {
  const t = useT()

  const option = (value: ResetScope, label: string, desc: string, disabled = false) => (
    <label
      className={`flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 ${
        scope === value ? 'border-accent/40 bg-accent-soft' : 'border-line bg-surface'
      } ${disabled ? 'cursor-not-allowed opacity-50' : ''}`}
    >
      <input
        type="radio"
        name="reset-scope"
        className="studio-checkbox mt-0.5"
        checked={scope === value}
        disabled={disabled}
        onChange={() => onScopeChange(value)}
      />
      <span>
        <span className="block text-xs font-medium text-ink">{label}</span>
        <span className="mt-0.5 block text-[11px] leading-5 text-ink-muted">{desc}</span>
      </span>
    </label>
  )

  return (
    <Dialog
      open={open}
      title={t('prompts.reset.title')}
      onClose={onClose}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="destructive" loading={resetting} disabled={resetting} onClick={onConfirm}>
            {t('prompts.reset.submit')}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="grid gap-2 sm:grid-cols-3">
          {option('lang', t('prompts.reset.scope.lang'), t('prompts.reset.scope.langDesc'))}
          {option('item', t('prompts.reset.scope.item'), t('prompts.reset.scope.itemDesc')(languageCount), languageCount <= 1)}
          {option(
            'project',
            t('prompts.reset.scope.project'),
            t('prompts.reset.scope.projectDesc')(customizedCount),
            customizedCount === 0,
          )}
        </div>

        {scope === 'project' ? (
          <p className="rounded-lg border border-line bg-surface-muted px-3 py-2 text-xs leading-5 text-ink-secondary">
            {t('prompts.reset.projectConfirm')(customizedCount)}
          </p>
        ) : (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-ink-secondary">{t('prompts.reset.diffTitle')}</p>
            <DiffView
              leftText={currentBody}
              rightText={defaultBody}
              leftLabel={t('prompts.diff.source.current')}
              rightLabel={t('prompts.diff.source.default')}
              mode="unified"
              className="max-h-64"
            />
          </div>
        )}

        {error && (
          <p className="text-xs leading-5 text-warning" role="alert">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  )
}
