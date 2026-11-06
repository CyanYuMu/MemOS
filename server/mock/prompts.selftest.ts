/** mock 的提示词数据与校验逻辑自检。`node server/mock/prompts.selftest.ts`，`pnpm verify` 也会跑。
 *
 *  覆盖的是不经过 HTTP 就能确定的部分：注册表完整性、静态校验的各种分支、槽位读取与
 *  重置语义、试跑与输入限额、限流桶。HTTP 状态码那层（400/404/409/413/429）在路由里，
 *  由浏览器验收覆盖。
 */

import {
  LIMITS,
  PROMPTS,
  checkInput,
  findPrompt,
  makeBucket,
  readSlot,
  simulateRun,
  stateOf,
  validatePrompt,
  type PromptDef,
} from './prompts.ts'
import { seedPromptVersions } from './prompts.ts'
import type { Row } from './data.ts'

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

const versions = seedPromptVersions()
const def = (key: string): PromptDef => {
  const d = findPrompt(key)
  if (!d) throw new Error(`注册表缺少 ${key}`)
  return d
}

/* ---------------------------------------------------------------- 注册表 */

{
  const keys = PROMPTS.map((p) => p.key)
  check('注册表覆盖 12 条提示词', PROMPTS.length === 12, keys.join(', '))
  check('每条 key 唯一', new Set(keys).size === keys.length)

  const chat = def('extract.chat')
  check('对话抽取有中英两种语言', chat.languages.join(',') === 'zh,en', chat.languages.join(','))
  check('对话抽取的默认值两种语言都有', Boolean(chat.defaults.zh) && Boolean(chat.defaults.en))
  check('对话抽取用 ${conversation}', chat.syntax === 'dollar' && chat.defaults.zh.includes('${conversation}'))

  const dream = PROMPTS.filter((p) => p.group === 'dream')
  check('包含四条 Dream 提示词', dream.length === 4, dream.map((p) => p.key).join(', '))
  const englishOnly = PROMPTS.filter((p) => p.languages.length === 1 && p.languages[0] === 'en')
  check('至少有一条只有英文', englishOnly.length > 0, englishOnly.map((p) => p.key).join(', '))
  check('动机提示词有只读后缀', Boolean(def('dream.motive').lockedSuffix))
  check(
    '输入 kind 都有 schema',
    PROMPTS.every((p) => p.inputSchema && typeof p.inputSchema === 'object'),
  )
  check('没有调用方的合并提示词不进注册表', !PROMPTS.some((p) => p.key === 'process.merge'))
  const qa = def('extract.qa_pair')
  check('问答对按真实的 qa_pair 字段描述输入', qa.inputKind === 'qa_pair' && JSON.stringify(qa.inputSchema).includes('"qa_pair"'))
  const mem = def('process.hallucination_filter')
  const memItems = (mem.inputSchema.properties as Record<string, { items?: { required?: string[] } }>).memories?.items
  check('记忆输入是 {id, memory} 对象数组', mem.inputKind === 'memories' && JSON.stringify(memItems?.required) === '["id","memory"]')
  check(
    '样例都带语言，方便按当前语言筛选',
    PROMPTS.every((p) => p.sampleInputs.every((x) => x.lang === 'zh' || x.lang === 'en')),
  )
  check(
    '显式与隐式偏好是两条独立的 key',
    PROMPTS.some((p) => p.key === 'extract.preference_explicit') && PROMPTS.some((p) => p.key === 'extract.preference_implicit'),
  )
  check(
    '文档、偏好、工具轨迹用 brace 语法',
    ['extract.doc', 'extract.preference_explicit', 'extract.preference_implicit', 'extract.tool_trace'].every(
      (k) => def(k).syntax === 'brace',
    ),
  )
  check(
    '事件与 Dream 用 format 语法',
    ['extract.event', 'dream.motive', 'dream.reasoning', 'dream.context_binding', 'dream.context_summary'].every(
      (k) => def(k).syntax === 'format',
    ),
  )
}

/* --------------------------------------------------- 默认值必须全部通过校验 */

{
  const bad: string[] = []
  for (const p of PROMPTS) {
    for (const lang of p.languages) {
      const v = validatePrompt(p, lang, p.defaults[lang] ?? '')
      if (!v.ok) bad.push(`${p.key}/${lang}: ${v.errors.map((e) => e.code).join(',')}`)
    }
  }
  check('每条内置默认值都能通过自己的校验', bad.length === 0, bad.join('；') || '全部通过')
}

/* ---------------------------------------------------------------- 静态校验 */

