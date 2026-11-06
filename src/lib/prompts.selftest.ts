/** 工作台里与界面无关的纯逻辑自检。`node src/lib/prompts.selftest.ts`，`pnpm verify` 也会跑。
 *
 *  这些判断（输入形状归一、保存前要不要再确认、800 毫秒连点、结构表格怎么取列、
 *  布尔项的取值）错了不会让页面报错，只会让按钮该灰的时候能点、或者把结果渲染成一片空白。
 *  每条都带一句「坏实现会怎样」的阴性对照，确认检查本身能挂。
 */

import {
  buildInput,
  compactCardLayout,
  defaultCompareLayout,
  countdownSeconds,
  customizedSlots,
  editorKind,
  flagTextKey,
  folderOpen,
  isRepeatClick,
  makeInputState,
  needsSaveConfirm,
  pickLang,
  promptFolders,
  sampleMatchesLang,
  schemaViolation,
  structuredTable,
  tableColumnWidths,
  validateJsonInput,
  versionLabel,
} from './prompts.ts'
import type { PromptListItem } from './api.ts'

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

/* ---------------------------------------------------------------- 输入类型 */

{
  check('对话输入', editorKind('conversation') === 'conversation')
  check('文本输入', editorKind('text') === 'text')
  check('问答对输入', editorKind('qa_pair') === 'qa_pair')
  check('记忆列表输入', editorKind('memories') === 'memories')
  check('不认识的 kind 退化成 JSON 编辑器', editorKind('embedding') === 'json' && editorKind('') === 'json')
}

/* ------------------------------------------------- 输入形状归一（真实形状） */

{
  // 真实注册表的 memories：{"memories":[{"id","memory"}]}
  const real = makeInputState('memories', { memories: [{ id: 'm1', memory: '用户偏好结论先行' }] })
  check(
    '记忆列表按 id/memory 两栏读',
    real.mode === 'memories' && real.memories[0].id === 'm1' && real.memories[0].memory === '用户偏好结论先行',
    JSON.stringify(real.memories),
  )

  const old = makeInputState('memories', { memories: ['旧形状的一条'] })
  check('字符串数组的旧形状也能读', old.mode === 'memories' && old.memories[0].memory === '旧形状的一条')

  const broken = makeInputState('memories', { memories: [{ foo: 1 }] })
  check('记忆条目缺 memory 字段时退回 JSON 编辑器', broken.mode === 'json', broken.mode)

  const qa = makeInputState('qa_pair', { qa_pair: '用户：靠窗\n助手：好的' })
  check('问答对从 qa_pair 字符串读', qa.mode === 'qa_pair' && qa.qaPair.includes('靠窗'))

  const qaObj = makeInputState('qa_pair', { question: 'q', answer: 'a' })
  check('问答对也容忍 question/answer 对象', qaObj.mode === 'qa_pair' && qaObj.qaPair === 'q\na')

  const conv = makeInputState('conversation', { messages: [{ role: 'user', content: '你好' }] })
  check('对话按 role/content 读', conv.mode === 'conversation' && conv.messages[0].content === '你好')
  const convBad = makeInputState('conversation', { messages: [{ role: 'user' }] })
  check('对话条目缺 content 时退回 JSON 编辑器', convBad.mode === 'json')

  const text = makeInputState('text', { text: '一段文本' })
  check('文本从 text 字段读', text.mode === 'text' && text.text === '一段文本')

  const unknown = makeInputState('json', { anything: [1, 2] })
  check('未知类型保留完整样例', unknown.mode === 'json' && unknown.jsonText.includes('anything'))
}

/* -------------------------------------------------------------- JSON 输入 */

{
  check('合法 JSON 被接受', validateJsonInput('{"a": 1}').ok)
  check('不合法 JSON 被拒绝', !validateJsonInput('{a: 1}').ok)
  check(
    '空字符串当成空对象',
    validateJsonInput('   ').ok && JSON.stringify((validateJsonInput('   ') as { value: unknown }).value) === '{}',
  )

  /** 阴性对照：只判断「不以 { 开头」的实现会把数组、数字判成坏值 */
  const naive = (t: string) => t.trim().startsWith('{')
  check('阴性对照：只看首字符的实现会拒绝合法数组', !naive('[1, 2]') && validateJsonInput('[1, 2]').ok)
}

/* ------------------------------------------------------------ 组装请求输入 */

