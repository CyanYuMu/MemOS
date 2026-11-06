/** `diff.ts` 的自检。用 `node src/lib/diff.selftest.ts` 跑，`pnpm verify` 也会跑它。
 *
 *  为什么要单独一个脚本：diff 的错误不会让类型检查或页面报错，只会让「改了一个词」
 *  在界面上显示成「整段重写」，或者明明一样的两段被判成全改。所以每条断言都要能失败，
 *  并且带阴性对照——故意用两种坏实现跑同一批用例，确认它们真的会挂。
 */

import { diffLines, groupBlocks, inlineSegments, splitLines, diffStats, type DiffRow, type DiffSegment } from './diff.ts'

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

const kinds = (rows: DiffRow[]) => rows.map((r) => r.kind).join(',')
const joinSegs = (segs: DiffSegment[]) => segs.map((s) => s.text).join('')

/* ---------------------------------------------------------------- 基本形状 */

{
  const empty = diffLines('', '')
  check('空文本 × 空文本没有差异行', empty.length === 0, kinds(empty))

  const added = diffLines('', '第一行\n第二行')
  check('空文本 → 两行全是新增', kinds(added) === 'add,add' && added[1].bLine === 2, kinds(added))

  const removed = diffLines('第一行\n第二行', '')
  check('两行 → 空文本全是删除', kinds(removed) === 'del,del' && removed[0].aLine === 1, kinds(removed))
}

/* ------------------------------------------------------- 整段新增 / 删除 */

{
  const a = '一\n二\n三'
  const b = '零\n一\n二\n三'
  const rows = diffLines(a, b)
  // 首行插入不能被错认成「每一行都改了」
  check('首行插入只标一行新增', kinds(rows) === 'add,equal,equal,equal', kinds(rows))
  check('首行插入后旧行号不错位', rows[1].aLine === 1 && rows[1].bLine === 2, `${rows[1].aLine}/${rows[1].bLine}`)

  const removedTail = diffLines('一\n二\n三', '一\n二')
  check('删掉末行只标一行删除', kinds(removedTail) === 'equal,equal,del', kinds(removedTail))
}

/* ------------------------------------------------------------ 行内修改 */

{
  const a = '用户偏好：结论先行，给出可量化的数字。'
  const b = '用户偏好：结论先行，给出可复现的步骤。'
  const rows = diffLines(a, b)
  check('行内修改是一删一增', kinds(rows) === 'del,add', kinds(rows))
  const del = rows[0]
  const add = rows[1]
  check('改动行带字符级片段', Boolean(del.segments && add.segments))
  check(
    '行内只高亮真正变化的那几个字',
    (del.segments ?? []).some((s) => s.kind === 'del' && s.text === '数字') &&
      (add.segments ?? []).some((s) => s.kind === 'add' && s.text === '步骤'),
    JSON.stringify(del.segments?.map((s) => `${s.kind}:${s.text}`)),
  )
  check('两侧片段拼回去等于原文', joinSegs(del.segments ?? []) === a && joinSegs(add.segments ?? []) === b)
}

/* ------------------------------------------------------------ 中英混排 */

{
  const a = 'Respond in Chinese. 每条记忆带 tags。\nKeep quotes "as is".'
  const b = 'Respond in Chinese. 每条记忆带 tags 与 confidence。\nKeep quotes "as is".'
  const rows = diffLines(a, b)
  check('中英混排只改第一行', kinds(rows) === 'del,add,equal', kinds(rows))
  check('混排行内片段拼回原文', joinSegs(rows[0].segments ?? []) === a.split('\n')[0])
  check('未改动的第二行保持 equal', rows[2].text === 'Keep quotes "as is".', rows[2].text)
}

/* -------------------------------------------------------------- 很长的一行 */

{
  const a = 'x'.repeat(20000) + '甲'
  const b = 'x'.repeat(20000) + '乙'
  const started = Date.now()
  const rows = diffLines(a, b)
  const cost = Date.now() - started
  check('两万字的单行改动不阻塞（< 1000ms）', cost < 1000, `${cost}ms`)
  check('超长行仍是一删一增', kinds(rows) === 'del,add', kinds(rows))
  check('超长行退化成整行高亮但不丢字', joinSegs(rows[0].segments ?? []) === a)
  check('超长行不抛异常且行号正确', rows[0].aLine === 1 && rows[1].bLine === 1)
}

