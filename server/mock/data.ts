/** mock 后端的数据集与固定事实。字段名按根目录 openapi.json 来。 */

import crypto from 'node:crypto'
import { seedPromptVersions } from './prompts.ts'

/* ---------------------------------------------------------------- 固定事实 */

/** 密钥前缀，经 /api/meta 下发 */
export const KEY_PREFIX = 'sk-'

/** 服务暴露的 MCP 工具。接入文档与示例命令据此生成，不写死在前端 */
export const TOOLS = [
  { name: 'add_message', title: '写入记忆' },
  { name: 'search_memory', title: '检索记忆' },
  { name: 'get_user_profile', title: '读取画像' },
  { name: 'delete_memory', title: '删除记忆' },
  { name: 'add_feedback', title: '提交反馈' },
]

/** 调用日志的事件类型。与前端 EVENT_TYPES 一致 */
export const EVENT_TYPES = [
  'addMessage',
  'searchMemory',
  'getMemory',
  'deleteMemory',
  'addFeedback',
  'manualAddMemory',
  'updateMemory',
  'bindProfileTemplate',
  'editProfile',
  'deleteProfile',
] as const

/** 记忆列表的 5 个标签，值就是接口的 memoryDetailType */
export const MEMORY_DETAIL_TYPES = ['text_mem', 'pref_mem', 'event_mem', 'tool_mem'] as const

export const SUBJECT_TYPES = ['user', 'agent', 'public'] as const

/* ------------------------------------------------------------------ 工具 */

/** 固定种子的伪随机，保证每次生成的历史数据一致，便于对账 */
export function makeRng(seed: number) {
  let s = seed >>> 0
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0
    return s / 0x100000000
  }
}