{
  const conv = makeInputState('conversation', { messages: [{ role: 'user', content: '你好' }] })
  const built = buildInput(conv)
  check(
    '对话组装成 messages',
    built.ok && JSON.stringify(built.input) === JSON.stringify({ messages: [{ role: 'user', content: '你好' }] }),
  )

  const empty = buildInput(makeInputState('memories', { memories: [{ id: 'm1', memory: '   ' }] }))
  check('全是空白内容时拒绝运行', !empty.ok && empty.reason === 'empty')

  const memories = makeInputState('memories', {
    memories: [
      { id: 'm1', memory: '一条' },
      { id: '', memory: '另一条' },
    ],
  })
  const memBuilt = buildInput(memories)
  check(
    '记忆列表补上缺失的 id',
    memBuilt.ok &&
      JSON.stringify(memBuilt.input) ===
        JSON.stringify({
          memories: [
            { id: 'm1', memory: '一条' },
            { id: '2', memory: '另一条' },
          ],
        }),
  )

  const qaBuilt = buildInput(makeInputState('qa_pair', { qa_pair: '问\n答' }))
  check('问答对组装成 qa_pair', qaBuilt.ok && JSON.stringify(qaBuilt.input) === JSON.stringify({ qa_pair: '问\n答' }))

  const badJson = makeInputState('json', {})
  const jsonBuilt = buildInput({ ...badJson, jsonText: '{bad' })
  check('JSON 语法错误拦住运行', !jsonBuilt.ok && jsonBuilt.reason === 'json')
}

/* ------------------------------------------------ 按 inputSchema 检查必填 */

{
  const memoriesSchema = {
    type: 'object',
    required: ['memories'],
    properties: {
      memories: {
        type: 'array',
        items: { type: 'object', required: ['id', 'memory'], properties: { id: { type: 'string' }, memory: { type: 'string' } } },
      },
    },
  }
  check('完整输入通过 schema', schemaViolation(memoriesSchema, { memories: [{ id: 'm1', memory: 'x' }] }) === null)
  check('缺顶层必填被指出', schemaViolation(memoriesSchema, {}) === 'memories')
  check('缺条目字段带下标路径', schemaViolation(memoriesSchema, { memories: [{ id: 'm1' }] }) === 'memories[0].memory')
  check('类型不对被指出', schemaViolation(memoriesSchema, { memories: 'x' }) === 'memories')
  check(
    '枚举之外的取值被指出',
    schemaViolation({ type: 'object', properties: { role: { enum: ['user', 'assistant'] } } }, { role: 'bot' }) === 'role',
  )

  /** 阴性对照：只看顶层 required 的实现会放过条目里的缺字段 */
  const naive = (schema: { required?: string[] }, value: Record<string, unknown>) => (schema.required ?? []).every((k) => k in value)
  check('阴性对照：只看顶层的实现会放过条目缺字段', naive(memoriesSchema, { memories: [{ id: 'm1' }] }))
  check('正解能抓到同一条输入', schemaViolation(memoriesSchema, { memories: [{ id: 'm1' }] }) !== null)
}

/* ------------------------------------------------------------ 语言与分组 */

{
  check('当前语言仍在列表里就保持', pickLang(['zh', 'en'], 'en') === 'en')
  check('当前语言不存在就退回第一种', pickLang(['en'], 'zh') === 'en')
  check('没有语言时返回空串', pickLang([], 'zh') === '')
  check('样例按语言前缀过滤', sampleMatchesLang('zh-deploy', 'zh') && !sampleMatchesLang('en-tools', 'zh'))
  check('样例 id 恰好等于语言也算匹配', sampleMatchesLang('en', 'en'))
}

/* ------------------------------------------------------ 保存前是否需要确认 */

{
  const okRun = { draft: 'v2', ok: true }
  check('没有试跑过就要确认', needsSaveConfirm(null, 'v2'))
  check('同一份草稿试跑通过就不用确认', !needsSaveConfirm(okRun, 'v2'))
  check('草稿改过之后要重新试跑', needsSaveConfirm(okRun, 'v3'))
  check('试跑失败要确认', needsSaveConfirm({ draft: 'v2', ok: false }, 'v2'))

  /** 阴性对照：只看「有没有跑过」的实现会把失败的试跑当成通过 */
  const naive = (run: { ok: boolean } | null) => run !== null
  check('阴性对照：只判断跑没跑过会放过失败的试跑', naive({ ok: false }))
  check('正解要求通过', needsSaveConfirm({ draft: 'v2', ok: false }, 'v2'))
}

