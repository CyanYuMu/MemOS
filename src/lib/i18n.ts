/** 界面文案的加载与取词。
 *
 *  **文案本体在 `src/locales/zh.json` 与 `src/locales/en.json`**，这里只负责取词、插值、
 *  单复数。之所以把词条从 .ts 搬进 .json：改文案的人不该为了改一句话去碰 TS 语法，
 *  文案也该能被单独 diff、单独 review（2026-10-19 用户提的「搞一个 i18n.json，
 *  方便编辑文案」）。
 *
 *  ⚠️ 两件事靠 `scripts/verify-i18n.mjs` 兜住，别指望编译期：
 *    1. zh.json 与 en.json 的**键必须一一对应**（少一条就是英文界面里冒出一句中文）；
 *    2. `ParamEntries` 里列出的键，两个 JSON 里**必须真的带占位符**，反之亦然
 *       ——这份类型是手写的，JSON 里看不出哪条带参数，写漏了编译期不会报。
 */

import enJson from '../locales/en.json'
import zhJson from '../locales/zh.json'
import { hasPlaceholder, interpolate } from './interpolate'

export type Lang = 'zh' | 'en'

/** 一条词条：普通的是字符串；需要分单复数的写成 `{ one, other }`。 */
export type Entry = string | { one: string; other: string }

export type TKey = keyof typeof zhJson

/** 运行时字典。zh.json 的形状就是权威形状，en.json 靠 verify 脚本对齐。 */
export const DICTS: Record<Lang, Record<TKey, Entry>> = {
  zh: zhJson as Record<TKey, Entry>,
  en: enJson as Record<TKey, Entry>,
}

/** 带参数的词条：值里含 `{0}` 这类占位符，取出来是**函数**，要再调一次。
 *
 *  ⚠️ 这份清单是手写的，且必须与两个 JSON 的实际内容一致（verify 脚本会核对）。
 *  参数名只为了读起来清楚，插值按位置走。
 *
 *  占位符名有两种：`{0}` / `{1}` 按位置取参数；`{total}` 这种按名字取，这时参数
 *  是一个对象（`graph.hud` 就是这种，它接的是一个状态快照而不是一串位置参数）。
 *  同一模板里两种不能混用。 */
export interface ParamEntries {
  'quickstart.agentCode.prompt': (language: string, examples: string) => string
  'chat.snapshot.count': (n: number) => string
  'chat.guide.intro.cuisines': (value: string) => string
  'chat.guide.intro.region': (value: string) => string
  'chat.guide.saved': (n: number) => string
  'chat.guide.recalled': (n: number) => string
  'chat.guide.countdown': (n: number) => string
  'playground.logs.updated': (n: number) => string
  'playground.logs.deleted': (n: number) => string
  'chat.feedback.correctionMessage': (answer: string, correction: string) => string
  'common.total': (n: number) => string
  'common.pageOf': (page: number, pages: number) => string
  'apikeys.confirmDelete': (name: string) => string
  'apikeys.confirmRegenerate': (name: string) => string
  'apikeys.deleted': (name: string) => string
  /** 上下文窗口已经格式化好（如 128K） */
  'chat.context': (contextWindow: string) => string
  'chat.history.error': (reason: string) => string
  'chat.history.showingRecent': (n: number) => string
  'chat.history.showingAll': (n: number) => string
  'chat.status.queued': (n: number) => string
  'playground.logs.found': (n: number) => string
  'playground.logs.extracted': (n: number) => string
  'playground.logs.added': (n: number) => string
  'chat.memory.count': (n: number) => string
  'chat.memory.viewAll': (n: number) => string
  'chat.memory.fromQuery': (query: string) => string
  'chat.memory.minutesAgo': (n: number) => string
  'chat.memory.hoursAgo': (n: number) => string
  'chat.memory.daysAgo': (n: number) => string
  'chat.tool.hits': (n: number) => string
  'playground.profile.intro.name': (value: { name: string }) => string
  'playground.profile.intro.preferences': (value: { preferences: string }) => string
  'playground.profile.intro.style': (value: { style: string }) => string
  'chat.config.apiKeyKeep': (masked: string) => string
  'projects.member.confirmRemove': (name: string) => string
  'projects.invite.days': (days: string) => string
  'users.assignDesc': (name: string) => string
  'users.assigned': (user: string, project: string) => string
  'users.confirmDisable': (name: string) => string
  'projects.deleteConfirm': (name: string) => string
  'kb.deleteConfirm': (name: string) => string
  'kb.deleteFileConfirm': (name: string) => string
  'users.deleteConfirm': (name: string) => string
  'projects.disableConfirm': (name: string) => string
  'memory.deleteConfirm': (n: number) => string
  'memory.deleteDone': (n: number) => string
  /** 本地记忆镜像：条数、配额、设备名都按位置插值 */
  'localMemories.action.batchPromoteCount': (n: number) => string
  'localMemories.action.retryFailed': (n: number) => string
  'localMemories.quota.summary': (usedBytes: string, limitBytes: string, usedRows: string, limitRows: string) => string
  'localMemories.quota.pending': (pendingBytes: string, pendingRows: string) => string
  'devices.quota.pending': (pendingBytes: string, pendingRows: string) => string
  'localMemories.deviceRevoked': (label: string) => string
  'localMemories.empty.quota.desc': (limit: string) => string
  'localMemories.deleteConfirm': (n: number) => string
  'localMemories.deleteDone': (n: number) => string
  'localMemories.promoteQueued': (n: number) => string
  'localMemories.promoteFailedTitle': (n: number) => string
  'localMemories.promotePartial': (queued: number, failed: number) => string
  'localMemories.status.promoting': (status: string) => string
  /** 本地设备页 */
  'devices.activeCount': (active: number, total: number) => string
  'devices.revokeConfirm': (label: string) => string
  'usage.kpi.rangeHint': (d: number) => string
  'usage.kpi.shareHint': (p: string) => string
  'usage.kpi.activeDays': (n: number, total: number) => string
  /** `n` 是**已经格式化好**的字符串：千分位属于调用侧的事，词条只管排版 */
  'usage.peak': (date: string, n: string) => string
  /** 星座图里每个会话区域上方那颗胶囊：会话 id 尾号 + 这个会话有几条记忆。
   *  ⚠️ 文案**不在** `constellation.ts` 里拼——那一层拿不到语言（见 `PlacedCluster.sessionTail`）。 */
  'graph.clusterLabel': (tail: string, n: number) => string
  'graph.truncated': (n: number) => string
  'graph.linkCount': (n: number) => string
  'graph.sources': (total: number) => string
  /** 搜索框下方的实时结果数；`aria-live` 用的也是这一条 */
  'graph.searchResults': (n: number) => string
  /** 聚焦胶囊：`{0}` 跳数、`{1}` 条数。中英文语序不同，两个占位符都按位置取。 */
  'graph.focusChip': (depth: number, count: number) => string
  'graph.focusDepth': (n: number) => string
  'graph.focusRange': (count: number, depth: number) => string
  /** `zoom` 同理，调用侧先 `toFixed(2)` */
  'graph.hud': (o: { total: number; shown: number; labels: number; links: number; zoom: string }) => string
  'graph.sessionSize': (n: number) => string
  /** 下面三条是**本地生成**的接口错误，展示时按当前语言取词（见 `errText`） */
  'error.notJson': (status: number) => string
  'error.requestFailed': (status: number) => string
  'error.code': (code: number) => string
  /* ------------------------------------------------ 提示词工作台（带参数）
   * 字段名/版本号这类格式化留给调用侧，词条只管排版。 */
  'prompts.output.line': (required: string, itemFields: string) => string
  'prompts.conflict.desc': (version: string) => string
  'prompts.reset.scope.itemDesc': (n: number) => string
  'prompts.reset.scope.projectDesc': (n: number) => string
  'prompts.reset.projectConfirm': (n: number) => string
  'prompts.reset.done': (n: number) => string
  'prompts.folder.changed': (n: number) => string
  'prompts.details.summary': (n: number) => string
  'prompts.diff.fold': (n: number) => string
  'prompts.diff.source.version': (version: string) => string
  'prompts.diff.dir': (left: string, right: string) => string
  'prompts.test.retryIn': (seconds: number) => string
  'prompts.test.runs': (n: number) => string
  'prompts.test.usageValue': (promptTokens: number, completionTokens: number) => string
  'prompts.test.contract.missing': (fields: string) => string
  'prompts.test.schemaInvalid': (field: string) => string
  'prompts.ai.validationFailed': (message: string) => string
}

