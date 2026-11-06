import { useEffect, useState } from 'react'
import { ThumbsDown, ThumbsUp } from 'lucide-react'
import { api } from '../../lib/api'
import { useLang, useT } from '../../lib/store'
import { errText } from '../../lib/i18n'
import { answerKey } from './assistantResponse'
import { Dialog } from '../ui'
import { Button } from '../ui/button'

export function AnswerFeedback({
  projectId,
  conversationId = '',
  identity,
  text,
  displayText = text,
  onCorrect,
}: {
  projectId: string
  conversationId?: string
  identity: string
  text: string
  displayText?: string
  onCorrect: (text: string) => Promise<boolean>
}) {
  const t = useT()
  const { lang } = useLang()
  const [key, setKey] = useState('')
  const [vote, setVote] = useState(0)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [correcting, setCorrecting] = useState(false)
  const [correction, setCorrection] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [saved, setSaved] = useState(false)
  useEffect(() => {
    let active = true
    answerKey(text, identity)
      .then(async (id) => {
        if (active) {
          setBusy(true)
          setKey(id)
        }
        const result = await api.chat.feedback(projectId, id)
        if (active) {
          setVote(result.vote)
          setCorrection(result.comment || '')
          setSaved(false)
          setError('')
        }
      })
      .catch((e) => {
        if (active) setError(errText(lang, e))
      })
      .finally(() => {
        if (active) setBusy(false)
      })
    return () => {
      active = false
    }
  }, [projectId, identity, text, lang])
  async function rate(value: number) {
    if (!key || busy) return
    setBusy(true)
    setError('')
    try {
      const result = await api.chat.rate(projectId, key, vote === value ? 0 : value)
      setVote(result.vote)
      if (result.vote === -1) setCorrecting(true)
    } catch (e) {
      setError(errText(lang, e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <div className="memos-answer-feedback" role="group" aria-label={t('chat.feedback.label')}>
        <button
          type="button"
          aria-label={t('chat.feedback.like')}
          aria-pressed={vote === 1}
          disabled={!key || busy}
          onClick={() => void rate(1)}
        >
          <ThumbsUp size={18} />
        </button>
        <button
          type="button"
          aria-label={t('chat.feedback.dislike')}
          aria-pressed={vote === -1}
          disabled={!key || busy}
          onClick={() => void rate(-1)}
        >
          <ThumbsDown size={18} />
        </button>
      </div>
      <Dialog
        open={correcting}
        title={t('chat.feedback.correctionTitle')}
        onClose={() => {
          if (!submitting) setCorrecting(false)
        }}
      >
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (!correction.trim() || submitting) return
            setSubmitting(true)
            setError('')
            void api.chat
              .rate(projectId, key, -1, { comment: correction.trim(), conversationId, answerExcerpt: displayText.slice(0, 2000) })
              .then(async () => {
                setSaved(true)
                const accepted = await onCorrect(t('chat.feedback.correctionMessage')(displayText.slice(0, 2000), correction.trim()))
                if (accepted) setCorrecting(false)
              })
              .catch((e) => setError(errText(lang, e)))
              .finally(() => setSubmitting(false))
          }}
        >
          <textarea
            className="memos-feedback-input"
            aria-label={t('chat.feedback.correctionTitle')}
            placeholder={t('chat.feedback.correctionPlaceholder')}
            value={correction}
            onChange={(event) => {
              setCorrection(event.target.value)
              setSaved(false)
            }}
            maxLength={2000}
            rows={5}
            disabled={submitting}
          />
          {saved && <p role="status">{t('chat.feedback.saved')}</p>}
          <Button type="submit" disabled={!correction.trim() || submitting}>
            {t('chat.feedback.correctionSend')}
          </Button>
        </form>
      </Dialog>
      {error && (
        <span className="memos-feedback-error" role="alert">
          {error}
        </span>
      )}
    </>
  )
}
