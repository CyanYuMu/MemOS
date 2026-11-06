/** mock 的提示词注册表与版本表。
 *
 *  这里模拟控制面 `prompts/*` 的返回形状（spec 第 7 节，camelCase），以及数据面
 *  `validate` / `test` 的行为。数据全部确定性生成，前端不依赖真实模型就能把
 *  工作台的每条路径点一遍。
 */

import type { Row } from './data.ts'

export type LangText = Record<string, string>

export interface PromptDef {
  key: string
  group: 'extract' | 'process' | 'dream'
  title: LangText
  summary: LangText
  languages: string[]
  syntax: 'dollar' | 'brace' | 'format'
  placeholders: { name: string; required: boolean; description: LangText }[]
  lockedSuffix: string | null
  inputKind: 'conversation' | 'text' | 'memories' | 'tool_trace' | 'qa_pair'
  inputSchema: Record<string, unknown>
  outputContract: {
    format: string
    requiredKeys: string[]
    itemKeys: string[]
    description: LangText
  }
  sampleInputs: { id: string; lang?: 'zh' | 'en'; title: LangText; input: unknown }[]
  editable: boolean
  /** 内置默认值，按语言。`detail` 接口把它拆成 `defaults` 下发，不混进注册项 */
  defaults: Record<string, string>
}

/* ------------------------------------------------------------ 输入 schema */

const CONVERSATION_SCHEMA = {
  type: 'object',
  required: ['messages'],
  properties: {
    messages: {
      type: 'array',
      items: {
        type: 'object',
        required: ['role', 'content'],
        properties: { role: { enum: ['user', 'assistant', 'system'] }, content: { type: 'string' } },
      },
    },
  },
}

const TEXT_SCHEMA = {
  type: 'object',
  required: ['text'],
  properties: { text: { type: 'string' } },
}

const MEMORIES_SCHEMA = {
  type: 'object',
  required: ['memories'],
  properties: {
    memories: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'memory'],
        properties: { id: { type: 'string' }, memory: { type: 'string' } },
      },
    },
  },
}

const textOf = (zh: string, en: string): LangText => ({ zh, en })

const placeholder = (name: string, required: boolean, zh: string, en: string) => ({
  name,
  required,
  description: textOf(zh, en),
})

const conversationSample = (id: string, locale: 'zh' | 'en') => ({
  id,
  lang: locale,
  title:
    locale === 'zh'
      ? { zh: '整理线上问题复盘', en: 'Turn an incident into a review memo' }
      : { zh: '回答偏好与结论', en: 'Answer preference and conclusion first' },
  input: {
    messages:
      locale === 'zh'
        ? [
            { role: 'user', content: '帮我把这次线上问题整理成复盘材料，结论先行，给出可量化的数字。' },
            { role: 'assistant', content: '好的，我会先给结论，再列出时间线和影响面。' },
            { role: 'user', content: '另外以后回答都用简体中文，代码标识符保留原文。' },
          ]
        : [
            { role: 'user', content: 'Turn this incident into a review memo. Lead with the conclusion and give numbers.' },
            { role: 'assistant', content: 'Sure. I will start with the conclusion, then the timeline and impact.' },
            { role: 'user', content: 'Also reply in English from now on, but keep code identifiers as-is.' },
          ],
  },
})

/* --------------------------------------------------------------- 注册表 */

