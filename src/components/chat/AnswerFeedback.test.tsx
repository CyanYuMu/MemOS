// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { webcrypto } from 'node:crypto'
import { LangProvider, ToastProvider } from '../../lib/store'
import { api } from '../../lib/api'
import { AnswerFeedback } from './AnswerFeedback'
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
it('只有点赞点踩入口，点踩后才展示意见输入', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.spyOn(api.chat, 'feedback').mockResolvedValue({ vote: 0 })
  const rate = vi.spyOn(api.chat, 'rate').mockResolvedValue({ vote: -1 })
  render(
    <LangProvider>
      <ToastProvider>
        <AnswerFeedback projectId="p" identity="answer-1" text="推荐川菜" onCorrect={vi.fn().mockResolvedValue(true)} />
      </ToastProvider>
    </LangProvider>,
  )
  expect(screen.queryByRole('button', { name: '纠正回答' })).toBeNull()
  await waitFor(() => expect(screen.getByRole('button', { name: '点踩' }).hasAttribute('disabled')).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: '点踩' }))
  expect(await screen.findByRole('dialog')).toBeTruthy()
  expect(screen.getByRole('textbox', { name: '这条回答哪里需要改进？' })).toBeTruthy()
  expect(rate).toHaveBeenCalledWith('p', expect.any(String), -1)
})
it('意见先持久化再发送纠正，发送失败仍显示已保存', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.spyOn(api.chat, 'feedback').mockResolvedValue({ vote: 0, comment: '原意见' })
  const order: string[] = []
  const rate = vi.spyOn(api.chat, 'rate').mockImplementation(async (_p, _r, _v, details) => {
    if (details) order.push('saved')
    return { vote: -1 }
  })
  const onCorrect = vi.fn(async () => {
    order.push('send')
    return false
  })
  render(
    <LangProvider>
      <ToastProvider>
        <AnswerFeedback projectId="p" conversationId="c" identity="a" text="回答" onCorrect={onCorrect} />
      </ToastProvider>
    </LangProvider>,
  )
  await waitFor(() => expect(screen.getByRole('button', { name: '点踩' }).hasAttribute('disabled')).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: '点踩' }))
  const input = await screen.findByRole('textbox', { name: '这条回答哪里需要改进？' })
  expect((input as HTMLTextAreaElement).value).toBe('原意见')
  fireEvent.change(input, { target: { value: '请缩短回答' } })
  fireEvent.click(screen.getByRole('button', { name: '保存并发送纠正' }))
  await waitFor(() => expect(order).toEqual(['saved', 'send']))
  expect(rate).toHaveBeenCalledWith('p', expect.any(String), -1, { comment: '请缩短回答', conversationId: 'c', answerExcerpt: '回答' })
  expect(screen.getByRole('status').textContent).toBe('意见已保存。')
})