/** 选单复数形式：第一个参数是数字且等于 1 就用 one，否则用 other */
function pick(entry: Entry, args: unknown[]): string {
  if (typeof entry === 'string') return entry
  return args[0] === 1 ? entry.one : entry.other
}

/** 不分单复数时用哪一支：中文只有一支，英文取 other（复数）那一支 */
const plainOf = (entry: Entry | undefined) => (!entry ? '' : typeof entry === 'string' ? entry : entry.other)

/** 取词。返回值要么是字符串（普通词条），要么是函数（带参数的词条）。 */
export function makeT(lang: Lang) {
  return <K extends TKey>(key: K): K extends keyof ParamEntries ? ParamEntries[K] : string => {
    const entry = DICTS[lang]?.[key]
    if (entry === undefined) {
      return String(key) as never
    }
    const tpl = plainOf(entry)
    if (!hasPlaceholder(tpl)) {
      // 不带参数的词条直接给字符串——调用侧写 `t('nav.docs')` 就是一个 string，
      // 不能让它变成 `t('nav.docs')()`。
      return (typeof entry === 'string' ? entry : tpl) as never
    }
    return ((...args: unknown[]) => interpolate(pick(entry, args), args)) as never
  }
}

/** 把一条异常变成能显示在页面/提示里的一句话。
 *
 *  ⚠️ 两条路必须分开：
 *    - **本地生成**的错误（`ApiError` 带 `msg`）走文案字典，能翻译；
 *    - **上游返回**的 message 原样显示，**不翻译**——那是服务端的原文，是排障依据，
 *      翻一遍只会让日志和界面上的话对不上。
 */
export function errText(lang: Lang, e: unknown): string {
  const m = (e as { msg?: { key: TKey; args: unknown[] } } | null)?.msg
  if (m) return interpolate(textOf(lang, m.key), m.args)
  if (e instanceof Error) return e.message
  return String(e)
}

/** 动态 key（非字面量）取词，返回值一定是字符串。
 *
 *  取不到就**原样回显键名**：这样页面上一眼能看出「这里少了一条文案」，
 *  而不是静默渲染成空——空着会被当成「设计上就没有」。 */
export function textOf(lang: Lang, key: TKey): string {
  const entry = DICTS[lang][key]
  if (entry === undefined) return key
  return typeof entry === 'string' ? entry : entry.other
}