/* --------------------------------------------------------------- 连点窗口 */

{
  check('800 毫秒内的重复点击被忽略', isRepeatClick(1000, 1799, 800))
  check('整 800 毫秒不再算重复', !isRepeatClick(1000, 1800, 800))
  check('第一次点击不算重复', !isRepeatClick(0, 100000, 800))

  /** 阴性对照：用 `now - last < 0` 这类写法永远为假，连点检查就白写了 */
  const broken = (last: number, now: number) => now - last < 0
  check('阴性对照：永远为假的实现拦不住连点', !broken(1000, 1100))
}

/* --------------------------------------------------------------- 倒计时 */

{
  check('倒计时向上取整到秒', countdownSeconds(10_000, 8_100) === 2)
  check('倒计时不会小于 0', countdownSeconds(10_000, 12_000) === 0)
  check('已经到期返回 0', countdownSeconds(0, 0) === 0)

  /** 阴性对照：忘了取整会让界面显示 1.9 秒这种值 */
  const raw = (deadline: number, now: number) => (deadline - now) / 1000
  check('阴性对照：不取整会得到小数', raw(10_000, 8_100) === 1.9)
}

/* ------------------------------------------------------------ 自定义槽位 */

{
  const items: PromptListItem[] = [
    {
      key: 'extract.chat',
      group: 'extract',
      title: { zh: '对话抽取', en: 'Conversation extraction' },
      summary: { zh: '', en: '' },
      languages: ['zh', 'en'],
      editable: true,
      state: {
        zh: { source: 'project', versionId: 2, updatedAt: '', author: '' },
        en: { source: 'default', versionId: 0, updatedAt: '', author: '' },
      },
    },
    {
      key: 'dream.motive',
      group: 'dream',
      title: { zh: '动机', en: 'Motive' },
      summary: { zh: '', en: '' },
      languages: ['en'],
      editable: true,
      state: { en: { source: 'project', versionId: 5, updatedAt: '', author: '' } },
    },
  ]

  const slots = customizedSlots(items)
  check('只统计项目覆盖的槽位', slots.length === 2, JSON.stringify(slots))
  check(
    '槽位带 key 与语言',
    slots.some((s) => s.key === 'extract.chat' && s.lang === 'zh'),
  )
  check('全部默认时为空', customizedSlots([]).length === 0)
}

/* ---------------------------------------------------------------- 左侧目录 */

{
  const item = (key: string, group: string) =>
    ({ key, group, title: { zh: key }, languages: ['zh'], editable: true, state: {} }) as unknown as PromptListItem
  const items = [
    item('dream.motive', 'dream'),
    item('extract.chat', 'extract'),
    item('process.hallucination_filter', 'process'),
    item('extract.doc', 'extract'),
  ]
  const folders = promptFolders(items)
  check(
    '目录顺序：记忆抽取、处理、Dream',
    folders.map((f) => f.id).join(',') === 'extract,process,dream',
    folders.map((f) => f.id).join(','),
  )
  check('目录内保持接口顺序', folders[0].items.map((i) => i.key).join(',') === 'extract.chat,extract.doc')
  check('未知分组排在最后', promptFolders([item('x.a', 'other'), item('extract.chat', 'extract')])[1].id === 'other')

  const base = { toggled: {}, activeFolder: 'extract', searching: false }
  check('默认只展开记忆抽取', folderOpen('extract', base) && !folderOpen('dream', base))
  check('选中项所在目录展开', folderOpen('dream', { ...base, activeFolder: 'dream' }))
  check('手动收起优先于默认', !folderOpen('extract', { ...base, toggled: { extract: false } }))
  check('搜索时全部展开', folderOpen('dream', { ...base, toggled: { dream: false }, searching: true }))
}

/* ---------------------------------------------------------- 结构化结果表格 */