export const PROMPTS: PromptDef[] = [
  {
    key: 'extract.chat',
    group: 'extract',
    title: textOf('对话抽取', 'Conversation extraction'),
    summary: textOf('把对话整理成记忆', 'Turns a conversation into memories'),
    languages: ['zh', 'en'],
    syntax: 'dollar',
    placeholders: [placeholder('conversation', true, '对话全文，由系统按消息角色拼接', 'The whole conversation, joined by role')],
    lockedSuffix: null,
    inputKind: 'conversation',
    inputSchema: CONVERSATION_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['memory list'],
      itemKeys: ['key', 'memory_type', 'value', 'tags'],
      description: textOf(
        '返回 memory list 数组，每项含 key、memory_type、value、tags',
        'Return a memory list array with key, memory_type, value and tags',
      ),
    },
    sampleInputs: [conversationSample('s1', 'zh'), conversationSample('s2', 'en')],
    editable: true,
    defaults: {
      zh: [
        '你是一个记忆抽取器。请从下面的对话中抽取值得长期保存的记忆。',
        '',
        '对话内容：',
        '${conversation}',
        '',
        '只保留跨任务、跨会话仍然成立的信息；一次性的任务流水与个案结论不要写入。',
        '每条记忆给出 key、memory_type、value 与 tags 四个字段，放在 memory list 里。',
      ].join('\n'),
      en: [
        'You extract long-lived memories from a conversation.',
        '',
        'Conversation:',
        '${conversation}',
        '',
        'Keep only what stays true across tasks and sessions; skip one-off steps and single-case conclusions.',
        'Return a memory list whose items carry key, memory_type, value and tags.',
      ].join('\n'),
    },
  },
  {
    key: 'extract.doc',
    group: 'extract',
    title: textOf('文档抽取', 'Document extraction'),
    summary: textOf('把文档与通用文本整理成记忆', 'Turns documents and plain text into memories'),
    languages: ['zh', 'en'],
    syntax: 'brace',
    placeholders: [placeholder('document', true, '文档正文', 'The document body')],
    lockedSuffix: null,
    inputKind: 'text',
    inputSchema: TEXT_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['memory list'],
      itemKeys: ['key', 'memory_type', 'value', 'tags'],
      description: textOf('返回 memory list 数组', 'Return a memory list array'),
    },
    sampleInputs: [
      {
        id: 's1',
        title: textOf('产品手册节选', 'Excerpt from a product manual'),
        input: { text: '记忆服务按项目隔离数据；每个项目可以单独配置抽取提示词。调用日志保留 14 天。' },
      },
    ],
    editable: true,
    defaults: {
      zh: [
        '你从下面的文档里抽取可以复用的记忆。',
        '',
        '文档：',
        '{document}',
        '',
        '返回 memory list，每项含 key、memory_type、value、tags。',
      ].join('\n'),
      en: [
        'Extract reusable memories from the document below.',
        '',
        'Document:',
        '{document}',
        '',
        'Return a memory list with key, memory_type, value and tags.',
      ].join('\n'),
    },
  },
  {
    key: 'extract.preference_explicit',
    group: 'extract',
    title: textOf('显式偏好抽取', 'Explicit preference extraction'),
    summary: textOf('抽取用户明确说过的偏好', 'Extracts preferences the user stated outright'),
    languages: ['zh', 'en'],
    syntax: 'brace',
    placeholders: [placeholder('conversation', true, '对话全文', 'The whole conversation')],
    lockedSuffix: null,
    inputKind: 'conversation',
    inputSchema: CONVERSATION_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['preference list'],
      itemKeys: ['preference', 'evidence'],
      description: textOf(
        '返回 preference list 数组，每项含 preference 与 evidence',
        'Return a preference list with preference and evidence',
      ),
    },
    sampleInputs: [conversationSample('s1', 'zh')],
    editable: true,
    defaults: {
      zh: [
        '只抽取用户明确表达过、以后仍然适用的偏好。',
        '',
        '对话：',
        '{conversation}',
        '',
        '返回 preference list，每项含 preference 与 evidence。',
      ].join('\n'),
      en: [
        'Extract only preferences the user stated explicitly and that still apply later.',
        '',
        'Conversation:',
        '{conversation}',
        '',
        'Return a preference list with preference and evidence.',
      ].join('\n'),
    },
  },
  {
    key: 'extract.preference_implicit',
    group: 'extract',
    title: textOf('隐式偏好抽取', 'Implicit preference extraction'),
    summary: textOf('从行为与措辞里推断偏好', 'Infers preferences from behaviour and wording'),
    languages: ['zh', 'en'],
    syntax: 'brace',
    placeholders: [placeholder('conversation', true, '对话全文', 'The whole conversation')],
    lockedSuffix: null,
    inputKind: 'conversation',
    inputSchema: CONVERSATION_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['implicit preference list'],
      itemKeys: ['preference', 'evidence'],
      description: textOf(
        '返回 implicit preference list 数组，每项含 preference 与 evidence',
        'Return an implicit preference list with preference and evidence',
      ),
    },
    sampleInputs: [conversationSample('s2', 'zh')],
    editable: true,
    defaults: {
      zh: [
        '从用户的做法与措辞里推断没有明说的偏好，并给出依据。',
        '',
        '对话：',
        '{conversation}',
        '',
        '返回 implicit preference list，每项含 preference 与 evidence。',
      ].join('\n'),
      en: [
        'Infer preferences the user did not state outright, and cite the evidence.',
        '',
        'Conversation:',
        '{conversation}',
        '',
        'Return an implicit preference list with preference and evidence.',
      ].join('\n'),
    },
  },
  {
    key: 'extract.qa_pair',
    group: 'extract',
    title: textOf('问答对抽取', 'Q&A pair extraction'),
    summary: textOf('从一组问答里提炼可复用的记忆', 'Turns a Q&A pair into reusable memories'),
    languages: ['zh', 'en'],
    syntax: 'dollar',
    placeholders: [placeholder('qa_pair', true, '一组问答', 'One question and its answer')],
    lockedSuffix: null,
    inputKind: 'qa_pair',
    inputSchema: {
      type: 'object',
      required: ['qa_pair'],
      properties: { qa_pair: { type: 'string' } },
    },
    outputContract: {
      format: 'json',
      requiredKeys: ['memory list'],
      itemKeys: ['key', 'memory_type', 'value', 'tags'],
      description: textOf('返回 memory list 数组', 'Return a memory list array'),
    },
    sampleInputs: [
      {
        id: 's1',
        lang: 'zh',
        title: textOf('一次接入咨询', 'An integration question'),
        input: { qa_pair: '用户：每个项目可以单独配置抽取提示词吗？\n助手：可以，按项目与语言分别覆盖。' },
      },
      {
        id: 's2',
        lang: 'en',
        title: textOf('一次接入咨询', 'An integration question'),
        input: { qa_pair: 'User: Can each project override the extraction prompt?\nAssistant: Yes, per project and language.' },
      },
    ],
    editable: true,
    defaults: {
      zh: [
        '从下面这组问答里抽取可以复用的记忆。',
        '',
        '问答：',
        '${qa_pair}',
        '',
        '返回 memory list，每项含 key、memory_type、value、tags。',
      ].join('\n'),
      en: [
        'Extract reusable memories from the Q&A pair below.',
        '',
        'Q&A:',
        '${qa_pair}',
        '',
        'Return a memory list with key, memory_type, value and tags.',
      ].join('\n'),
    },
  },
  {
    key: 'extract.event',
    group: 'extract',
    title: textOf('事件抽取', 'Event extraction'),
    summary: textOf('把对话里发生过的事整理成事件', 'Turns what happened in a conversation into events'),
    languages: ['en'],
    syntax: 'format',
    placeholders: [placeholder('conversation', true, '对话全文', 'The whole conversation')],
    lockedSuffix: null,
    inputKind: 'conversation',
    inputSchema: CONVERSATION_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['event list'],
      itemKeys: ['name', 'time', 'participants'],
      description: textOf('返回 event list 数组', 'Return an event list array'),
    },
    sampleInputs: [conversationSample('s1', 'en')],
    editable: true,
    defaults: {
      en: [
        'You extract events from a conversation.',
        '',
        'Conversation:',
        '{conversation}',
        '',
        'Write one item per event with name, time and participants, grouped under an event list.',
      ].join('\n'),
    },
  },
  {
    key: 'extract.tool_trace',
    group: 'extract',
    title: textOf('工具轨迹抽取', 'Tool trace extraction'),
    summary: textOf('从工具调用记录里抽出可复用的做法', 'Pulls reusable practice out of tool calls'),
    languages: ['en'],
    syntax: 'brace',
    placeholders: [placeholder('tool_trace', true, '工具调用记录', 'The tool call trace')],
    lockedSuffix: null,
    inputKind: 'tool_trace',
    inputSchema: TEXT_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['trace list'],
      itemKeys: ['tool', 'purpose'],
      description: textOf('返回 trace list 数组', 'Return a trace list array'),
    },
    sampleInputs: [
      {
        id: 's1',
        title: textOf('一次排障的命令序列', 'A debugging command sequence'),
        input: { text: '1. curl /healthz  2. kubectl logs -f deploy/api  3. 查看最近一次发布记录' },
      },
    ],
    editable: true,
    defaults: {
      en: [
        'Summarize the reusable pattern in the tool trace below.',
        '',
        'Trace:',
        '{tool_trace}',
        '',
        'Return a trace list with tool and purpose.',
      ].join('\n'),
    },
  },
  {
    key: 'process.hallucination_filter',
    group: 'process',
    title: textOf('幻觉过滤', 'Hallucination filter'),
    summary: textOf('丢掉没有原文依据的记忆', 'Drops memories with no evidence in the source'),
    languages: ['en'],
    syntax: 'format',
    placeholders: [placeholder('memories', true, '待检查的记忆列表', 'Memories to check')],
    lockedSuffix: null,
    inputKind: 'memories',
    inputSchema: MEMORIES_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['memory list'],
      itemKeys: ['key', 'value', 'dropped'],
      description: textOf('返回 memory list 数组，被丢掉的记在 dropped 里', 'Return a memory list, dropped items noted under dropped'),
    },
    sampleInputs: [
      {
        id: 's1',
        title: textOf('一条没有依据的记忆', 'A memory without evidence'),
        input: {
          memories: [
            { id: 'm1', memory: '用户提到团队使用 Go 与 Python' },
            { id: 'm2', memory: '用户明年会搬到上海' },
          ],
        },
      },
    ],
    editable: true,
    defaults: {
      en: [
        'Drop memories that the source text does not support.',
        '',
        'Memories:',
        '{memories}',
        '',
        'Return a memory list; note what you dropped under dropped.',
      ].join('\n'),
    },
  },
  {
    key: 'dream.motive',
    group: 'dream',
    title: textOf('Dream 动机', 'Dream motive'),
    summary: textOf('判断哪些记忆值得自动整理', 'Decides which memories are worth reorganizing'),
    languages: ['en'],
    syntax: 'format',
    placeholders: [placeholder('memories', true, '现有记忆列表', 'Existing memories')],
    lockedSuffix: 'Return strict JSON with a motive list and nothing else.',
    inputKind: 'memories',
    inputSchema: MEMORIES_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['motive list'],
      itemKeys: ['key', 'motive'],
      description: textOf('返回 motive list 数组，每项含 key 与 motive', 'Return a motive list with key and motive'),
    },
    sampleInputs: [
      {
        id: 's1',
        title: textOf('一组等待整理的记忆', 'A set of memories to reorganize'),
        input: {
          memories: [
            { id: 'm1', memory: '用户偏好结论先行' },
            { id: 'm2', memory: '用户偏好给出可量化的数字' },
            { id: 'm3', memory: '用户使用 Python 与 Go' },
            { id: 'm4', memory: '团队约定提交信息使用中文' },
          ],
        },
      },
    ],
    editable: true,
    defaults: {
      en: [
        'Decide which memories deserve a reorganization round.',
        '',
        'Memories:',
        '{memories}',
        '',
        'Return a motive list; each item carries key and motive.',
      ].join('\n'),
    },
  },
  {
    key: 'dream.reasoning',
    group: 'dream',
    title: textOf('Dream 推理', 'Dream reasoning'),
    summary: textOf('把整理目标拆成具体动作', 'Turns a motive into concrete steps'),
    languages: ['en'],
    syntax: 'format',
    placeholders: [
      placeholder('memories', true, '现有记忆列表', 'Existing memories'),
      placeholder('motive', true, '上一步给出的整理动机', 'The motive from the previous step'),
    ],
    lockedSuffix: null,
    inputKind: 'memories',
    inputSchema: MEMORIES_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['reasoning list'],
      itemKeys: ['step', 'reason'],
      description: textOf('返回 reasoning list 数组', 'Return a reasoning list array'),
    },
    sampleInputs: [
      {
        id: 's1',
        title: textOf('合并相近偏好', 'Merge similar preferences'),
        input: {
          memories: [
            { id: 'm1', memory: '用户偏好结论先行' },
            { id: 'm2', memory: '用户偏好给出可量化的数字' },
          ],
          motive: '两条偏好可以合成一条。',
        },
      },
    ],
    editable: true,
    defaults: {
      en: [
        'Turn the motive into concrete steps.',
        '',
        'Memories:',
        '{memories}',
        '',
        'Motive:',
        '{motive}',
        '',
        'Return a reasoning list; each item carries step and reason.',
      ].join('\n'),
    },
  },
  {
    key: 'dream.context_binding',
    group: 'dream',
    title: textOf('Dream 上下文绑定', 'Dream context binding'),
    summary: textOf('把记忆挂到对应的会话或任务上', 'Binds memories to their session or task'),
    languages: ['en'],
    syntax: 'format',
    placeholders: [placeholder('memories', true, '现有记忆列表', 'Existing memories')],
    lockedSuffix: null,
    inputKind: 'memories',
    inputSchema: MEMORIES_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['binding list'],
      itemKeys: ['key', 'context'],
      description: textOf('返回 binding list 数组', 'Return a binding list array'),
    },
    sampleInputs: [
      {
        id: 's1',
        title: textOf('两条同会话的记忆', 'Two memories from one session'),
        input: {
          memories: [
            { id: 'm1', memory: '任务：梳理调用日志' },
            { id: 'm2', memory: '结论：按租户汇总后差异来自统计口径' },
          ],
        },
      },
    ],
    editable: true,
    defaults: {
      en: [
        'Bind each memory to the context it came from.',
        '',
        'Memories:',
        '{memories}',
        '',
        'Return a binding list; each item carries key and context.',
      ].join('\n'),
    },
  },
  {
    key: 'dream.context_summary',
    group: 'dream',
    title: textOf('Dream 上下文摘要', 'Dream context summary'),
    summary: textOf('为一段上下文写一句摘要', 'Writes one summary line per context'),
    languages: ['en'],
    syntax: 'format',
    placeholders: [placeholder('memories', true, '同一上下文的记忆列表', 'Memories of one context')],
    lockedSuffix: null,
    inputKind: 'memories',
    inputSchema: MEMORIES_SCHEMA,
    outputContract: {
      format: 'json',
      requiredKeys: ['summary list'],
      itemKeys: ['context', 'summary'],
      description: textOf('返回 summary list 数组', 'Return a summary list array'),
    },
    sampleInputs: [
      {
        id: 's1',
        title: textOf('一次复盘会话', 'A review session'),
        input: {
          memories: [
            { id: 'm1', memory: '任务：整理线上问题复盘' },
            { id: 'm2', memory: '结论：根因是连接池耗尽' },
          ],
        },
      },
    ],
    editable: true,
    defaults: {
      en: [
        'Write one summary line per context.',
        '',
        'Memories:',
        '{memories}',
        '',
        'Return a summary list; each item carries context and summary.',
      ].join('\n'),
    },
  },
]

