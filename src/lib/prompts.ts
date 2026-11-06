/** 提示词工作台的纯逻辑：输入类型与形状归一、保存闸门、连点窗口、结构化结果取列。
 *
 *  放在这里是为了能脱离浏览器跑自检（见 `prompts.selftest.ts`）。页面里只做渲染和请求。
 *
 *  ⚠️ 真实注册表的输入形状以 `prompts/detail` 的 `inputSchema` 为准，例如 `memories` 是
 *  `{"memories":[{"id","memory"}]}` 而不是字符串数组。这里的归一函数必须容错：
 *  形状不认识就退回 JSON 编辑器，绝不能让页面在渲染或取值时抛异常。
 */

import type { PromptListItem } from './api'

export type EditorKind = 'conversation' | 'text' | 'qa_pair' | 'memories' | 'json'

export type MessageRow = { role: string; content: string }
export type MemoryRow = { id: string; memory: string }

export interface InputState {
  mode: EditorKind
  messages: MessageRow[]
  text: string
  qaPair: string
  memories: MemoryRow[]
  jsonText: string
}

/** `input_kind` → 前端真正渲染的编辑器。不认识的一律退化成 JSON 编辑器 */
export function editorKind(inputKind: string): EditorKind {
  if (inputKind === 'conversation') return 'conversation'
  if (inputKind === 'text' || inputKind === 'tool_trace') return 'text'
  if (inputKind === 'qa_pair') return 'qa_pair'
  if (inputKind === 'memories') return 'memories'
  return 'json'
}

const asRecord = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null

/** 形状不认识时退回 JSON 编辑器，原样保留样例，用户仍能试跑 */
function toJson(raw: unknown): InputState {
  return { mode: 'json', messages: [], text: '', qaPair: '', memories: [], jsonText: JSON.stringify(raw ?? {}, null, 2) }
}

function toMessages(raw: unknown): MessageRow[] | null {
  if (!Array.isArray(raw)) return null
  const rows: MessageRow[] = []
  for (const item of raw) {
    if (typeof item === 'string') {
      rows.push({ role: 'user', content: item })
      continue
    }
    const rec = asRecord(item)
    if (!rec || typeof rec.content !== 'string') return null
    rows.push({ role: typeof rec.role === 'string' && rec.role ? rec.role : 'user', content: rec.content })
  }
  return rows
}

function toMemories(raw: unknown): MemoryRow[] | null {
  if (!Array.isArray(raw)) return null
  const rows: MemoryRow[] = []
  for (let i = 0; i < raw.length; i++) {
    const item = raw[i]
    if (typeof item === 'string') {
      rows.push({ id: String(i + 1), memory: item })
      continue
    }
    const rec = asRecord(item)
    if (!rec || typeof rec.memory !== 'string') return null
    rows.push({ id: rec.id === undefined || rec.id === null ? String(i + 1) : String(rec.id), memory: rec.memory })
  }
  return rows
}

function toQaPair(raw: unknown): string | null {
  if (typeof raw === 'string') return raw
  const rec = asRecord(raw)
  if (!rec) return null
  if (typeof rec.qa_pair === 'string') return rec.qa_pair
  if (typeof rec.question === 'string' || typeof rec.answer === 'string') {
    return [String(rec.question ?? ''), String(rec.answer ?? '')].filter(Boolean).join('\n')
  }
  return null
}