{
  const contract = { requiredKeys: ['memory list'], itemKeys: ['key', 'memory_type', 'value', 'tags'] }
  const output = {
    'memory list': [
      { key: 'a', memory_type: 'UserMemory', value: '第一条', tags: ['#x'] },
      { key: 'b', memory_type: 'UserMemory', value: '第二条', tags: [] },
    ],
    summary: '两条',
  }
  const table = structuredTable(output, contract)
  check('按输出契约找到数组', Boolean(table) && table!.rows.length === 2)
  check('列顺序按 itemKeys', table!.columns.join(',') === 'key,memory_type,value,tags', table!.columns.join(','))
  check('数组字段拼成可读文本', table!.rows[0].tags === '#x' && table!.rows[1].tags === '—')

  const extra = structuredTable({ 'memory list': [{ key: 'a', note: '额外字段' }] }, { requiredKeys: ['memory list'], itemKeys: ['key'] })
  check('契约之外的字段追加在后面', extra!.columns.join(',') === 'key,note', extra!.columns.join(','))

  const otherKey = structuredTable({ 'preference list': [{ preference: '结论先行' }] }, contract)
  check('契约键不存在时退到第一个数组', Boolean(otherKey) && otherKey!.rows.length === 1)

  check('没有数组时返回 null', structuredTable({ summary: '只有一段话' }, contract) === null)
  check('输出不是对象时返回 null', structuredTable('plain text', contract) === null && structuredTable(null, contract) === null)

  /** 阴性对照：直接取第一个值当表格的实现，会在 summary 是字符串时渲染成空表 */
  const naive = (o: Record<string, unknown>) => Object.values(o)[0] as unknown[]
  check('阴性对照：取第一个值当表格会拿到字符串', typeof naive({ summary: '两条', 'memory list': [] }) === 'string')

  check('列宽数量与列数一致', tableColumnWidths(4).length === 4 && tableColumnWidths(1)[0] === '100%')
  check(
    '列宽按百分比给出',
    tableColumnWidths(4).every((w) => w.endsWith('%')),
    tableColumnWidths(4).join(','),
  )
  check('1440 及以上默认并排', defaultCompareLayout(1440) === 'split' && defaultCompareLayout(1920) === 'split')
  check('低于 1440 默认上下', defaultCompareLayout(1439) === 'stacked' && defaultCompareLayout(1280) === 'stacked')

  const memoryCols = ['key', 'memory_type', 'value', 'tags', 'memory_form']
  const card = compactCardLayout(memoryCols, {
    key: 'preference.seat',
    memory_type: 'UserMemory',
    value: '用户偏好靠窗的座位',
    tags: '#travel',
    memory_form: 'state',
  })
  check('并排卡片把 value 当正文', card.primary === 'value', card.primary)
  check('小字只放 key/memory_type/tags', card.meta.join(',') === 'key,memory_type,tags', card.meta.join(','))
  check('memory_form 放进折叠详情', card.details.join(',') === 'memory_form', card.details.join(','))
  check('负数对照：并排卡片不会把 memory_form 挤进小字', !card.meta.includes('memory_form'))
  check(
    '没有 value 列时正文取最长字段',
    compactCardLayout(['preference', 'evidence'], { preference: '靠窗', evidence: '用户明确说更喜欢靠窗' }).primary === 'evidence',
  )

  const five = tableColumnWidths(5).map((w) => Number(w.replace('%', '')))
  check('五列表格总和约 100%', Math.abs(five.reduce((a, b) => a + b, 0) - 100) <= 1, five.join(','))
  check('五列表格里 value 最宽', five[2] === Math.max(...five), five.join(','))
}

/* ------------------------------------------------------- 布尔文案与版本号 */

{
  check('布尔为真时显示「是」', flagTextKey(true) === 'prompts.test.yes')
  check('布尔为假时显示「否」', flagTextKey(false) === 'prompts.test.no')

  /** 阴性对照：写反的实现会让「不会整段存原文」显示成「是」 */
  const inverted = (v: boolean) => (v ? 'prompts.test.no' : 'prompts.test.yes')
  check('阴性对照：写反的实现两处都对不上', inverted(true) === 'prompts.test.no' && inverted(false) === 'prompts.test.yes')
  check('正解与写反的实现相反', flagTextKey(true) !== inverted(true))

  check('有槽位序号时用序号', versionLabel(1, 9) === 'v1')
  check('没有序号时退回兜底序号', versionLabel(undefined, 3) === 'v3')
  check('两个都没有时不出「v0」', versionLabel(undefined, 0) === '')
}

/* ------------------------------------------------------------------ 汇总 */

console.log(`\n${passed}/${passed + failures.length} 通过`)
if (failures.length) {
  console.log('失败项：' + failures.join('、'))
  throw new Error(`工作台逻辑自检失败 ${failures.length} 项`)
}