// 老样例可能没写 lang：单语言提示词就取那一种，多语言的默认按中文标注
for (const def of PROMPTS) {
  for (const sample of def.sampleInputs) {
    if (!sample.lang) sample.lang = (def.languages[0] === 'en' && def.languages.length === 1 ? 'en' : 'zh') as 'zh' | 'en'
  }
}

export const findPrompt = (key: string) => PROMPTS.find((p) => p.key === key)

/* --------------------------------------------------------------- 版本表 */

/** 预置历史：默认项目（`proj_default`）的对话抽取中文有两版覆盖，其余项目全用默认值。
 *  这样项目隔离、版本历史、diff 打开就有内容，不用先造一遍数据。 */
export function seedPromptVersions(): Row[] {
  const row = (
    id: string,
    versionId: number,
    promptKey: string,
    lang: string,
    body: string,
    note: string,
    createdAt: string,
    parentVersionId: number | null,
    kind = 'save',
    projectId = 'proj_default',
  ): Row => ({ id, versionId, projectId, promptKey, lang, kind, body, note, author: 'demo-user', parentVersionId, createdAt })

  const chatV1 = [
    '你是一个记忆抽取器。请从下面的对话中抽取值得长期保存的记忆。',
    '',
    '对话内容：',
    '${conversation}',
    '',
    '只保留跨任务、跨会话仍然成立的信息。',
    '每条记忆给出 key、memory_type、value 与 tags 四个字段，放在 memory list 里。',
  ].join('\n')

  const chatV2 = [chatV1, '', '另外：每条记忆的 tags 里加上 #team-review，方便复盘时检索。'].join('\n')

  return [
    row('pv_001', 101, 'extract.chat', 'zh', chatV1, '第一版：去掉任务流水', '2026-10-20 10:12:00', null),
    row('pv_002', 102, 'extract.chat', 'zh', chatV2, '加上复盘标签', '2026-10-22 15:40:00', 101),
    row(
      'pv_003',
      103,
      'extract.preference_explicit',
      'zh',
      [
        '只抽取用户明确说过的偏好，并附上原话作为依据。',
        '',
        '对话：',
        '{conversation}',
        '',
        '返回 preference list，每项含 preference 与 evidence。',
      ].join('\n'),
      '补上 evidence',
      '2026-10-23 09:05:00',
      null,
    ),
    row(
      'pv_004',
      104,
      'process.hallucination_filter',
      'en',
      [
        'Drop memories that the source text does not support.',
        '',
        'Memories:',
        '{memories}',
        '',
        'Return a memory list; note what you dropped under dropped.',
      ].join('\n'),
      'Keep dropped',
      '2026-10-24 18:20:00',
      null,
    ),
  ]
}

