import { useT } from '../../lib/store'

export function ChatComposerFooter() {
  const t = useT()
  return (
    <div className="memos-composer-hint">
      <span>{t('playground.composer.hint')}</span>
      <span>{t('playground.composer.disclaimer')}</span>
    </div>
  )
}