export function iso(d: Date) {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export const newSecret = () => `${KEY_PREFIX}${crypto.randomBytes(24).toString('hex')}`

export const newId = (prefix: string) => `${prefix}_${crypto.randomBytes(6).toString('hex')}`

/* ---------------------------------------------------------------- 数据集 */

/** 落盘文件的 schema 版本。字段名有改动就 +1，老文件会被丢弃重建 */
export const SCHEMA_VERSION = 3

export interface Row {
  id: string
  [k: string]: unknown
}

export interface Db {
  /** 资源名 → 该资源的行。带索引签名，路由才能按名字取表 */
  [collection: string]: Row[]

  projects: Row[]
  keys: Row[]
  logs: Row[]
  memories: Row[]
  knowledgeBases: Row[]
  templates: Row[]
  promptConfigs: Row[]
  /** 提示词版本表：追加写，槽位的当前值 = 同项目同 key 同语言里 versionId 最大的一行 */
  promptVersions: Row[]
}

const MEMORY_TEXTS = [
  '用户偏好使用简体中文回答，代码标识符保留原文',
  '本项目对外提供 HTTP 接口，端口与路径有固定约定',
  '任务：梳理某业务线的调用日志并按租户汇总',
  '用户是后端工程师，主要使用 Python 与 Go',
  '偏好：结论先行，给出可量化的数字，不要罗列过程',
  '任务：把某次线上问题的时间线整理成复盘材料',
  '团队约定：提交信息使用中文，变更需附影响面说明',
  '用户所在团队负责长期记忆服务的检索链路',
  '偏好：给出命令时用可直接粘贴的完整命令',
  '任务：核对两个数据源的统计范围差异并给出结论',
  '用户习惯在上午处理需要专注的分析类工作',
  '约定：对外材料不得出现内部实现细节与产品来源',
]

const EXTERNAL_USERS = ['demo-agent', 'demo-user', 'default']

/* ------------------------------------------------------------ 各表的种子 */

function seedProjects(): Row[] {
  return [
    {
      id: 'proj_default',
      name: '默认项目',
      description: '控制台自带项目',
      status: 'ENABLED',
      createTime: '2026-08-01 09:00:00',
      isMultiView: 0,
      role: 'owner',
      member: true,
    },
    {
      id: 'proj_alpha',
      name: 'demo-agent',
      description: '接入方 A 的项目',
      status: 'DISABLED',
      createTime: '2026-09-21 11:08:00',
      isMultiView: 1,
    },
    {
      id: 'proj_beta',
      name: 'demo-user',
      description: '内部自测项目',
      status: 'DISABLED',
      createTime: '2026-08-14 16:20:00',
      isMultiView: 0,
    },
  ]
}

function seedLogs(): Row[] {
  const rnd = makeRng(20261016)
  const now = Date.now()
  const out: Row[] = []
  for (let i = 0; i < 260; i++) {
    const t = new Date(now - Math.floor(rnd() * 14 * 24 * 3600 * 1000))
    const failed = rnd() < 0.06
    const eventType = EVENT_TYPES[Math.floor(rnd() * EVENT_TYPES.length)]
    const user = EXTERNAL_USERS[Math.floor(rnd() * EXTERNAL_USERS.length)]
    const ok = !failed
    out.push({
      id: `log_${(i + 1).toString().padStart(4, '0')}`,
      projectId: 'proj_default',
      createTime: iso(t),
      eventType,
      userIds: [user],
      agentIds: [user],
      status: ok ? 'SUCCESS' : 'FAILED',
      duration: Math.round(60 + rnd() * 2400),
      traceId: crypto.randomBytes(8).toString('hex'),
      userId: user,
      agentId: user,
      requestTokens: ok ? Math.round(80 + rnd() * 1200) : 0,
      responseTokens: ok ? Math.round(20 + rnd() * 400) : 0,
      requestCode: ok ? 200 : 500,
      responseCode: ok ? 200 : 500,
      // 下发的是 JSON 字符串，前端二次 parse
      requestBody: JSON.stringify(sampleRequestBody(eventType)),
      responseBody: JSON.stringify(ok ? { code: 0, data: {}, message: 'success' } : { code: 50000, data: null, message: '内部错误' }),
    })
  }
  return out.sort((a, b) => (String(a.createTime) < String(b.createTime) ? 1 : -1))
}

/** 按事件类型造一份像样的请求体，字段名照字段表 */
function sampleRequestBody(eventType: string): Record<string, unknown> {
  switch (eventType) {
    case 'addMessage':
      return {
        conversation_id: `conv_${crypto.randomBytes(4).toString('hex')}`,
        messages: [{ role: 'user', content: '帮我把这次的线上问题整理成复盘材料' }],
        async_mode: false,
        allow_memory_view: true,
      }
    case 'searchMemory':
      return {
        query: '用户的回答偏好是什么',
        conversation_id: `conv_${crypto.randomBytes(4).toString('hex')}`,
        include_memory_view: true,
        memory_limit_number: 10,
        includePreference: true,
        preferenceLimitNumber: 6,
      }
    case 'getMemory':
      return { page: 1, size: 20, filter: {}, include_memory_view: true }
    case 'deleteMemory':
      return { memory_ids: [newId('mem')] }
    case 'addFeedback':
      return { feedback_target: { conversation_id: `conv_${crypto.randomBytes(4).toString('hex')}` }, feedback_content: '回答很有帮助' }
    case 'manualAddMemory':
      return { content: '用户偏好结论先行的回答方式', memory_view: 'text_mem', algorithm_updatable: true }
    case 'updateMemory':
      return { memory_id: newId('mem'), content: '用户偏好结论先行、可量化的回答', algorithm_updatable: true }
    case 'bindProfileTemplate':
      return { bind_list: [{ profile_template_id: newId('tpl') }] }
    case 'editProfile':
      return { profile_template_id: newId('tpl'), metadata: {}, remove_fields: [] }
    case 'deleteProfile':
      return { profile_template_id: newId('tpl') }
    default:
      return {}
  }
}

function seedMemories(): Row[] {
  const rnd = makeRng(20261017)
  const now = Date.now()
  const out: Row[] = []
  for (let i = 0; i < 180; i++) {
    const t = new Date(now - Math.floor(rnd() * 30 * 24 * 3600 * 1000))
    const type = MEMORY_DETAIL_TYPES[Math.floor(rnd() * MEMORY_DETAIL_TYPES.length)]
    const row: Row = {
      id: newId('mem'),
      memoryId: newId('mem'),
      projectId: 'proj_default',
      memoryDetailType: type,
      memoryContent: MEMORY_TEXTS[Math.floor(rnd() * MEMORY_TEXTS.length)],
      subjectType: SUBJECT_TYPES[Math.floor(rnd() * SUBJECT_TYPES.length)],
      externalUserId: EXTERNAL_USERS[Math.floor(rnd() * EXTERNAL_USERS.length)],
      createTime: iso(t),
      updatedAt: iso(t),
    }
    if (type === 'text_mem') row.profileTemplateId = newId('tpl')
    out.push(row)
  }
  return out.sort((a, b) => (String(a.createTime) < String(b.createTime) ? 1 : -1))
}

function seedKnowledgeBases(): Row[] {
  return [
    {
      id: 'kb_1',
      kbId: 'kb_1',
      knowledgebaseName: '产品手册',
      knowledgebaseDescription: '面向使用者的功能说明',
      projectIds: ['proj_default'],
      createTime: '2026-10-14 09:12:03',
    },
    {
      id: 'kb_2',
      kbId: 'kb_2',
      knowledgebaseName: '运维手册',
      knowledgebaseDescription: '部署、排障与巡检',
      projectIds: ['proj_default', 'proj_beta'],
      createTime: '2026-10-11 16:40:22',
    },
    {
      id: 'kb_3',
      kbId: 'kb_3',
      knowledgebaseName: '接入示例',
      knowledgebaseDescription: '常见接法的最小示例',
      projectIds: ['proj_default'],
      createTime: '2026-10-08 11:05:47',
    },
  ]
}

/** 画像模板。结构是「分组 → 字段 → { value, algorithm_updatable }」 */
function seedTemplates(): Row[] {
  return [
    {
      id: 'tpl_1',
      templateId: 'tpl_1',
      templateName: '通用用户画像',
      bindingCount: 3,
      updatedAt: '2026-10-13 10:20:00',
      projectId: 'proj_default',
      基础信息: {
        姓名: { value: '', algorithm_updatable: false },
        职业: { value: '', algorithm_updatable: true },
        居住地: { value: '', algorithm_updatable: true },
      },
      性格标签: {
        最大优点: { value: '', algorithm_updatable: true },
        最大弱点: { value: '', algorithm_updatable: true },
      },
      互动偏好: {
        安慰方式: { value: '', algorithm_updatable: true },
        决策偏好: { value: '', algorithm_updatable: true },
        雷点: { value: '', algorithm_updatable: true },
      },
    },
    {
      id: 'tpl_2',
      templateId: 'tpl_2',
      templateName: '工程团队画像',
      bindingCount: 1,
      updatedAt: '2026-10-09 15:02:31',
      projectId: 'proj_default',
      技术栈: {
        主力语言: { value: '', algorithm_updatable: true },
        部署环境: { value: '', algorithm_updatable: true },
      },
      协作约定: {
        提交规范: { value: '', algorithm_updatable: true },
        评审要求: { value: '', algorithm_updatable: true },
      },
    },
  ]
}

/** 自定义抽取的 Prompt 配置，5 个 prompt-config 接口都围绕它 */
function seedPromptConfigs(): Row[] {
  return [
    {
      id: 'pc_default',
      projectId: 'proj_default',
      extractionTypes: ['text_mem', 'pref_mem', 'event_mem'],
      prompt: [
        '你是一个记忆抽取器。从下面的对话里抽取值得长期保留的信息。',
        '',
        '只保留跨任务、跨会话仍然成立的内容；一次性的任务流水与个案结论不要写入。',
      ].join('\n'),
      updatedAt: '2026-10-15 08:30:12',
    },
  ]
}

/** 初始数据集。加一个资源就在这里加一张表，路由那边不用动 */
export const COLLECTIONS: Db = {
  projects: seedProjects(),
  keys: [
    {
      id: 'key_default',
      projectId: 'proj_default',
      keyName: '默认密钥',
      apiKey: newSecret(),
      createTime: iso(new Date()),
    },
  ],
  logs: seedLogs(),
  memories: seedMemories(),
  knowledgeBases: seedKnowledgeBases(),
  templates: seedTemplates(),
  promptConfigs: seedPromptConfigs(),
  promptVersions: seedPromptVersions(),
}
