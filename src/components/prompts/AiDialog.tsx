/** AI 改写：写一句要求，先看结果与校验，再决定是否替换正文。 */

import { useState } from 'react'
import type { PromptValidation } from '../../lib/api'
import { useT } from '../../lib/store'
import { Dialog } from '../ui'
import { Button } from '../ui/button'
import { PromptCodeEditor } from './PromptCodeEditor'

export function AiDialog({
  open,
  onClose,
  syntax,
  busy,
  retryIn,
  error,
  result,
  onGenerate,
  onApply,
}: {
  open: boolean
  onClose: () => void
  syntax: string
  busy: boolean
  retryIn: number
  error: string
  result: { body: string; validation: PromptValidation } | null
  onGenerate: (instruction: string) => void
  onApply: (body: string) => void
}) {
  const t = useT()
  // 对话框每次打开都会重新挂载（父组件条件渲染），所以初始值直接用默认那句就够
  const [instruction, setInstruction] = useState(() => t('prompts.ai.defaultInstruction'))

  return (
    <Dialog
      open={open}
      title={t('prompts.ai.title')}
      onClose={onClose}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          {result ? (
            <Button variant="primary" onClick={() => onApply(result.body)}>
              {t('prompts.ai.apply')}
            </Button>
          ) : (
            <Button
              variant="primary"
              loading={busy}
              disabled={!instruction.trim() || retryIn > 0}
              onClick={() => onGenerate(instruction.trim())}
            >
              {retryIn > 0 ? t('prompts.test.retryIn')(retryIn) : t('prompts.ai.submit')}
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-3">
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium text-ink-secondary">{t('prompts.ai.instruction')}</span>
          <textarea
            rows={3}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            className="studio-test-textarea"
            placeholder={t('prompts.ai.instructionPlaceholder')}
          />
        </label>

        {error && (
          <p className="text-xs leading-5 text-warning" role="alert">
            {error}
          </p>
        )}

        {result && (
          <div className="space-y-2">
            <p className="text-xs font-medium text-ink-secondary">{t('prompts.ai.result')}</p>
            <PromptCodeEditor
              value={result.body}
              syntax={syntax}
              readOnly
              ariaLabel={t('prompts.ai.result')}
              className="studio-code--short"
            />
            {!result.validation.ok ? (
              <p className="text-xs leading-5 text-warning" role="alert">
                {t('prompts.ai.validationFailed')(result.validation.errors.map((e) => e.message).join('；'))}
              </p>
            ) : (
              <p className="text-xs leading-5 text-success">{t('prompts.validation.ok')}</p>
            )}
          </div>
        )}
      </div>
    </Dialog>
  )
}