/* ------------------------------------------------------------ 槽位与状态 */

const MAX_BODY = 20000
const MAX_NOTE = 200

export interface SlotView {
  source: 'default' | 'project'
  versionId: number
  /** 槽位内序号，从 1 开始；默认值没有版本号，用 0 */
  versionNo: number
  body: string
  /** 默认槽位没有时间与作者，真实接口给 null；mock 保持一致 */
  updatedAt: string | null
  author: string | null
}

/** 某个历史版本在它自己槽位里的序号（按 versionId 升序，从 1 开始） */
export function versionNoOf(versions: Row[], projectId: string, def: PromptDef, lang: string, versionId: number): number {
  return versions.filter((v) => v.projectId === projectId && v.promptKey === def.key && v.lang === lang && Number(v.versionId) <= versionId)
    .length
}

/** 某槽位当前生效值：取该项目、该 key、该语言里 versionId 最大的一行 */
export function readSlot(versions: Row[], projectId: string, def: PromptDef, lang: string): SlotView {
  let best: Row | null = null
  for (const v of versions) {
    if (v.projectId !== projectId || v.promptKey !== def.key || v.lang !== lang) continue
    if (!best || Number(v.versionId) > Number(best.versionId)) best = v
  }
  const fallback = def.defaults[lang] ?? ''
  if (!best || best.kind === 'reset') {
    return { source: 'default', versionId: 0, versionNo: 0, body: fallback, updatedAt: null, author: null }
  }
  return {
    source: 'project',
    versionId: Number(best.versionId),
    versionNo: versionNoOf(versions, projectId, def, lang, Number(best.versionId)),
    body: String(best.body),
    updatedAt: String(best.createdAt ?? ''),
    author: String(best.author ?? ''),
  }
}