/** 样例 / 空输入 → 面板状态。声明了 kind 但样例形状不对，就直接用 JSON 编辑器 */
export function makeInputState(kind: EditorKind, raw: unknown): InputState {
  const rec = asRecord(raw) ?? {}
  if (kind === 'conversation') {
    const messages = toMessages(rec.messages)
    if (messages)
      return {
        mode: 'conversation',
        messages: messages.length ? messages : [{ role: 'user', content: '' }],
        text: '',
        qaPair: '',
        memories: [],
        jsonText: '',
      }
    if (raw === undefined) return { ...toJson({}), mode: 'conversation', messages: [{ role: 'user', content: '' }] }
    return toJson(raw)
  }
  if (kind === 'text') {
    if (typeof rec.text === 'string') return { mode: 'text', messages: [], text: rec.text, qaPair: '', memories: [], jsonText: '' }
    if (raw === undefined) return { ...toJson({}), mode: 'text' }
    return toJson(raw)
  }
  if (kind === 'qa_pair') {
    const v = toQaPair(raw)
    if (v !== null) return { mode: 'qa_pair', messages: [], text: '', qaPair: v, memories: [], jsonText: '' }
    if (raw === undefined) return { ...toJson({}), mode: 'qa_pair' }
    return toJson(raw)
  }
  if (kind === 'memories') {
    const rows = toMemories(rec.memories)
    if (rows) {
      return {
        mode: 'memories',
        messages: [],
        text: '',
        qaPair: '',
        memories: rows.length ? rows : [{ id: '1', memory: '' }],
        jsonText: '',
      }
    }
    if (raw === undefined) return { ...toJson({}), mode: 'memories', memories: [{ id: '1', memory: '' }] }
    return toJson(raw)
  }
  return toJson(raw)
}

export type BuildResult = { ok: true; input: unknown } | { ok: false; reason: 'empty' | 'json' }

/** 面板状态 → 请求里的 `input`。空内容与 JSON 语法错误在点运行前就拦住 */
export function buildInput(state: InputState): BuildResult {
  if (state.mode === 'conversation') {
    const messages = state.messages.filter((m) => m.content.trim())
    if (!messages.length) return { ok: false, reason: 'empty' }
    return { ok: true, input: { messages: messages.map((m) => ({ role: m.role, content: m.content })) } }
  }
  if (state.mode === 'text') {
    if (!state.text.trim()) return { ok: false, reason: 'empty' }
    return { ok: true, input: { text: state.text } }
  }
  if (state.mode === 'qa_pair') {
    if (!state.qaPair.trim()) return { ok: false, reason: 'empty' }
    return { ok: true, input: { qa_pair: state.qaPair } }
  }
  if (state.mode === 'memories') {
    const memories = state.memories.filter((m) => m.memory.trim())
    if (!memories.length) return { ok: false, reason: 'empty' }
    return {
      ok: true,
      input: { memories: memories.map((m, i) => ({ id: m.id.trim() || String(i + 1), memory: m.memory })) },
    }
  }
  const parsed = validateJsonInput(state.jsonText)
  return parsed.ok ? { ok: true, input: parsed.value } : { ok: false, reason: 'json' }
}

/** 按 `inputSchema` 检查必填与类型。返回第一处问题的路径（如 `memories[0].memory`），合法返回 null。
 *
 *  只实现注册表实际用到的子集（object / array / string / number / integer / boolean / enum）；
 *  遇到不认识的写法就跳过，宁可放过也不误拦。 */
export function schemaViolation(schema: unknown, value: unknown, path = ''): string | null {
  const rec = asRecord(schema)
  if (!rec) return null

  if (Array.isArray(rec.enum) && !rec.enum.some((v) => v === value)) return path || '(value)'

  const type = typeof rec.type === 'string' ? rec.type : null
  if (type) {
    const ok =
      (type === 'object' && value !== null && typeof value === 'object' && !Array.isArray(value)) ||
      (type === 'array' && Array.isArray(value)) ||
      (type === 'string' && typeof value === 'string') ||
      (type === 'boolean' && typeof value === 'boolean') ||
      (type === 'number' && typeof value === 'number' && Number.isFinite(value)) ||
      (type === 'integer' && typeof value === 'number' && Number.isInteger(value))
    if (!ok) return path || '(value)'
  }

  if (rec.items && Array.isArray(value)) {
    const itemSchema = Array.isArray(rec.items) ? rec.items[0] : rec.items
    for (let i = 0; i < value.length; i++) {
      const bad = schemaViolation(itemSchema, value[i], `${path}[${i}]`)
      if (bad) return bad
    }
  }

  if (type === 'object' || rec.properties) {
    const obj = asRecord(value)
    if (!obj) return path || '(value)'
    const required = Array.isArray(rec.required) ? rec.required : []
    for (const key of required) {
      if (typeof key !== 'string') continue
      const childPath = path ? `${path}.${key}` : key
      if (obj[key] === undefined || obj[key] === null || obj[key] === '') return childPath
    }
    const props = asRecord(rec.properties)
    if (props) {
      for (const [key, child] of Object.entries(props)) {
        if (obj[key] === undefined) continue
        const bad = schemaViolation(child, obj[key], path ? `${path}.${key}` : key)
        if (bad) return bad
      }
    }
  }

  return null
}

