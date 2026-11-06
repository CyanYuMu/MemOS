import { expect, it } from 'vitest'
import { NAV_GROUPS } from './nav'

const items = NAV_GROUPS.flatMap((group) => group.items)

it('本地记忆暂不出现在侧栏，其他入口照常显示', () => {
  const hidden = items.filter((item) => item.hidden).map((item) => item.label)
  expect(hidden).toEqual(['nav.localMemories'])
})

it('提示词工作台使用 /cn/prompts', () => {
  expect(items.find((item) => item.label === 'nav.prompts')?.to).toBe('/cn/prompts')
  expect(items.some((item) => item.to === '/cn/customExtraction')).toBe(false)
})