{
  const chat = def('extract.chat')

  const missing = validatePrompt(chat, 'zh', '没有占位符，也没有输出结构。')
  check(
    '缺少必填占位符会被拒绝',
    !missing.ok && missing.errors.some((e) => e.code === 'missing_placeholder'),
    missing.errors.map((e) => e.code).join(','),
  )

  const unknown = validatePrompt(chat, 'zh', '${conversation} ${nope} memory list')
  check(
    '注册表之外的占位符会被拒绝',
    unknown.errors.some((e) => e.code === 'unknown_placeholder'),
  )

  const empty = validatePrompt(chat, 'zh', '   ')
  check('空内容会被拒绝', !empty.ok && empty.errors.some((e) => e.code === 'empty'))

  const tooLong = validatePrompt(chat, 'zh', '${conversation} memory list ' + 'x'.repeat(LIMITS.body))
  check(
    '超过 20000 字符会被拒绝',
    tooLong.errors.some((e) => e.code === 'too_long'),
  )

  const draftFailInput = { messages: [{ role: 'user', content: '__llm_error_draft__ 保留这一侧的输入' }] }
  const draftFail = simulateRun(chat, chat.defaults.zh ?? '', draftFailInput, 'draft')
  const defaultStillOk = simulateRun(chat, chat.defaults.zh ?? '', draftFailInput, 'default')
  check('只让草稿侧失败时，默认侧照常返回结果', !draftFail.ok && defaultStillOk.ok && !defaultStillOk.fallbackTriggered)
  check(
    '失败侧带错误文案与稳定 code',
    draftFail.error?.code === 'llm_error' && Boolean(draftFail.error?.message),
    JSON.stringify(draftFail.error),
  )

  const defaultFailInput = { messages: [{ role: 'user', content: '__llm_error_default__ 保留这一侧的输入' }] }
  const defaultFail = simulateRun(chat, chat.defaults.zh ?? '', defaultFailInput, 'default')
  const draftStillOk = simulateRun(chat, chat.defaults.zh ?? '', defaultFailInput, 'draft')
  check('只让默认侧失败时，草稿侧照常返回结果', !defaultFail.ok && draftStillOk.ok && draftStillOk.parseOk)

  const motive = def('dream.motive')
  const brace = validatePrompt(motive, 'en', '{memories')
  check(
    'format 语法花括号不配对会被拒绝',
    brace.errors.some((e) => e.code === 'brace'),
  )

  const positional = validatePrompt(motive, 'en', '{0} motive list')
  check(
    'format 语法不接受位置参数',
    positional.errors.some((e) => e.code === 'positional'),
  )

  const literal = validatePrompt(motive, 'en', 'Write {{ }} literally.\n{memories}\nmotive list')
  check('字面花括号写成双花括号是合法的', literal.ok, JSON.stringify(literal.errors))

  const conversion = validatePrompt(motive, 'en', '{memories!r}\nmotive list')
  check('format 的转换后缀不影响字段名', conversion.ok, JSON.stringify(conversion.errors))

  const warn = validatePrompt(chat, 'zh', '${conversation}\n只说人话')
  check(
    '少了输出结构关键字只警告、不阻止保存',
    warn.ok && warn.warnings.some((w) => w.code === 'contract_keyword'),
    JSON.stringify(warn.warnings),
  )

  const wrongLang = validatePrompt(chat, 'jp', chat.defaults.zh ?? '')
  check('不支持的语言直接拒绝', !wrongLang.ok && wrongLang.errors[0]?.code === 'invalid_lang')

  const doc = def('extract.doc')
  const jsonBraces = validatePrompt(doc, 'zh', '示例输出：{"key": "x", "tags": []}\n文档：\n{document}\n返回 memory list。')
  check('brace 语法允许正文里的 JSON 花括号', jsonBraces.ok, JSON.stringify(jsonBraces.errors))
  const braceMissing = validatePrompt(doc, 'zh', '示例：{"key": "x"}，没有占位符。')
  check(
    'brace 语法缺必填占位符会被拒绝',
    braceMissing.errors.some((e) => e.code === 'missing_placeholder'),
    JSON.stringify(braceMissing.errors),
  )
  const braceUnbalanced = validatePrompt(doc, 'zh', '{document} 后面有个孤立的 }')
  check('brace 语法不检查花括号配对', braceUnbalanced.ok, JSON.stringify(braceUnbalanced.errors))
}

/* ---------------------------------------------------------- 槽位与项目隔离 */