/** 样例 id 以语言开头（`zh-...` / `en-...`）。没有匹配时调用方退回全部样例 */
export function sampleMatchesLang(id: string, lang: string): boolean {
  return id === lang || id.startsWith(`${lang}-`)
}

/** 版本号显示：优先数据面给的槽位序号，接口还没给时用调用方算的兜底序号 */
export function versionLabel(versionNo: number | undefined, fallback: number): string {
  const n = typeof versionNo === 'number' && versionNo > 0 ? versionNo : fallback
  return n > 0 ? `v${n}` : ''
}

/** 结果表格的列宽：短列窄、value 列宽，让所有列在窄面板里也可见（换行不裁切） */
export function tableColumnWidths(count: number): string[] {
  if (count <= 0) return []
  if (count === 1) return ['100%']
  if (count === 2) return ['34%', '66%']
  if (count === 3) return ['24%', '24%', '52%']
  if (count === 4) return ['20%', '16%', '42%', '22%']
  if (count === 5) return ['16%', '15%', '34%', '14%', '21%']
  // 6 列以上：前两列给短键，第三列留给 value，其余均分
  const rest = Math.floor((100 - 14 - 13 - 32) / (count - 3))
  const widths = ['14%', '13%', '32%']
  for (let i = 3; i < count; i++) widths.push(`${rest}%`)
  return widths
}

/** 对比结果的默认排布：1440 及以上左右并排，更窄时上下堆叠 */
export type CompareLayout = 'split' | 'stacked'
export function defaultCompareLayout(viewportWidth: number): CompareLayout {
  return viewportWidth >= 1440 ? 'split' : 'stacked'
}

/** 并排时一张结果卡的字段分配：value 当正文，key/memory_type/tags 当小字，
 *  `memory_form` 这类次要字段放进折叠详情。列名不认识时按最长内容选正文。 */
export function compactCardLayout(
  columns: string[],
  row: Record<string, string>,
): {
  primary: string
  meta: string[]
  details: string[]
} {
  if (columns.length === 0) return { primary: '', meta: [], details: [] }
  const primary =
    columns.find((c) => c === 'value') ??
    columns.reduce((best, c) => (String(row[c] ?? '').length > String(row[best] ?? '').length ? c : best), columns[0])
  const rest = columns.filter((c) => c !== primary)
  const details: string[] = rest.filter((c) => c === 'memory_form')
  const restMeta = rest.filter((c) => !details.includes(c))
  return { primary, meta: restMeta.slice(0, 3), details: [...details, ...restMeta.slice(3)] }
}

/** 「会整段存原文」这类布尔项的中英文取值。抽出来是为了自检能验证真假没有写反 */
export function flagTextKey(value: boolean): 'prompts.test.yes' | 'prompts.test.no' {
  return value ? 'prompts.test.yes' : 'prompts.test.no'
}

/** 切换提示词时选语言：原来的语言还在就保持，否则用第一种 */
export function pickLang(languages: string[], current: string): string {
  if (languages.includes(current)) return current
  return languages[0] ?? ''
}

/** 左侧目录的一层。技能相关的抽取提示词单独成一层，和记忆抽取分开。 */
export interface PromptFolder {
  id: string
  items: PromptListItem[]
}

const FOLDER_ORDER = ['extract', 'process', 'dream']

export function folderOf(item: Pick<PromptListItem, 'key' | 'group'>): string {
  return item.group
}

