import { expect, it } from 'vitest'
import { parseAssistantResponse } from './assistantResponse'
it('参考问题取自各轮LLM结果，不用固定列表补齐', () => {
  const a = parseAssistantResponse('菜谱<graph-memory-followups>["豆腐怎么焯水？","能少放辣椒吗？"]</graph-memory-followups>')
  const b = parseAssistantResponse('行程<graph-memory-followups>["行程能改到周六吗？"]</graph-memory-followups>')
  expect(a.questions).toEqual(['豆腐怎么焯水？', '能少放辣椒吗？'])
  expect(b.questions).toEqual(['行程能改到周六吗？'])
  expect(a.text).toBe('菜谱')
  expect(parseAssistantResponse('没有后续问题').questions).toEqual([])
  expect(parseAssistantResponse('正文<graph-memory-followups>["未完成').questions).toEqual([])
})
it('标题与动态问题顺序无关，标题元数据和流式半截都不显示', () => {
  for (const tags of [
    '<graph-memory-title>"午餐"</graph-memory-title><graph-memory-followups>["怎么备菜？"]</graph-memory-followups>',
    '<graph-memory-followups>["怎么备菜？"]</graph-memory-followups><graph-memory-title>"午餐"</graph-memory-title>',
  ]) {
    const result = parseAssistantResponse('正文' + tags)
    expect(result.text).toBe('正文')
    expect(result.questions).toEqual(['怎么备菜？'])
  }
  expect(parseAssistantResponse('正文<graph-memory-ti').text).toBe('正文')
})

it('代码块与行内代码中的元数据示例原样保留', () => {
  const text =
    '示例\n```xml\n<graph-memory-title>"例子"</graph-memory-title>\n```\n`<graph-memory-followups>["例子"]</graph-memory-followups>`'
  expect(parseAssistantResponse(text).text).toBe(text)
})
