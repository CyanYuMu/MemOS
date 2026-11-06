// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { LangProvider, ToastProvider } from '../../lib/store'
import { api, type Memory } from '../../lib/api'
import { Markdown } from './Markdown'
import { ThinkingBlock } from './ThinkingBlock'
import { entryFromMemory } from './memory'
import { memoryCitationText, resolveMemoryCitation } from './memoryCitations'
import type { TurnSnapshot } from './turnSnapshot'

const memory: Memory = {
  memoryId: '12345678-abcd',
  memoryKey: '口味',
  memoryContent: '喜欢川菜',
  subjectType: 'user',
  externalUserId: 'u',
  createTime: '2026-11-01 10:00:00',
  updateTime: '2026-11-01 10:00:00',
  memoryType: 'UserMemory',
  status: 'activated',
  tags: ['川菜'],
  version: 1,
}
const snapshot: TurnSnapshot = {
  status: 'available',
  partial: false,
  entries: [{ entry: entryFromMemory(memory), record: memory, query: '口味', origins: [{ kind: 'search', callId: 'actual-call' }] }],
}
function page(thinking = false) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <LangProvider>
        <ToastProvider>
          {thinking ? (
            <ThinkingBlock
              item={{ kind: 'thinking', id: 't', text: '结合偏好 [memory:actual-call/12345678-abcd]。', streaming: false }}
              memory={{ projectId: 'p', snapshot }}
            />
          ) : (
            <Markdown
              text={
                '推荐川菜 [memory:actual-call/12345678-abcd]。无效 [memory:foreign/12345678-abcd]。代码 ` [memory:actual-call/12345678-abcd] `'
              }
              memory={{ projectId: 'p', snapshot }}
            />
          )}
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  )
}
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
it('思考与回答均显示绑定本轮实体的圆形编号，点击打开历史七字段', async () => {
  const detail = vi.spyOn(api.memory, 'detail')
  page(true)
  fireEvent.click(screen.getByRole('button', { name: '查看记忆 1: 口味' }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText('喜欢川菜')).toBeTruthy()
  for (const f of ['ID', 'Key', 'value', 'memory_type', 'status', 'tags', 'updated_at'])
    expect(within(dialog).getByRole('rowheader', { name: f })).toBeTruthy()
  expect(detail).not.toHaveBeenCalled()
})
it('无效调用不生成引用按钮，代码中的标记保持字面文本', () => {
  page()
  expect(screen.getAllByRole('button', { name: /查看记忆/ })).toHaveLength(1)
  expect(document.querySelector('code')?.textContent).toContain('[memory:actual-call/12345678-abcd]')
})
it('跨版本ID或前缀歧义不猜关联，调用ID可明确绑定当时版本', () => {
  const two = {
    ...snapshot,
    entries: [
      ...snapshot.entries,
      {
        ...snapshot.entries[0],
        entry: { ...snapshot.entries[0].entry, value: '不吃辣' },
        origins: [{ kind: 'profile' as const, callId: 'later' }],
      },
    ],
  }
  expect(resolveMemoryCitation('[memory:12345678-abcd]', two)).toBeNull()
  expect(resolveMemoryCitation('[1:12345678]', two)).toBeNull()
  expect(resolveMemoryCitation('[memory:actual-call/12345678-abcd]', two)?.index).toBe(0)
  expect(resolveMemoryCitation('[4:12345678]', snapshot)?.index).toBe(0)
  expect(resolveMemoryCitation('[memory:actual-call/foreign-id]', snapshot)).toBeNull()
})

it('同ID正文的元数据变化仍按引用调用保留原版本，不借画像补齐搜索旧摘要', () => {
  const original = { ...memory, version: 1, tags: ['原标签'] }
  const changed = { ...memory, version: 2, tags: ['新标签'] }
  const row = {
    entry: entryFromMemory(changed),
    record: changed,
    query: '',
    origins: [
      { kind: 'search' as const, callId: 'old', entry: entryFromMemory(original), record: original },
      { kind: 'profile' as const, callId: 'new', entry: entryFromMemory(changed), record: changed },
      { kind: 'search' as const, callId: 'summary-only', entry: entryFromMemory(original) },
    ],
  }
  const snap = { ...snapshot, entries: [row] }
  expect(resolveMemoryCitation('[memory:old/12345678-abcd]', snap)?.row.record?.version).toBe(1)
  expect(resolveMemoryCitation('[memory:new/12345678-abcd]', snap)?.row.record?.version).toBe(2)
  expect(resolveMemoryCitation('[memory:summary-only/12345678-abcd]', snap)?.row.record).toBeUndefined()
})

it('复制和反馈文字显示编号，代码中的协议示例保持字面值', () => {
  const text = '偏好 [memory:actual-call/12345678-abcd]，代码 `[memory:actual-call/12345678-abcd]`'
  expect(memoryCitationText(text, snapshot)).toBe('偏好 [1]，代码 `[memory:actual-call/12345678-abcd]`')
})
