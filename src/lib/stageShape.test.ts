// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { stageShape } from './stageShape'

function build(html: string) {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.append(host)
  return host
}
afterEach(() => {
  document.body.innerHTML = ''
})

it('高亮框圆角 = 元素自身圆角 + 留白，和页面上的卡片同心', () => {
  const host = build('<button style="border-top-left-radius: 8px">新建</button>')
  expect(stageShape(host.querySelector('button')!, 8)).toEqual({ stagePadding: 8, stageRadius: 16 })
})

it('元素自己没有圆角时取最近一层有圆角的祖先，不看子元素', () => {
  const host = build(
    '<div style="border-top-left-radius: 22px"><section><div id="thread"><p style="border-top-left-radius: 4px">x</p></div></section></div>',
  )
  expect(stageShape(host.querySelector('#thread')!, 0)).toEqual({ stagePadding: 0, stageRadius: 22 })
})

it('容器没有圆角但里面是圆角卡片时，用 data-stage-radius 指定；结果限制在 8 到 28 之间', () => {
  const host = build('<div id="a" data-stage-radius="12"></div><div id="b"></div><div id="c" data-stage-radius="40"></div>')
  expect(stageShape(host.querySelector('#a')!, 8).stageRadius).toBe(20)
  expect(stageShape(host.querySelector('#b')!, 0).stageRadius).toBe(8)
  expect(stageShape(host.querySelector('#c')!, 8).stageRadius).toBe(28)
})
