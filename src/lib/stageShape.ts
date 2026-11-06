/** driver.js 的高亮框只有一个圆角值。取元素自身（或最近一层有圆角的祖先）的圆角，再加上留白，
 *  高亮框的圆角就和页面上被框住的卡片同心，不会出现两层不一致的圆角。
 *  容器本身没有圆角、但里面是圆角卡片时，在元素上写 `data-stage-radius` 指定。 */
export function stageShape(element: Element, padding: number) {
  const radiusOf = (node: Element) => parseFloat(getComputedStyle(node).borderTopLeftRadius) || 0
  let radius = Number((element as HTMLElement).dataset?.stageRadius) || radiusOf(element)
  for (let node = element.parentElement, depth = 0; !radius && node && depth < 4; node = node.parentElement, depth++) {
    radius = radiusOf(node)
  }
  return { stagePadding: padding, stageRadius: Math.min(28, Math.max(8, radius + padding)) }
}