/** 按目录分组并排序；空目录不出现，未知分组排在已知分组之后。 */
export function promptFolders(items: PromptListItem[]): PromptFolder[] {
  const byId = new Map<string, PromptListItem[]>()
  for (const item of items) {
    const id = folderOf(item)
    byId.set(id, [...(byId.get(id) ?? []), item])
  }
  const rank = (id: string) => (FOLDER_ORDER.includes(id) ? FOLDER_ORDER.indexOf(id) : FOLDER_ORDER.length)
  return [...byId.entries()].sort(([a], [b]) => rank(a) - rank(b)).map(([id, list]) => ({ id, items: list }))
}

/** 默认只展开记忆抽取；当前选中项所在目录、以及有搜索词时的全部目录也展开。 */
export function folderOpen(id: string, opts: { toggled: Record<string, boolean>; activeFolder: string; searching: boolean }): boolean {
  if (opts.searching) return true
  if (id in opts.toggled) return opts.toggled[id]
  return id === 'extract' || id === opts.activeFolder
}

/** 保存前是否要用户勾选确认：没试跑过、草稿改过、或最近一次试跑没通过，都要确认 */
export function needsSaveConfirm(run: { draft: string; ok: boolean } | null, draft: string): boolean {
  if (!run) return true
  return run.draft !== draft || !run.ok
}

/** 同一操作 800 毫秒内的重复点击直接忽略（体验层，不是安全边界） */
export function isRepeatClick(last: number, now: number, windowMs = 800): boolean {
  return now - last < windowMs
}

/** 倒计时秒数：向上取整，已经到期就是 0 */
export function countdownSeconds(deadline: number, now: number): number {
  return Math.max(0, Math.ceil((deadline - now) / 1000))
}

/** 整个项目里当前不是默认值的槽位 */
export function customizedSlots(items: PromptListItem[]): { key: string; lang: string }[] {
  const out: { key: string; lang: string }[] = []
  for (const item of items) {
    for (const lang of item.languages) {
      if (item.state[lang]?.source === 'project') out.push({ key: item.key, lang })
    }
  }
  return out
}

/** 试跑输入的 JSON 编辑器：空串当空对象，其余必须能被 JSON.parse */
export function validateJsonInput(text: string): { ok: true; value: unknown } | { ok: false } {
  if (!text.trim()) return { ok: true, value: {} }
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch {
    return { ok: false }
  }
}

export interface ResultTable {
  columns: string[]
  rows: Record<string, string>[]
}

function cellText(v: unknown): string {
  if (v === undefined || v === null || v === '') return '—'
  if (Array.isArray(v)) return v.length ? v.map(cellText).join(', ') : '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/** 把试跑的结构化输出变成表格：优先输出契约里的键，找不到数组就退到第一个数组；
 *  一个数组都没有就返回 null，交给界面显示原始 JSON。 */
export function structuredTable(output: unknown, contract: { requiredKeys: string[]; itemKeys: string[] }): ResultTable | null {
  if (!output || typeof output !== 'object' || Array.isArray(output)) return null
  const obj = output as Record<string, unknown>

  let list: unknown[] | null = null
  for (const key of contract.requiredKeys) {
    if (Array.isArray(obj[key])) {
      list = obj[key] as unknown[]
      break
    }
  }
  if (!list) {
    for (const v of Object.values(obj)) {
      if (Array.isArray(v)) {
        list = v
        break
      }
    }
  }
  if (!list) return null

  const rows: Record<string, string>[] = list.map((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return { value: cellText(item) }
    const row: Record<string, string> = {}
    for (const [k, v] of Object.entries(item as Record<string, unknown>)) row[k] = cellText(v)
    return row
  })

  const present = new Set<string>()
  for (const row of rows) for (const k of Object.keys(row)) present.add(k)

  const columns: string[] = []
  for (const k of contract.itemKeys) if (present.has(k)) columns.push(k)
  for (const k of [...present].sort()) if (!columns.includes(k)) columns.push(k)
  if (!columns.length) columns.push('value')

  return { columns, rows }
}