export function stateOf(versions: Row[], projectId: string, def: PromptDef) {
  const state: Record<
    string,
    { source: 'default' | 'project'; versionId: number; versionNo: number; updatedAt: string | null; author: string | null }
  > = {}
  for (const lang of def.languages) {
    const { source, versionId, versionNo, updatedAt, author } = readSlot(versions, projectId, def, lang)
    state[lang] = { source, versionId, versionNo, updatedAt, author }
  }
  return state
}

/* ---------------------------------------------------------------- 校验 */

export interface Issue {
  code: string
  message: string
}

export interface Validation {
  ok: boolean
  errors: Issue[]
  warnings: Issue[]
}

/** `format` 语法的花括号扫描：返回字段名与是否配对。字面花括号写成 `{{` / `}}`。 */
function scanFormat(body: string): { fields: string[]; braceError: boolean } {
  const fields: string[] = []
  let i = 0
  while (i < body.length) {
    const ch = body[i]
    if (ch === '{') {
      if (body[i + 1] === '{') {
        i += 2
        continue
      }
      let field = ''
      i++
      let closed = false
      while (i < body.length) {
        if (body[i] === '}') {
          closed = true
          break
        }
        if (body[i] === '{') return { fields, braceError: true }
        field += body[i]
        i++
      }
      if (!closed) return { fields, braceError: true }
      fields.push(field.split(/[:!]/)[0])
      i++
    } else if (ch === '}') {
      if (body[i + 1] === '}') {
        i += 2
        continue
      }
      return { fields, braceError: true }
    } else {
      i++
    }
  }
  return { fields, braceError: false }
}

