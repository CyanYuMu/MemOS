/** 文本 diff：按行的最长公共子序列 + 行内字符级高亮。
 *
 *  不引入 diff 依赖（包体已经有 500kB 告警，`make web` 又用冻结的 lockfile），
 *  所以这里是唯一的一份实现，界面层只消费下面四种导出：
 *
 *    splitLines    按行切分，统一换行符
 *    diffLines     两段文本 → 有序的行级差异
 *    inlineSegments 一对被改动的行 → 各自的字符级片段
 *    groupBlocks   把行级差异分组，长段「未改动」折叠成一块
 *
 *  ⚠️ 比较前一律把 CRLF / CR 归一成 LF：同一段提示词在 Windows 上编辑过就会带上
 *  `\r`，不归一的话整篇都会被判成「每一行都改了」，行内高亮更是一屏红绿。
 */

export type DiffKind = 'equal' | 'add' | 'del'

export interface DiffSegment {
  kind: DiffKind
  text: string
}

export interface DiffRow {
  kind: DiffKind
  /** 左侧（旧）行号，1 起；这一侧没有该行时为 null */
  aLine: number | null
  /** 右侧（新）行号 */
  bLine: number | null
  text: string
  /** 行内高亮。只有成对出现的「删 + 增」才有，其余行为 undefined */
  segments?: DiffSegment[]
}

export type DiffBlock = { kind: 'equal' | 'change'; rows: DiffRow[] } | { kind: 'fold'; rows: DiffRow[]; count: number }

/** 行内比较的字符乘积上限。超过就退化成整行高亮：
 *  20000 字的正文如果只有一行，逐字符 LCS 是 4 亿格，页面会直接卡死。 */
const MAX_INLINE_CELLS = 250_000
/** 行级 LCS 的格子数上限，作用同上（正常提示词远到不了） */
const MAX_LINE_CELLS = 4_000_000

export function splitLines(text: string): string[] {
  if (!text) return []
  return text.replace(/\r\n?/g, '\n').split('\n')
}

/** 字符数组的 LCS 长度表。从右下往左上填，回溯时按「相等就前进、否则走格子大的一边」取。 */
function lcsTable(a: readonly string[], b: readonly string[]): number[][] {
  const n = a.length
  const m = b.length
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  return dp
}

function pushSegment(out: DiffSegment[], kind: DiffKind, text: string) {
  if (!text) return
  const last = out[out.length - 1]
  if (last && last.kind === kind) last.text += text
  else out.push({ kind, text })
}

/** 一对行做字符级比较。返回两侧各自的片段，拼接起来等于原文。 */
export function inlineSegments(a: string, b: string): { a: DiffSegment[]; b: DiffSegment[] } {
  if (a === b) return { a: [{ kind: 'equal', text: a }], b: [{ kind: 'equal', text: b }] }

  const left: DiffSegment[] = []
  const right: DiffSegment[] = []
  const ac = Array.from(a)
  const bc = Array.from(b)

  // 超长行不逐字比较：退化成整行删除 + 整行新增，样式上仍然是「这一行被换掉了」
  if (ac.length * bc.length > MAX_INLINE_CELLS) {
    pushSegment(left, 'del', a)
    pushSegment(right, 'add', b)
    return { a: left, b: right }
  }

  const dp = lcsTable(ac, bc)
  let i = 0
  let j = 0
  while (i < ac.length && j < bc.length) {
    if (ac[i] === bc[j]) {
      pushSegment(left, 'equal', ac[i])
      pushSegment(right, 'equal', bc[j])
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      pushSegment(left, 'del', ac[i])
      i++
    } else {
      pushSegment(right, 'add', bc[j])
      j++
    }
  }
  for (; i < ac.length; i++) pushSegment(left, 'del', ac[i])
  for (; j < bc.length; j++) pushSegment(right, 'add', bc[j])
  return { a: left, b: right }
}

export function diffLines(aText: string, bText: string): DiffRow[] {
  const a = splitLines(aText)
  const b = splitLines(bText)
  const rows: DiffRow[] = []

  if (a.length * b.length > MAX_LINE_CELLS) {
    a.forEach((text, i) => rows.push({ kind: 'del', aLine: i + 1, bLine: null, text }))
    b.forEach((text, i) => rows.push({ kind: 'add', aLine: null, bLine: i + 1, text }))
    return rows
  }

  const dp = lcsTable(a, b)
  let i = 0
  let j = 0
  /** 一段改动里还没配对的删除行与新增行；遇到未改动行时按出现顺序逐对补行内高亮 */
  const pendingDel: DiffRow[] = []
  const pendingAdd: DiffRow[] = []

  const flushInline = () => {
    const pairs = Math.min(pendingDel.length, pendingAdd.length)
    for (let k = 0; k < pairs; k++) {
      const seg = inlineSegments(pendingDel[k].text, pendingAdd[k].text)
      pendingDel[k].segments = seg.a
      pendingAdd[k].segments = seg.b
    }
    pendingDel.length = 0
    pendingAdd.length = 0
  }

  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      flushInline()
      rows.push({ kind: 'equal', aLine: i + 1, bLine: j + 1, text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      const row: DiffRow = { kind: 'del', aLine: i + 1, bLine: null, text: a[i] }
      rows.push(row)
      pendingDel.push(row)
      i++
    } else {
      const row: DiffRow = { kind: 'add', aLine: null, bLine: j + 1, text: b[j] }
      rows.push(row)
      pendingAdd.push(row)
      j++
    }
  }

  for (; i < a.length; i++) {
    const row: DiffRow = { kind: 'del', aLine: i + 1, bLine: null, text: a[i] }
    rows.push(row)
    pendingDel.push(row)
  }
  for (; j < b.length; j++) {
    const row: DiffRow = { kind: 'add', aLine: null, bLine: j + 1, text: b[j] }
    rows.push(row)
    pendingAdd.push(row)
  }
  flushInline()

  return rows
}

/** 把行级差异切成「未改动（可折叠）」与「改动」两类块。
 *
 *  长度超过 `context * 2` 的未改动段，前 `context` 行与后 `context` 行留在页面上，
 *  中间单独成一块 `fold`（`rows` 是被藏起来的原行，`count` 等于它的长度）。
 *  折叠按钮只需要把 `fold` 块替换成展开即可，不用重算 diff。 */
export function groupBlocks(rows: DiffRow[], context = 3): DiffBlock[] {
  const blocks: DiffBlock[] = []
  let idx = 0
  while (idx < rows.length) {
    const kind = rows[idx].kind === 'equal' ? 'equal' : 'change'
    let end = idx
    while (end < rows.length && (rows[end].kind === 'equal' ? 'equal' : 'change') === kind) end++
    const run = rows.slice(idx, end)
    if (kind === 'equal' && run.length > context * 2) {
      blocks.push({ kind: 'equal', rows: run.slice(0, context) })
      blocks.push({ kind: 'fold', rows: run.slice(context, run.length - context), count: run.length - context * 2 })
      blocks.push({ kind: 'equal', rows: run.slice(run.length - context) })
    } else {
      blocks.push({ kind: kind as 'equal' | 'change', rows: run })
    }
    idx = end
  }
  return blocks
}

/** 改动的行数统计，给标题旁的「+n −n」用 */
export function diffStats(rows: DiffRow[]): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const r of rows) {
    if (r.kind === 'add') added++
    else if (r.kind === 'del') removed++
  }
  return { added, removed }
}