{
  const chat = def('extract.chat')
  const mine = readSlot(versions, 'proj_default', chat, 'zh')
  check('默认项目的对话抽取有项目覆盖', mine.source === 'project' && mine.versionId === 102, `${mine.source}/${mine.versionId}`)
  check(
    '槽位版本号从 1 开始且与全局 id 不同',
    mine.versionNo > 0 && mine.versionNo !== mine.versionId,
    `no=${mine.versionNo} id=${mine.versionId}`,
  )
  check('项目覆盖带更新时间与作者', Boolean(mine.updatedAt) && Boolean(mine.author), `${mine.updatedAt} ${mine.author}`)

  const other = readSlot(versions, 'proj_alpha', chat, 'zh')
  check('另一个项目读到的是内置默认', other.source === 'default' && other.versionId === 0 && other.body === chat.defaults.zh)
  check('默认值没有版本号，用 0 表示', other.versionNo === 0)
  check('默认槽位的更新时间与作者是 null（与真实响应一致）', other.updatedAt === null && other.author === null, JSON.stringify(other))

  const en = readSlot(versions, 'proj_default', chat, 'en')
  check('同项目另一种语言不受影响', en.source === 'default')

  const state = stateOf(versions, 'proj_default', chat)
  check('列表状态按语言分别给出', state.zh.source === 'project' && state.en.source === 'default')
  check('列表状态带槽位版本号', state.zh.versionNo > 0 && state.en.versionNo === 0)

  // 重置：追加一行 kind=reset 后回到默认；之后的 save 再次生效
  const resetRow: Row = {
    id: 'test_reset',
    versionId: 900,
    projectId: 'proj_default',
    promptKey: 'extract.chat',
    lang: 'zh',
    kind: 'reset',
    body: '',
    note: '',
    author: 'tester',
    parentVersionId: 102,
    createdAt: '2026-10-29 00:00:00',
  }
  const afterReset = readSlot([...versions, resetRow], 'proj_default', chat, 'zh')
  check('reset 之后回到内置默认', afterReset.source === 'default' && afterReset.versionId === 0)

  const saveAfterReset: Row = { ...resetRow, id: 'test_save', versionId: 901, kind: 'save', body: '${conversation} memory list' }
  const afterSave = readSlot([...versions, resetRow, saveAfterReset], 'proj_default', chat, 'zh')
  check('reset 之后再保存会重新生效', afterSave.source === 'project' && afterSave.versionId === 901)
}

/* ------------------------------------------------------------------ 试跑 */

{
  const chat = def('extract.chat')
  const input = { messages: [{ role: 'user', content: '我喜欢结论先行的回答' }] }

  const ok = simulateRun(chat, chat.defaults.zh ?? '', input, 'draft')
  check('默认模板试跑解析成功', ok.ok && ok.parseOk && !ok.fallbackTriggered)
  check('默认模板满足输出契约', ok.contract.ok && ok.contract.missing.length === 0)
  check('试跑带耗时、模型与用量', ok.latencyMs > 0 && ok.model.length > 0 && ok.usage.promptTokens > 0)
  check('解析结果按原字段透传', Boolean((ok.output as Record<string, unknown>)?.['memory list']))

  const noKeyword = simulateRun(chat, '${conversation}\n随便说点什么', input, 'draft')
  check('正文没写输出结构时，试跑报告会整段存原文', noKeyword.ok && !noKeyword.parseOk && noKeyword.fallbackTriggered)
  check('不满足契约时指出缺哪个键', !noKeyword.contract.ok && noKeyword.contract.missing.includes('memory list'))

  const llmFail = simulateRun(chat, chat.defaults.zh ?? '', { messages: [{ role: 'user', content: '__llm_error__' }] }, 'draft')
  check('模型失败时 ok=false 且带稳定 code', !llmFail.ok && llmFail.error?.code === 'llm_error', JSON.stringify(llmFail.error))

  const draftFailInput = { messages: [{ role: 'user', content: '__llm_error_draft__ 保留这一侧的输入' }] }
  const draftFail = simulateRun(chat, chat.defaults.zh ?? '', draftFailInput, 'draft')
  const defaultStillOk = simulateRun(chat, chat.defaults.zh ?? '', draftFailInput, 'default')
  check('只让草稿侧失败时，默认侧照常返回结果', !draftFail.ok && defaultStillOk.ok && !defaultStillOk.fallbackTriggered)
  check(
    '失败侧带错误文案与稳定 code',
    draftFail.error?.code === 'llm_error' && Boolean(draftFail.error?.message),
    JSON.stringify(draftFail.error),
  )

  const defaultFailInput = { messages: [{ role: 'user', content: '__llm_error_default__ 保留这一侧的输入' }] }
  const defaultFail = simulateRun(chat, chat.defaults.zh ?? '', defaultFailInput, 'default')
  const draftStillOk = simulateRun(chat, chat.defaults.zh ?? '', defaultFailInput, 'draft')
  check('只让默认侧失败时，草稿侧照常返回结果', !defaultFail.ok && draftStillOk.ok && draftStillOk.parseOk)

  const motive = def('dream.motive')
  const dreamRun = simulateRun(motive, motive.defaults.en ?? '', { memories: [{ id: 'm1', memory: '用户偏好结论先行' }] }, 'default')
  check('Dream 提示词用样例记忆能跑出结构', dreamRun.parseOk && dreamRun.contract.ok, JSON.stringify(dreamRun.contract))
  check('run 的 label 区分草稿与默认', dreamRun.label === 'default')
  check(
    'run 记录实际使用的模板',
    dreamRun.usedSlots.length === 1 &&
      dreamRun.usedSlots[0].key === 'dream.motive' &&
      dreamRun.usedSlots[0].source === 'default' &&
      dreamRun.usedSlots[0].versionId === 0,
    JSON.stringify(dreamRun.usedSlots),
  )
  check('正常试跑没有告警', dreamRun.warnings.length === 0, JSON.stringify(dreamRun.warnings))

  const ignored = simulateRun(chat, chat.defaults.zh ?? '', { messages: [{ role: 'user', content: '__draft_not_used__' }] }, 'draft')
  check(
    '带标记的输入模拟「没有使用草稿」告警',
    ignored.warnings.some((w) => w.code === 'draft_not_used') && ignored.usedSlots[0].source === 'project',
    JSON.stringify({ warnings: ignored.warnings, usedSlots: ignored.usedSlots }),
  )

  const long = simulateRun(chat, chat.defaults.zh ?? '', { messages: [{ role: 'user', content: 'x'.repeat(30000) }] }, 'draft')
  check('超长原始输出会被截断并标记', long.rawOutput.length === LIMITS.body && long.truncated)
}