export function validatePrompt(def: PromptDef, lang: string, body: string): Validation {
  const errors: Issue[] = []
  const warnings: Issue[] = []

  if (!def.languages.includes(lang)) {
    return { ok: false, errors: [{ code: 'invalid_lang', message: `这条提示词没有「${lang}」版本。` }], warnings }
  }
  if (!body.trim()) errors.push({ code: 'empty', message: '内容不能为空，至少保留一句说明。' })
  if (body.length > MAX_BODY)
    errors.push({ code: 'too_long', message: `内容超过 ${MAX_BODY} 字符（当前 ${body.length}），请删减后再保存。` })

  const names = def.placeholders.map((p) => p.name)
  if (def.syntax === 'dollar') {
    const used = [...body.matchAll(/\$\{([^}]*)\}/g)].map((m) => m[1])
    for (const p of def.placeholders) {
      if (p.required && !used.includes(p.name)) {
        errors.push({ code: 'missing_placeholder', message: `缺少必填占位符 \${${p.name}}，试跑时取不到内容。` })
      }
    }
    for (const u of used) {
      if (!names.includes(u))
        errors.push({
          code: 'unknown_placeholder',
          message: `占位符 \${${u}} 不在允许的列表里：${names.map((n) => `\${${n}}`).join('、')}。`,
        })
    }
  } else if (def.syntax === 'brace') {
    // brace 只认「占位符以 {name} 逐字出现」，正文里其他花括号（JSON 示例等）原样保留
    const used = [...body.matchAll(/\{([^{}]+)\}/g)].map((m) => m[1])
    for (const p of def.placeholders) {
      if (p.required && !used.includes(p.name))
        errors.push({ code: 'missing_placeholder', message: `缺少必填占位符 {${p.name}}，试跑时取不到内容。` })
    }
  } else {
    const { fields, braceError } = scanFormat(body)
    if (braceError) errors.push({ code: 'brace', message: '花括号没有成对，字面花括号要写成 {{ 和 }}。' })
    for (const f of fields) {
      if (/^\d+$/.test(f) || f === '') errors.push({ code: 'positional', message: '不能用位置参数，请写占位符名字。' })
      else if (!names.includes(f))
        errors.push({ code: 'unknown_placeholder', message: `占位符 {${f}} 不在允许的列表里：${names.map((n) => `{${n}}`).join('、')}。` })
    }
    for (const p of def.placeholders) {
      if (p.required && !fields.includes(p.name))
        errors.push({ code: 'missing_placeholder', message: `缺少必填占位符 {${p.name}}，试跑时取不到内容。` })
    }
  }

  if (!errors.length) {
    for (const key of def.outputContract.requiredKeys) {
      if (!body.includes(key)) {
        warnings.push({ code: 'contract_keyword', message: `正文里没有出现输出结构要求的「${key}」，试跑可能解析不出结果。` })
      }
    }
  }

  return { ok: errors.length === 0, errors, warnings }
}

/* ---------------------------------------------------------------- 试跑 */

