/** 词条占位符的插值。
 *
 *  单独一个文件（而不是留在 `i18n.ts` 里）是为了**能被 Node 直接 import 来测**：
 *  `i18n.ts` 要 import 两个 JSON，Node 的 type-stripping 处理不了 JSON 的
 *  import attributes，于是整个模块在 Node 里加载不起来，插值逻辑也就测不了。
 *  这个文件不依赖任何 JSON，`node --experimental-strip-types` 直接跑。
 */

/** ⚠️ 两个正则分开：`test` 用不带 `g` 的（带 `g` 的 `test` 会推进 `lastIndex`，
 *  连着测两条词条时第二条可能漏判），`replace` 用带 `g` 的。
 *
 *  ⚠️ **必须有捕获组**。`/\{\w+\}/g` 这种写法看着一样，但替换回调的第二个参数
 *  会变成「匹配位置的下标」而不是占位符名字，于是 `Number(name)` 拿到一个下标、
 *  `args[下标]` 是 undefined，最后**原样把 `{0}` 吐回页面上**。不报错、不抛异常，
 *  页面上就是一排 `第 {0} / {1} 页`（2026-10-19 线上实测，分页器的 aria-label 就是这样）。 */
const HAS_PLACEHOLDER = /\{(\w+)\}/
const PLACEHOLDER = /\{(\w+)\}/g

/** 插值。
 *
 *  占位符名有两种：`{0}` / `{1}` 按位置取参数；`{total}` 这种按名字取，
 *  这时第一个参数是一个对象（`graph.hud` 就是这种，它接的是一个状态快照
 *  而不是一串位置参数）。同一模板里两种不能混用。
 *
 *  ⚠️ 取不到值的占位符**原样留在文案里**（而不是换成空串）：空串会让「少传了一个参数」
 *  看起来像「这里本来就没内容」，留在页面上是 `{1}` 这种一眼能认出来的东西。 */
export function interpolate(tpl: string, args: unknown[]): string {
  return tpl.replace(PLACEHOLDER, (whole, name: string) => {
    let v: unknown
    if (/^\d+$/.test(name)) {
      v = args[Number(name)]
    } else {
      const o = args[0]
      v = o && typeof o === 'object' ? (o as Record<string, unknown>)[name] : undefined
    }
    return v === undefined || v === null ? whole : String(v)
  })
}

/** 词条里有没有占位符。决定 `t(key)` 返回字符串还是函数。 */
export const hasPlaceholder = (tpl: string): boolean => HAS_PLACEHOLDER.test(tpl)
