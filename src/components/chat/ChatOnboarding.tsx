import { useEffect, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { ArrowRight, LoaderCircle, X } from 'lucide-react'
import { useLang, useMeta, useT } from '../../lib/store'
import { errText } from '../../lib/i18n'
import { GuideStepOverlay } from './GuideStepOverlay'
import type { OnboardingCheckpoint } from '../../lib/api'
import type { GuideProgress } from './guideProgress'

type Stage = 'welcome' | 'profile' | 'first' | 'logs' | 'recall' | 'finish' | 'closed'
interface Checkpoint {
  stage: Stage
  first?: string
  second?: string
}
interface Props {
  embedded?: boolean
  savedCheckpoint?: OnboardingCheckpoint
  onCheckpoint?: (checkpoint: OnboardingCheckpoint) => Promise<void>
  externalBusy?: boolean
  onComplete?: () => Promise<void>
  onDismiss?: () => Promise<void>
  onBack?: () => Promise<void>
  onRetryWrite?: () => Promise<void>
  projectId: string
  replay: boolean
  conversationId: string
  progress: GuideProgress | null
  onBegin: (text: string, title: string, purpose: 'intro' | 'recall') => Promise<string>
  onSelect: (id: string) => void
  onActive: (active: boolean) => void
  onLogs: () => void
  onReturn: () => void
  onRetry: () => Promise<unknown> | undefined
}
const cuisines = ['shandong', 'sichuan', 'cantonese', 'jiangsu', 'fujian', 'zhejiang', 'hunan', 'anhui'] as const
function seen(key: string) {
  try {
    return localStorage.getItem(key) === '1'
  } catch {
    return false
  }
}
function initialCheckpoint(key: string, legacy: string, replay: boolean): Checkpoint {
  if (!replay && (seen(key) || seen(legacy))) return { stage: 'closed' }
  try {
    const value = JSON.parse(localStorage.getItem(key + ':progress') || 'null') as Checkpoint | null
    if (
      value &&
      ['first', 'logs', 'recall', 'finish'].includes(value.stage) &&
      typeof value.first === 'string' &&
      value.first.length > 0 &&
      value.first.length <= 80 &&
      (!(value.stage === 'recall' || value.stage === 'finish') || (typeof value.second === 'string' && value.second.length > 0)) &&
      (!value.second || (typeof value.second === 'string' && value.second.length <= 80))
    )
      return value
  } catch {
    /* 存储不可用时从欢迎页重新开始，不重发旧消息。 */
  }
  return { stage: 'welcome' }
}

/** 等账号就绪后再决定首次状态，避免误用匿名存储键。 */
export function ChatOnboarding(props: Props) {
  const { account } = useMeta()
  const identity = account?.accountId || account?.username
  return identity ? <AccountGuide key={identity} {...props} identity={identity} /> : null
}

function AccountGuide({
  embedded,
  savedCheckpoint,
  onCheckpoint,
  externalBusy,
  onComplete,
  onDismiss,
  onBack,
  onRetryWrite,
  identity,
  projectId,
  replay,
  conversationId,
  progress,
  onBegin,
  onSelect,
  onActive,
  onLogs,
  onReturn,
  onRetry,
}: Props & { identity: string }) {
  const t = useT(),
    { lang } = useLang()
  const storageKey = `console.chat.guide.v2:${identity}:${projectId}`
  const seenKey = `console.chat.guide.v2:${identity}`
  const [checkpoint, setCheckpoint] = useState<Checkpoint>(() => {
    if (embedded && savedCheckpoint) return savedCheckpoint
    const value = initialCheckpoint(storageKey, `console.chat.guide.v1:${identity}`, replay)
    return embedded && value.stage === 'welcome' ? { stage: 'profile' } : value
  })
  // 新版完成状态按账号保存；未完成步骤按项目保存，不保存表单或对话正文。
  const [dismissed] = useState(() => !replay && seen(seenKey))
  const { stage, first, second } = checkpoint
  const saved = useRef(JSON.stringify(savedCheckpoint || null))
  const active = !dismissed && stage !== 'closed'
  const modal = active && (stage === 'welcome' || stage === 'profile')
  const [tourReady, setTourReady] = useState(() => !['welcome', 'profile'].includes(stage))
  const [name, setName] = useState(''),
    [preferences, setPreferences] = useState(''),
    [region, setRegion] = useState('')
  const [selectedCuisines, setSelectedCuisines] = useState<string[]>([])
  const [answerStyle, setAnswerStyle] = useState('')
  const [submitting, setSubmitting] = useState(false),
    [error, setError] = useState('')
  const [seconds, setSeconds] = useState(5)
  const startButton = useRef<HTMLButtonElement>(null),
    nameInput = useRef<HTMLInputElement>(null)
  const mounted = useRef(true),
    attempt = useRef(0)
  const leaving = useRef(false)
  const target = stage === 'recall' || stage === 'finish' ? second : first
  const current = progress?.conversationId === target ? progress : null

  useEffect(() => {
    onActive(active)
    return () => onActive(false)
  }, [active, onActive])
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      attempt.current += 1
    }
  }, [])
  useEffect(() => {
    // onBegin先选择新房间，准备好后才返回；等待期间不能抢回旧步骤的房间。
    if (!submitting && active && target && target !== conversationId) onSelect(target)
  }, [active, target, conversationId, onSelect, submitting])
  useEffect(() => {
    if (active && stage === 'logs' && current?.ready) onLogs()
  }, [active, stage, current?.ready, onLogs])
  useEffect(() => {
    if (!active) return
    try {
      localStorage.setItem(storageKey + ':progress', JSON.stringify(checkpoint))
    } catch {
      /* 不阻止当前步骤。 */
    }
  }, [active, stage, checkpoint, storageKey])
  useEffect(() => {
    if (stage !== 'recall') return
    const timer = setInterval(() => setSeconds((value) => Math.max(0, value - 1)), 1000)
    return () => clearInterval(timer)
  }, [stage])
  useEffect(() => {
    if (stage === 'profile') nameInput.current?.focus()
  }, [stage])
  useEffect(() => {
    if (!embedded || !onCheckpoint || !first || !['first', 'logs', 'recall', 'finish'].includes(stage)) return
    const value: OnboardingCheckpoint = { stage: stage as OnboardingCheckpoint['stage'], first, ...(second ? { second } : {}) }
    const fingerprint = JSON.stringify(value)
    if (saved.current === fingerprint) return
    saved.current = fingerprint
    void onCheckpoint(value).catch((e: unknown) => setError(errText(lang, e)))
  }, [embedded, onCheckpoint, first, second, stage, lang])
  async function leave(complete = false) {
    if (leaving.current) return
    if (!complete && onDismiss) {
      try {
        await onDismiss()
      } catch (e) {
        setError(errText(lang, e))
        return
      }
    }
    leaving.current = true
    attempt.current += 1
    try {
      localStorage.setItem(seenKey, '1')
      localStorage.setItem(storageKey, '1')
      if (complete || stage === 'welcome' || stage === 'profile') localStorage.removeItem(storageKey + ':progress')
    } catch {
      /* 当前页仍可退出。 */
    }
    setCheckpoint((old) => ({ ...old, stage: 'closed' }))
    onReturn()
  }
  async function finish() {
    if (submitting) return
    if (embedded && (!current?.recalled || current.recall !== 'succeeded')) return
    setSubmitting(true)
    try {
      await onComplete?.()
      leave(true)
    } catch (e) {
      setError(errText(lang, e))
    } finally {
      if (mounted.current) setSubmitting(false)
    }
  }
  async function introduce() {
    if (submitting || (!preferences.trim() && !selectedCuisines.length)) return
    const token = ++attempt.current
    setSubmitting(true)
    setError('')
    const parts = [
      name.trim() && t('playground.profile.intro.name')({ name: name.trim() }),
      selectedCuisines.length &&
        t('chat.guide.intro.cuisines')(
          selectedCuisines.map((key) => t(`chat.guide.cuisine.${key as (typeof cuisines)[number]}`)).join('、'),
        ),
      region.trim() && t('chat.guide.intro.region')(region.trim()),
      preferences.trim() && t('playground.profile.intro.preferences')({ preferences: preferences.trim() }),
      answerStyle && t('playground.profile.intro.style')({ style: answerStyle }),
      t('playground.profile.intro.remember'),
    ].filter(Boolean)
    try {
      const id = await onBegin(parts.join('\n'), t('chat.guide.introTitle'), 'intro')
      if (mounted.current && token === attempt.current) setCheckpoint({ stage: 'first', first: id })
    } catch (e) {
      if (mounted.current && token === attempt.current) setError(errText(lang, e))
    } finally {
      if (mounted.current && token === attempt.current) setSubmitting(false)
    }
  }
  async function recall() {
    if (submitting) return
    if (second) {
      setSeconds(5)
      setCheckpoint((old) => ({ ...old, stage: 'recall' }))
      return
    }
    const token = ++attempt.current
    setSubmitting(true)
    setError('')
    try {
      const id = await onBegin(t('chat.guide.recallQuestion'), t('chat.guide.recallTitle'), 'recall')
      if (mounted.current && token === attempt.current) {
        setSeconds(5)
        setCheckpoint((old) => ({ ...old, stage: 'recall', second: id }))
      }
    } catch (e) {
      if (mounted.current && token === attempt.current) setError(errText(lang, e))
    } finally {
      if (mounted.current && token === attempt.current) setSubmitting(false)
    }
  }
  async function retryWrite() {
    if (submitting || !onRetryWrite) return
    setSubmitting(true)
    setError('')
    try {
      await onRetryWrite()
    } catch (e) {
      setError(errText(lang, e))
    } finally {
      if (mounted.current) setSubmitting(false)
    }
  }
  function back() {
    setError('')
    if (stage === 'first') {
      setTourReady(false)
      setCheckpoint((old) => ({ ...old, stage: 'profile' }))
    } else setCheckpoint((old) => ({ ...old, stage: stage === 'logs' ? 'first' : stage === 'recall' ? 'logs' : 'recall' }))
  }
  const firstReady = !!current?.ready && current.hasAnswer && !current.busy && !current.needsRetry && !current.error
  const nextDisabled =
    submitting ||
    !!externalBusy ||
    (!!embedded && stage === 'finish' && (!current?.recalled || current.recall !== 'succeeded')) ||
    (stage === 'first'
      ? !firstReady
      : stage === 'logs'
        ? current?.write !== 'succeeded'
        : stage === 'recall'
          ? !firstReady || current?.recall !== 'succeeded' || seconds > 0 || (!!embedded && !current?.recalled)
          : false)
  const title =
    stage === 'first'
      ? t('chat.guide.first.title')
      : stage === 'logs'
        ? t('playground.tour.logs.title')
        : stage === 'recall'
          ? t('chat.guide.recall.title')
          : t('chat.guide.finish.title')
  if (!active) return null
  return (
    <>
      <Dialog.Root
        open={modal}
        onOpenChange={(open) => {
          if (!open) leave()
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className={`memos-welcome-overlay${stage === 'profile' ? ' profile' : ''}`} />
          <Dialog.Content
            className={`memos-welcome${stage === 'profile' ? ' memos-profile' : ''}`}
            onPointerDownOutside={(event) => event.preventDefault()}
            onCloseAutoFocus={(event) => {
              event.preventDefault()
              if (leaving.current) onReturn()
              else setTourReady(true)
            }}
            onOpenAutoFocus={(event) => {
              event.preventDefault()
              ;(stage === 'profile' ? nameInput.current : startButton.current)?.focus()
            }}
          >
            <Dialog.Close className="memos-welcome-close memos-icon-button" aria-label={t('common.close')}>
              <X size={19} />
            </Dialog.Close>
            <img className="memos-guide-robot" src="/playground/robot.png" width="80" height="80" alt="" />
            <Dialog.Title className="memos-welcome-title">
              {stage === 'welcome' ? t('playground.welcome.title') : t('playground.profile.title')}
            </Dialog.Title>
            <Dialog.Description className="memos-welcome-description">
              {stage === 'welcome' ? t('playground.welcome.body') : t('playground.profile.body')}
            </Dialog.Description>
            {stage === 'welcome' ? (
              <>
                <button
                  ref={startButton}
                  className="memos-welcome-start"
                  onClick={() => setCheckpoint((old) => ({ ...old, stage: 'profile' }))}
                >
                  {t('playground.welcome.start')}
                  <ArrowRight size={17} />
                </button>
                <button className="memos-welcome-skip" onClick={() => leave()}>
                  {t('playground.welcome.skip')}
                </button>
              </>
            ) : (
              <form
                className="memos-profile-form"
                onSubmit={(event) => {
                  event.preventDefault()
                  void introduce()
                }}
              >
                <label>
                  <span>{t('playground.profile.name')}</span>
                  <input
                    ref={nameInput}
                    value={name}
                    maxLength={60}
                    onChange={(event) => setName(event.target.value)}
                    placeholder={t('playground.profile.namePlaceholder')}
                    disabled={submitting}
                  />
                </label>
                <fieldset disabled={submitting}>
                  <legend>{t('chat.guide.cuisines')}</legend>
                  <div className="memos-cuisine-choices">
                    {cuisines.map((key) => (
                      <label key={key}>
                        <input
                          type="checkbox"
                          checked={selectedCuisines.includes(key)}
                          onChange={(event) =>
                            setSelectedCuisines((values) =>
                              event.target.checked ? [...values, key] : values.filter((value) => value !== key),
                            )
                          }
                        />
                        <span>{t(`chat.guide.cuisine.${key}`)}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
                <label>
                  <span>{t('chat.guide.region')}</span>
                  <input
                    value={region}
                    maxLength={100}
                    onChange={(event) => setRegion(event.target.value)}
                    placeholder={t('chat.guide.regionPlaceholder')}
                    disabled={submitting}
                  />
                </label>
                <label>
                  <span>{t('playground.profile.preferences')}</span>
                  <textarea
                    value={preferences}
                    maxLength={500}
                    rows={2}
                    onChange={(event) => setPreferences(event.target.value)}
                    placeholder={t('playground.profile.preferencesPlaceholder')}
                    disabled={submitting}
                  />
                </label>
                <fieldset disabled={submitting}>
                  <legend>{t('playground.profile.style')}</legend>
                  <div className="memos-profile-choices">
                    {(['playground.profile.concise', 'playground.profile.detailed', 'playground.profile.examples'] as const).map((key) => (
                      <button
                        key={key}
                        type="button"
                        aria-pressed={answerStyle === t(key)}
                        onClick={() => setAnswerStyle((value) => (value === t(key) ? '' : t(key)))}
                      >
                        {t(key)}
                      </button>
                    ))}
                  </div>
                </fieldset>
                {error && (
                  <p className="memos-guide-error" role="alert">
                    {error}
                  </p>
                )}
                <button
                  className="memos-welcome-start"
                  type="submit"
                  disabled={submitting || (!preferences.trim() && !selectedCuisines.length)}
                >
                  {submitting ? <LoaderCircle size={17} className="animate-spin" /> : <ArrowRight size={17} />}{' '}
                  {t('playground.profile.continue')}
                </button>
                <div className="memos-guide-form-actions">
                  <button
                    type="button"
                    disabled={submitting}
                    onClick={() => {
                      if (embedded && onBack) void onBack().catch((e: unknown) => setError(errText(lang, e)))
                      else setCheckpoint((old) => ({ ...old, stage: 'welcome' }))
                    }}
                  >
                    {t('playground.tour.previous')}
                  </button>
                  <button type="button" onClick={() => leave()}>
                    {t('playground.welcome.skip')}
                  </button>
                </div>
              </form>
            )}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
      {!modal && tourReady && (
        <GuideStepOverlay stage={stage} title={title} onSkip={leave}>
          <p>
            {stage === 'first'
              ? t('chat.guide.first.body')
              : stage === 'logs'
                ? t('playground.tour.logs.body')
                : stage === 'recall'
                  ? t('chat.guide.recall.body')
                  : current?.recalled
                    ? t('chat.guide.finish.body')
                    : t('chat.guide.noRecall')}
          </p>
          {stage === 'logs' && (
            <p className="memos-guide-result" role="status">
              {current?.write === 'succeeded'
                ? t('chat.guide.saved')(current.written)
                : current?.write === 'failed'
                  ? t('chat.guide.writeFailed')
                  : current?.write === 'unavailable'
                    ? t('chat.guide.writeUnavailable')
                    : current?.write === 'none' && !current?.busy
                      ? t('chat.guide.writeMissing')
                      : t('chat.guide.extracting')}
            </p>
          )}
          {stage === 'logs' && current?.ready && current.write === 'none' && !current.busy && onRetryWrite && (
            <button className="memos-guide-retry-write" disabled={submitting || externalBusy} onClick={() => void retryWrite()}>
              {t('chat.guide.retryWrite')}
            </button>
          )}
          {stage === 'recall' && (
            <p className="memos-guide-result" role="status">
              {current?.recall === 'succeeded'
                ? t('chat.guide.recalled')(current.recalled)
                : current?.recall === 'failed'
                  ? t('chat.guide.searchFailed')
                  : t('chat.guide.recalling')}
            </p>
          )}
          {(current?.error || current?.needsRetry) && (
            <div className="memos-guide-error" role="alert">
              <p>{current.error || t('chat.status.needsRetry')}</p>
              <button
                onClick={() => {
                  setError('')
                  void onRetry()?.catch((e: unknown) => setError(errText(lang, e)))
                }}
              >
                {t('common.retry')}
              </button>
            </div>
          )}
          {error && (
            <p role="alert" className="memos-guide-error">
              {error}
            </p>
          )}
          <footer className="memos-guide-step-footer">
            <small>{stage === 'first' ? '3' : stage === 'logs' ? '4' : stage === 'recall' ? '5' : '6'} / 6</small>
            <div>
              <button onClick={back} disabled={submitting}>
                {t('playground.tour.previous')}
              </button>
              <button
                disabled={nextDisabled}
                onClick={() => {
                  if (stage === 'first') setCheckpoint((old) => ({ ...old, stage: 'logs' }))
                  else if (stage === 'logs') void recall()
                  else if (stage === 'recall') setCheckpoint((old) => ({ ...old, stage: 'finish' }))
                  else void finish()
                }}
              >
                {submitting
                  ? t('common.loading')
                  : stage === 'finish'
                    ? t('chat.guide.finish.action')
                    : stage === 'recall' && seconds > 0
                      ? t('chat.guide.countdown')(seconds)
                      : nextDisabled
                        ? t('chat.guide.waiting')
                        : t('playground.tour.next')}
              </button>
            </div>
          </footer>
          <button className="memos-guide-exit" onClick={() => leave()}>
            {t('chat.guide.return')}
          </button>
        </GuideStepOverlay>
      )}
    </>
  )
}