/** 模拟一次模型调用：输出是确定性的，规则写在这里而不是页面里。
 *
 *  - 正文里带输出结构关键字（例如 `memory list`）→ 返回合法 JSON；
 *  - 不带 → 返回一段散文，解析失败，等同于真实抽取里「整段原文被存成 UserMemory」；
 *  - 输入里带 `__llm_error__` → 模拟模型调用失败，用来验证失败态界面。
 */
function simulateOutput(def: PromptDef, body: string, input: unknown): string | null {
  const serialized = JSON.stringify(input ?? {})
  if (serialized.includes('__llm_error__')) return null

  const text = (() => {
    const obj = input as {
      messages?: { content?: string }[]
      text?: string
      memories?: { id?: string; memory?: string }[]
      qa_pair?: string
      question?: string
      answer?: string
      motive?: string
    } | null
    if (obj?.messages?.length) return obj.messages.map((m) => m.content ?? '').join('\n')
    if (obj?.text) return obj.text
    if (obj?.memories?.length) return obj.memories.map((m) => String(m?.memory ?? '')).join('\n')
    if (obj?.qa_pair) return obj.qa_pair
    if (obj?.question || obj?.answer) return [obj.question ?? '', obj.answer ?? ''].join('\n')
    return ''
  })()

  const keyword = def.outputContract.requiredKeys[0]
  // 正文里没写输出结构 → 模型只回一段散文。真实抽取会把这段原文整段存成 UserMemory
  if (!keyword || !body.includes(keyword)) return text

  const fact =
    text
      .split(/[\n。.!?]/)
      .map((s) => s.trim())
      .filter(Boolean)[0] ?? '（无内容）'

  if (def.group === 'extract') {
    if (def.key.startsWith('extract.preference')) {
      return JSON.stringify({ [keyword]: [{ preference: fact, evidence: fact }] }, null, 2)
    }
    if (def.key === 'extract.event') {
      return JSON.stringify({ 'event list': [{ name: fact, time: '2026-10-29 10:00:00', participants: ['user', 'assistant'] }] }, null, 2)
    }
    if (def.key === 'extract.tool_trace') {
      return JSON.stringify({ 'trace list': [{ tool: 'curl', purpose: fact }] }, null, 2)
    }
    return JSON.stringify(
      {
        'memory list': [{ key: 'preference.style', memory_type: 'UserMemory', value: fact, tags: ['#demo'], memory_form: 'state' }],
        summary: '从对话里抽取到一条偏好类记忆',
      },
      null,
      2,
    )
  }

  if (def.group === 'process') {
    return JSON.stringify(
      { 'memory list': [{ key: 'preference.style', value: fact, merged_from: ['a', 'b'], memory_form: 'state' }] },
      null,
      2,
    )
  }

  const payload: Record<string, unknown> = {
    [keyword]: [{ key: 'dream.context', [keyword.split(' ')[0]]: fact }],
  }
  return JSON.stringify(payload, null, 2)
}

export interface RunView {
  label: 'draft' | 'default'
  ok: boolean
  parseOk: boolean
  fallbackTriggered: boolean
  contract: { ok: boolean; missing: string[] }
  output: unknown
  rawOutput: string
  truncated: boolean
  latencyMs: number
  model: string
  usage: { promptTokens: number; completionTokens: number }
  /** 这次实际用了哪些模板；draft / project / default 三选一 */
  usedSlots: { key: string; lang: string; source: 'draft' | 'project' | 'default'; versionId?: number; versionNo?: number }[]
  warnings: Issue[]
  error?: { code: string; message: string }
}

export interface RunContext {
  key: string
  lang: string
  source: 'draft' | 'project' | 'default'
  versionId?: number
  versionNo?: number
}

