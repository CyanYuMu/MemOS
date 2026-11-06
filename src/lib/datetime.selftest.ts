/** 北京时间格式化的自检。`node src/lib/datetime.selftest.ts`，`pnpm verify` 也会跑。
 *
 *  时区换算错了不会报错，只会把「昨天 19:53 UTC」显示成「昨天 19:53」——看起来很像对的。
 *  所以每条都带阴性对照（直接用字符串截断会得到什么）。
 */

import { formatBeijingTime } from './datetime.ts'

let passed = 0
const failures: string[] = []
function check(name: string, pass: boolean, detail = '') {
  if (pass) {
    passed++
    console.log(`  ok    ${name}${detail ? ` — ${detail}` : ''}`)
  } else {
    failures.push(name)
    console.log(` FAIL   ${name}${detail ? ` — ${detail}` : ''}`)
  }
}

{
  const iso = '2026-10-28T19:53:35.075376+00:00'
  check('UTC 带微秒与偏移 → 北京时间', formatBeijingTime(iso) === '2026-10-29 03:53', formatBeijingTime(iso))
  check('Z 结尾同样按 UTC 换算', formatBeijingTime('2026-10-28T19:53:35Z') === '2026-10-29 03:53')

  /** 阴性对照：直接截断前 16 位，会把 UTC 时间当成北京时间显示 */
  const naive = (s: string) => s.slice(0, 16).replace('T', ' ')
  check('阴性对照：直接截断会得到 UTC 时间', naive(iso) === '2026-10-28 19:53', naive(iso))
  check('正解与截断结果不同', formatBeijingTime(iso) !== naive(iso))

  check('无时区字符串原样截到分钟', formatBeijingTime('2026-10-22 15:40:00') === '2026-10-22 15:40')
  check('无时区的 ISO 也不二次平移', formatBeijingTime('2026-10-22T15:40:00.123') === '2026-10-22 15:40')
  check('空值返回空串', formatBeijingTime('') === '' && formatBeijingTime(null) === '')
  check('无法解析时原样返回', formatBeijingTime('not-a-time') === 'not-a-time')
  check(
    '午夜显示 00 而不是 24',
    formatBeijingTime('2026-10-28T16:00:00Z') === '2026-10-29 00:00',
    formatBeijingTime('2026-10-28T16:00:00Z'),
  )
}

console.log(`\n${passed}/${passed + failures.length} 通过`)
if (failures.length) throw new Error(`时间格式自检失败 ${failures.length} 项`)