/* ------------------------------------------------------------------- CRLF */

{
  const rows = diffLines('一\r\n二\r\n三', '一\n二\n三')
  check('CRLF 与 LF 视为没有差异', kinds(rows) === 'equal,equal,equal', kinds(rows))
  check('splitLines 统一换行符', splitLines('a\r\nb\rc').join('|') === 'a|b|c', splitLines('a\r\nb\rc').join('|'))
}

/* ---------------------------------------------------------------- 折叠分组 */

{
  const text = Array.from({ length: 100 }, (_, i) => `第 ${i + 1} 行`).join('\n')
  const blocks = groupBlocks(diffLines(text, text), 3)
  check(
    '100 行未改动被折成「3 + 折叠 + 3」',
    blocks.length === 3 && blocks[0].kind === 'equal' && blocks[1].kind === 'fold' && blocks[2].kind === 'equal',
    blocks.map((b) => b.kind).join(','),
  )
  const fold = blocks[1]
  check('折叠块带被藏起来的行数与行内容', fold.kind === 'fold' && fold.count === 94 && fold.rows.length === 94)
  check('短段不折叠', groupBlocks(diffLines('a\nb', 'a\nb'), 3).length === 1)
}

/* ------------------------------------------------------------- 统计与排序 */

{
  const rows = diffLines('a\nb\nc', 'a\nB\nc\nd')
  const stats = diffStats(rows)
  check('统计新增 2 行、删除 1 行', stats.added === 2 && stats.removed === 1, JSON.stringify(stats))
  check(
    '每一行恰好属于一侧或两侧',
    rows.every((r) => (r.kind === 'equal' ? r.aLine !== null && r.bLine !== null : true)),
  )
}

/* ------------------------------------------------- 阴性对照：坏实现必须挂 */

/** 坏实现 1：按行号对齐比较（不找公共子序列）。首行插入时会把后面每一行都判成改动。 */
function naiveByIndex(a: string, b: string) {
  const al = splitLines(a)
  const bl = splitLines(b)
  const n = Math.max(al.length, bl.length)
  let changed = 0
  for (let i = 0; i < n; i++) if (al[i] !== bl[i]) changed++
  return changed
}

/** 坏实现 2：行数相同就认为整段没改。行内修改会被它整段吞掉。 */
function naiveSameLength(a: string, b: string) {
  return splitLines(a).length === splitLines(b).length && a.length === b.length
}

{
  const a = '一\n二\n三'
  const b = '零\n一\n二\n三'
  check('阴性对照：按行号对齐的实现会把首行插入误判成 4 行改动', naiveByIndex(a, b) === 4, `坏实现改了 ${naiveByIndex(a, b)} 行`)

  const inlineA = '用户偏好：结论先行'
  const inlineB = '用户偏好：数字先行'
  check('阴性对照：行数相同的实现会把行内修改判成没变', naiveSameLength(inlineA, inlineB), '坏实现返回 true')
  check(
    '正解把行内修改标出来',
    diffLines(inlineA, inlineB).some((r) => r.kind === 'del' || r.kind === 'add'),
  )
}

/* --------------------------------------------- 阴性对照：行内片段不能丢字 */

{
  const a = 'abc'
  const b = 'abd'
  const seg = inlineSegments(a, b)
  check('行内分段不丢字（正解）', joinSegs(seg.a) === a && joinSegs(seg.b) === b)

  /** 坏实现：把不等的前缀整段丢掉，只吐后半截 */
  const broken = (x: string, y: string) => ({ a: [{ kind: 'del', text: x.slice(1) }], b: [{ kind: 'add', text: y.slice(1) }] })
  const bad = broken(a, b)
  check('阴性对照：丢掉前缀的实现拼不回原文', joinSegs(bad.a as DiffSegment[]) !== a, `坏实现得到「${joinSegs(bad.a as DiffSegment[])}」`)
}

/* ------------------------------------------------------------------ 汇总 */

console.log(`\n${passed}/${passed + failures.length} 通过`)
if (failures.length) {
  console.log('失败项：' + failures.join('、'))
  throw new Error(`diff 自检失败 ${failures.length} 项`)
}