export function simulateRun(
  def: PromptDef,
  body: string,
  input: unknown,
  label: 'draft' | 'default',
  ctx: RunContext = { key: def.key, lang: def.languages[0] ?? 'zh', source: label === 'default' ? 'default' : 'draft' },
): RunView {
  const serialized = JSON.stringify(input ?? {})
  // 场景开关：只让其中一侧失败，用来验证并排时另一侧仍照常显示
  const failThisSide = serialized.includes(`__llm_error_${label}__`)
  const raw = failThisSide ? null : simulateOutput(def, body, input)
  // 场景开关：输入里带这个标记时，模拟「这次没有用草稿」的告警
  const draftIgnored = serialized.includes('__draft_not_used__')
  // 真实响应只有 key / lang / source / versionId；默认与草稿的 versionId 也是 0，要带上
  const usedSlots = [
    {
      key: ctx.key,
      lang: ctx.lang,
      source: draftIgnored ? ('project' as const) : ctx.source,
      versionId: ctx.versionId ?? 0,
    },
  ]
  const warnings: Issue[] = draftIgnored ? [{ code: 'draft_not_used', message: '这次试跑没有使用草稿，结果来自当前生效版本。' }] : []
  const latencyMs = 240 + (body.length % 360)
  const promptTokens = 120 + (JSON.stringify(input ?? {}).length % 700)
  if (raw === null) {
    return {
      label,
      ok: false,
      parseOk: false,
      fallbackTriggered: false,
      contract: { ok: false, missing: def.outputContract.requiredKeys },
      output: null,
      rawOutput: '',
      truncated: false,
      latencyMs,
      model: 'DeepSeek-V4.1-Flash',
      usage: { promptTokens, completionTokens: 0 },
      usedSlots,
      warnings,
      error: { code: 'llm_error', message: '模型调用失败，请稍后重试。' },
    }
  }

  let parsed: unknown = null
  let parseOk = true
  try {
    parsed = JSON.parse(raw)
  } catch {
    parseOk = false
  }

  const obj = (parsed ?? {}) as Record<string, unknown>
  const missing = def.outputContract.requiredKeys.filter((k) => !(k in obj))
  const clipped = raw.length > MAX_BODY ? raw.slice(0, MAX_BODY) : raw

  return {
    label,
    ok: true,
    parseOk,
    fallbackTriggered: !parseOk,
    contract: { ok: parseOk && missing.length === 0, missing },
    output: parsed,
    rawOutput: clipped,
    truncated: raw.length > MAX_BODY,
    latencyMs,
    model: 'DeepSeek-V4.1-Flash',
    usage: { promptTokens, completionTokens: 80 + (raw.length % 240) },
    usedSlots,
    warnings,
  }
}

/* ------------------------------------------------------------ 请求体限额 */

export const LIMITS = {
  body: MAX_BODY,
  instruction: 2000,
  note: MAX_NOTE,
  messages: 30,
  inputChars: 12000,
  requestBytes: 256 * 1024,
} as const

/** 按 `input_kind` 校验试跑输入。返回一句面向用户的错误；合法时返回 null */
export function checkInput(def: PromptDef, input: unknown): string | null {
  const obj = (input ?? {}) as { messages?: unknown; text?: unknown; memories?: unknown; qa_pair?: unknown }
  if (def.inputKind === 'conversation') {
    if (!Array.isArray(obj.messages) || obj.messages.length === 0) return '请输入至少一条对话消息。'
    if (obj.messages.length > LIMITS.messages) return `对话最多 ${LIMITS.messages} 条消息，请删减后再试。`
    const chars = obj.messages.reduce((n: number, m) => n + String((m as { content?: unknown })?.content ?? '').length, 0)
    if (chars > LIMITS.inputChars) return `对话总长度超过 ${LIMITS.inputChars} 字符，请删减后再试。`
    return null
  }
  if (def.inputKind === 'text' || def.inputKind === 'tool_trace') {
    if (typeof obj.text !== 'string' || !obj.text.trim()) return '请输入要处理的文本。'
    if (obj.text.length > LIMITS.inputChars) return `输入超过 ${LIMITS.inputChars} 字符，请删减后再试。`
    return null
  }
  if (def.inputKind === 'memories') {
    if (!Array.isArray(obj.memories) || obj.memories.length === 0) return '请至少输入一条记忆。'
    const bad = obj.memories.find((m) => typeof m !== 'object' || m === null || typeof (m as { memory?: unknown }).memory !== 'string')
    if (bad !== undefined) return '每条记忆都要包含 id 与 memory 字段。'
    const chars = obj.memories.reduce((n: number, m) => n + String((m as { memory?: unknown }).memory ?? '').length, 0)
    if (chars > LIMITS.inputChars) return `记忆总长度超过 ${LIMITS.inputChars} 字符，请删减后再试。`
    return null
  }
  if (def.inputKind === 'qa_pair') {
    if (typeof obj.qa_pair !== 'string' || !obj.qa_pair.trim()) return '请输入一组问答。'
    if (obj.qa_pair.length > LIMITS.inputChars) return `问答总长度超过 ${LIMITS.inputChars} 字符，请删减后再试。`
    return null
  }
  return null
}

/* ---------------------------------------------------------- 简单令牌桶 */

/** 每个项目一个桶。mock 里的数值与控制面默认值一致：试跑突发 3、每分钟补 6。 */
export function makeBucket(burst: number, refillPerMinute: number) {
  let tokens = burst
  let last = Date.now()
  return (): number => {
    const now = Date.now()
    tokens = Math.min(burst, tokens + ((now - last) / 60000) * refillPerMinute)
    last = now
    if (tokens < 1) return Math.ceil(((1 - tokens) / refillPerMinute) * 60)
    tokens -= 1
    return 0
  }
}