/* ---------------------------------------------------------------- 输入限额 */

{
  const chat = def('extract.chat')
  check('空对话被拒绝', Boolean(checkInput(chat, { messages: [] })))
  check('超过 30 条消息被拒绝', Boolean(checkInput(chat, { messages: Array.from({ length: 31 }, () => ({ role: 'user', content: 'x' })) })))
  check('对话总长超限被拒绝', Boolean(checkInput(chat, { messages: [{ role: 'user', content: 'x'.repeat(LIMITS.inputChars + 1) }] })))
  check('合法对话通过', checkInput(chat, { messages: [{ role: 'user', content: '你好' }] }) === null)

  const doc = def('extract.doc')
  check('文本输入为空被拒绝', Boolean(checkInput(doc, { text: '  ' })))
  check('文本输入合法通过', checkInput(doc, { text: '一段文档' }) === null)

  const mem = def('process.hallucination_filter')
  check('记忆输入为空被拒绝', Boolean(checkInput(mem, { memories: [] })))
  check('记忆还是裸字符串时给出字段错误', Boolean(checkInput(mem, { memories: ['旧形状'] })))
  check('记忆对象数组通过', checkInput(mem, { memories: [{ id: 'm1', memory: '一条记忆' }] }) === null)

  const qa = def('extract.qa_pair')
  check('问答为空被拒绝', Boolean(checkInput(qa, { qa_pair: '  ' })))
  check('问答字符串通过', checkInput(qa, { qa_pair: '用户：问题\n助手：回答' }) === null)
  check('旧的 question/answer 形状不再被接受', Boolean(checkInput(qa, { question: 'q', answer: 'a' })))
}

/* ------------------------------------------------------------------ 限流 */

{
  const bucket = makeBucket(3, 6)
  const waits = [bucket(), bucket(), bucket(), bucket()]
  check(
    '突发额度内不拒绝',
    waits.slice(0, 3).every((w) => w === 0),
    waits.join(','),
  )
  check('超出突发额度给出等待秒数', waits[3] >= 9 && waits[3] <= 11, String(waits[3]))
}

/* ------------------------------------------------- 阴性对照：检查真的会失败 */

{
  const chat = def('extract.chat')
  /** 坏校验器：只看非空，不看占位符与输出结构 */
  const naiveOk = (body: string) => body.trim().length > 0
  const bad = '完全没有占位符与输出结构'
  check('阴性对照：只看非空的校验器会放过坏正文', naiveOk(bad))
  check('正解拒绝同一段正文', !validatePrompt(chat, 'zh', bad).ok)

  /** 坏槽位读取：忽略项目维度，返回全局最新一行 */
  const globalLatest = versions.reduce((m, v) => (Number(v.versionId) > Number(m.versionId) ? v : m), versions[0])
  check('阴性对照：忽略项目的读法会把默认项目的内容读给其他项目', globalLatest.projectId === 'proj_default')
  check('正解按项目隔离', readSlot(versions, 'proj_alpha', chat, 'zh').source === 'default')
}

/* ------------------------------------------------------------------ 汇总 */

console.log(`\n${passed}/${passed + failures.length} 通过`)
if (failures.length) {
  console.log('失败项：' + failures.join('、'))
  throw new Error(`mock 提示词自检失败 ${failures.length} 项`)
}
