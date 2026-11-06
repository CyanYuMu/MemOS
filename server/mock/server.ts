/** 独立跑的 mock 后端：真 HTTP 服务，实现根目录 openapi.json 里那套接口。
 *
 *  跑法: pnpm mock          （默认 5280）
 *        pnpm dev:all       （同时起 mock 与前端）
 */

import http from 'node:http'
import { COLLECTIONS, KEY_PREFIX, TOOLS, iso, newId, newSecret, type Row } from './data.ts'
import {
  LIMITS,
  PROMPTS,
  checkInput,
  findPrompt,
  makeBucket,
  readSlot,
  simulateRun,
  stateOf,
  versionNoOf,
  validatePrompt,
  type PromptDef,
} from './prompts.ts'
import { load, save } from './store.ts'
import type { Memory } from '../../shared/contract.ts'

const PORT = Number(process.env.MOCK_PORT || 5280)
const DB_FILE = process.env.MOCK_DB || '.data/mock-db.json'

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type,Authorization',
}

function json(res: http.ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...CORS })
  res.end(JSON.stringify(body))
}

/** 成功响应：code === 0 时前端取 data */
const ok = (res: http.ServerResponse, data: unknown) => json(res, 200, { code: 0, data, message: 'success' })

/** 202：发消息是“已受理”，回复走同一会话的 SSE */
const accepted = (res: http.ServerResponse, data: unknown) => json(res, 202, { code: 0, data, message: 'success' })

/** 失败响应：错误码见 openapi.json 里那张表。
 *  提示词接口按 spec 用真实 HTTP 状态码（400/404/409/413/429），其余接口保持 200 + code。 */
function fail(res: http.ServerResponse, code: number, message: string, status = 200, data: unknown = null) {
  json(res, status, { code, data, message })
}

function readBody(req: http.IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve) => {
    // ⚠️ 按字节攒、最后一次性按 UTF-8 解码：`raw += chunk` 会让每个分片各自解码，
    //    汉字跨分片时会变成 U+FFFD（同样的写法在旧实现里已经造成过一次真实的静默损坏）
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => {
      try {
        const raw = Buffer.concat(chunks).toString('utf8')
        resolve(raw ? JSON.parse(raw) : {})
      } catch {
        resolve({})
      }
    })
  })
}

const db = load(DB_FILE)

/* ------------------------------------------------------------ 对话会话 */

interface ChatConfigState {
  baseUrl: string
  model: string
  apiKey: string
}

interface ChatSession {
  config: ChatConfigState
  messages: Record<string, unknown>[]
  /** 已连上的 SSE 客户端；发消息时把事件推给它们 */
  clients: Set<http.ServerResponse>
  seq: number
  running: boolean
  abort: boolean
}

/** 一个账号在一个项目下只有一个固定会话；mock 按项目缓存，重启即重置。 */
const chatSessions = new Map<string, ChatSession>()
const answerFeedback = new Map<string, number>()

interface MockMemory {
  metadata?: { memory_type: string; status: string; tags: string[]; updated_at: string }
  memory_id: string
  memory_value: string
  memory_key?: string
  relativity: number
  create_time: string
}

const CHAT_MEMORY: Memory = {
  memoryId: 'mem_write_seed',
  memoryKey: '饮食偏好',
  memoryContent: '用户喜欢川菜和湘菜。',
  memoryType: 'UserMemory',
  status: 'activated',
  tags: ['川菜', '湘菜'],
  version: 3,
  history: [
    { version: 1, content: '用户喜欢清淡菜。', createdAt: '2026-10-29 12:00:00', updateType: 'update' },
    { version: 2, content: '用户喜欢川菜。', createdAt: '2026-10-30 12:00:00', updateType: 'update' },
  ],
  sources: [{ type: 'chat', role: 'user', content: '我现在喜欢川菜和湘菜，请记住。', time: '2026-10-31 12:00:00' }],
  updateTime: '2026-10-31 12:00:00',
  createTime: '2026-10-31 12:00:00',
  subjectType: 'user',
  externalUserId: 'demo',
}

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
const CHAT_MEMORY_COMPLETED_AT = ago(130_000)

/** 真实的 search_memory 结果把业务 JSON 放在 content[0].text 字符串里 */
function memoryContent(memories: MockMemory[], dream: MockMemory[] = []): { type: string; text: string }[] {
  return [
    {
      type: 'text',
      text: JSON.stringify({ dream_memory_detail_list: dream, memory_detail_list: memories }),
    },
  ]
}

function memoryResult(memories: MockMemory[], dream: MockMemory[] = []) {
  return { content: memoryContent(memories, dream), isError: false }
}

function contentHasToolCall(content: unknown): boolean {
  if (!Array.isArray(content)) return false
  return content.some((block) => {
    const rec = block as Record<string, unknown> | null
    const type = str(rec?.type).toLowerCase()
    return type === 'toolcall' || type === 'tool_call' || type === 'tool_use'
  })
}

/** 页尾的下一条是工具结果，且这段里最近的非工具消息是带 toolCall 的助手消息 */
function hasPendingToolResult(messages: Record<string, unknown>[], start: number, end: number): boolean {
  if (end >= messages.length || str(messages[end]?.role) !== 'toolResult') return false
  for (let i = end - 1; i >= start; i--) {
    const role = str(messages[i]?.role)
    if (role === 'toolResult') continue
    return role === 'assistant' && contentHasToolCall(messages[i]?.content)
  }
  return false
}

/** 24 条历史：前 12 条用于分页，后 12 条保留 Markdown/公式与工具调用 */
function seedChatMessages(): Record<string, unknown>[] {
  // 前 12 条是普通问答，让首屏 10 条之后还能再加载一页（10 → 20）
  const earlier: Record<string, unknown>[] = []
  for (let i = 1; i <= 6; i++) {
    earlier.push(
      { role: 'user', content: `第 ${i} 轮：帮我记一下项目背景 ${i}`, timestamp: Date.now() - (1_200_000 - i * 60_000) },
      {
        role: 'assistant',
        content: [{ type: 'text', text: `第 ${i} 轮的回答：这是用于验证历史分页的示例消息。` }],
        timestamp: Date.now() - (1_140_000 - i * 60_000),
      },
    )
  }

  return [
    ...earlier,
    { role: 'user', content: '帮我梳理一下这个项目的背景', timestamp: Date.now() - 720_000 },
    {
      role: 'assistant',
      content: [{ type: 'text', text: '这个项目是团队的长期记忆服务，主要记录偏好、约定与任务上下文。' }],
      timestamp: Date.now() - 660_000,
    },
    { role: 'user', content: '这个项目主要记录了哪些偏好？', timestamp: Date.now() - 600_000 },
    {
      role: 'assistant',
      content: [{ type: 'text', text: '我先看看项目里有哪些偏好记录。' }],
      timestamp: Date.now() - 540_000,
    },
    { role: 'user', content: '团队有哪些协作约定？', timestamp: Date.now() - 480_000 },
    {
      role: 'assistant',
      content: [
        { type: 'text', text: '我先检索一下项目记忆。' },
        { type: 'toolCall', id: 'call_seed', name: 'mcp__memory_console__search_memory', arguments: { query: '用户偏好', limit: 5 } },
      ],
      timestamp: Date.now() - 420_000,
    },
    {
      role: 'toolResult',
      toolCallId: 'call_seed',
      toolName: 'mcp__memory_console__search_memory',
      content: memoryContent([
        {
          memory_id: 'mem_seed_1',
          memory_value: '回答偏好结论先行，给出可量化的数字。',
          memory_key: '回答偏好',
          relativity: 0.95,
          create_time: ago(2 * 3600_000),
          metadata: { memory_type: 'UserMemory', status: 'activated', tags: ['偏好', '回答'], updated_at: ago(3600_000) },
        },
        {
          memory_id: 'mem_seed_2',
          memory_value: '偏好给出命令时用可直接粘贴的完整命令。',
          memory_key: '命令偏好',
          relativity: 0.82,
          create_time: ago(3 * 86_400_000),
        },
      ]),
      isError: false,
      timestamp: Date.now() - 360_000,
    },
    {
      role: 'assistant',
      content: [{ type: 'text', text: '找到 2 条与偏好相关的记忆：结论先行，以及给出可直接粘贴的完整命令。' }],
      timestamp: Date.now() - 300_000,
    },
    { role: 'user', content: '再帮我列一下团队约定', timestamp: Date.now() - 240_000 },
    {
      role: 'assistant',
      // 一条 Markdown 富文本，方便本地验证标题/列表/表格/代码块/公式
      content: [
        {
          type: 'thinking',
          thinking: '用户在问团队约定。先把已有的协作规则整理成分点，再补一个表格和公式示例。',
        },
        {
          type: 'text',
          text: [
            '## 团队约定',
            '',
            '- 提交信息使用中文',
            '- 变更需附影响面说明',
            '',
            '| 项目 | 约定 |',
            '| --- | --- |',
            '| 提交 | 中文 |',
            '| 评审 | 附影响面 |',
            '',
            '行内公式 $E = mc^2$，块级公式：',
            '',
            '$$',
            '\\int_0^1 x^2 \\, dx = \\frac{1}{3}',
            '$$',
            '',
            '示例：',
            '',
            '```ts',
            'const answer = 42',
            '```',
          ].join('\n'),
        },
      ],
      timestamp: Date.now() - 180_000,
    },
    {
      role: 'assistant',
      content: [
        { type: 'thinking', thinking: '把这轮对话提交给记忆服务，等待它整理偏好。' },
        {
          type: 'toolCall',
          id: 'call_write_seed',
          name: 'mcp__memory_console__add_message',
          arguments: { messages: [{ role: 'user', content: '我喜欢川菜和湘菜，请记住我的饮食偏好。' }] },
        },
      ],
      timestamp: Date.now() - 150_000,
    },
    {
      role: 'toolResult',
      toolCallId: 'call_write_seed',
      toolName: 'mcp__memory_console__add_message',
      content: [
        {
          type: 'text',
          text: JSON.stringify({ status: 'success', deferred: true, call_id: 'mock-call-write', message: 'Memory accepted.' }),
        },
      ],
      timestamp: Date.now() - 140_000,
    },
    { role: 'user', content: '谢谢', timestamp: Date.now() - 120_000 },
    {
      role: 'assistant',
      content: [
        {
          type: 'text',
          text: '不客气。需要时我可以继续检索。\n<graph-memory-followups>["你记得我的哪些偏好？","按我的偏好推荐一道家常菜","帮我整理下一步计划"]</graph-memory-followups>',
        },
      ],
      timestamp: Date.now() - 60_000,
    },
  ]
}

function ensureChatSession(projectId: string): ChatSession {
  const existing = chatSessions.get(projectId)
  if (existing) return existing
  const session: ChatSession = {
    // 与后端内置模型一致：项目没有自配 BYOK 时也能直接对话
    config: { baseUrl: 'https://example.invalid/v1', model: 'DeepSeek-V4.1-Flash', apiKey: 'sk-mock-platform' },
    messages: projectId === 'proj_default' ? seedChatMessages() : [],
    clients: new Set(),
    seq: 0,
    running: false,
    abort: false,
  }
  chatSessions.set(projectId, session)
  return session
}

function chatStateFor(projectId: string) {
  const project = db.projects.find((p) => p.id === projectId)
  const key = db.keys.find((k) => k.projectId === projectId)
  const session = ensureChatSession(projectId)
  const hasApiKey = Boolean(key)
  const hasConfig = Boolean(session.config.baseUrl && session.config.model && session.config.apiKey)

  return {
    projectId,
    projectName: str(project?.name),
    hasProject: Boolean(project),
    hasApiKey,
    hasConfig,
    canChat: Boolean(project) && hasApiKey && hasConfig,
    apiKey: key ? { id: str(key.id), label: str(key.keyName), prefix: str(key.apiKey).slice(0, 10) } : null,
    sessionId: `chat_${projectId}`,
    contextWindow: 131_072,
    autoCompact: true,
    piAvailable: true,
  }
}

function chatConfigFor(projectId: string) {
  const { config } = ensureChatSession(projectId)
  const masked = config.apiKey ? `${config.apiKey.slice(0, 6)}…${config.apiKey.slice(-4)}` : ''
  return {
    baseUrl: config.baseUrl,
    model: config.model,
    apiKeyMasked: masked,
    configured: Boolean(config.baseUrl && config.model && config.apiKey),
  }
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/** 按字符切块，模拟逐字输出；用 Array.from 避免把代理对拆成两半 */
function chunks(text: string): string[] {
  const chars = Array.from(text)
  const out: string[] = []
  for (let i = 0; i < chars.length; i += 2) out.push(chars.slice(i, i + 2).join(''))
  return out
}

function broadcast(session: ChatSession, type: string, data: unknown) {
  session.seq += 1
  const payload = `data: ${JSON.stringify({ seq: session.seq, type, data })}\n\n`
  for (const client of session.clients) {
    if (client.writableEnded) {
      session.clients.delete(client)
      continue
    }
    try {
      client.write(payload)
    } catch {
      session.clients.delete(client)
    }
  }
}

/** 流一条助手消息，返回实际发出去的部分（中止时就只有前半句） */
async function streamText(session: ChatSession, text: string, announce = true): Promise<string> {
  if (announce) broadcast(session, 'message_start', { message: { role: 'assistant', content: [] } })
  // 真实 pi 的文本流是 text_start -> text_delta* -> text_end；
  // text_end 带整段 content，前端只认 delta，最终文本由 message_end 对账。
  broadcast(session, 'message_update', {
    message: { role: 'assistant', content: [] },
    assistantMessageEvent: { type: 'text_start', contentIndex: 0, partial: { role: 'assistant', content: [] } },
  })

  let out = ''
  for (const chunk of chunks(text)) {
    if (session.abort) break
    out += chunk
    broadcast(session, 'message_update', {
      message: { role: 'assistant', content: out ? [{ type: 'text', text: out }] : [] },
      assistantMessageEvent: { type: 'text_delta', contentIndex: 0, delta: chunk },
    })
    await sleep(32)
  }

  broadcast(session, 'message_update', {
    message: { role: 'assistant', content: out ? [{ type: 'text', text: out }] : [] },
    assistantMessageEvent: { type: 'text_end', contentIndex: 0, content: out },
  })
  if (announce) {
    broadcast(session, 'message_end', { message: { role: 'assistant', content: out ? [{ type: 'text', text: out }] : [] } })
  }
  return out
}

/** 思考过程：只发 thinking_start / thinking_delta / thinking_end，正文由调用方拼 */
async function streamThinking(session: ChatSession, text: string): Promise<string> {
  broadcast(session, 'message_update', {
    message: { role: 'assistant', content: [] },
    assistantMessageEvent: { type: 'thinking_start', contentIndex: 0 },
  })
  let out = ''
  for (const chunk of chunks(text)) {
    if (session.abort) break
    out += chunk
    broadcast(session, 'message_update', {
      message: { role: 'assistant', content: [] },
      assistantMessageEvent: { type: 'thinking_delta', contentIndex: 0, delta: chunk },
    })
    await sleep(18)
  }
  broadcast(session, 'message_update', {
    message: { role: 'assistant', content: [] },
    assistantMessageEvent: { type: 'thinking_end', contentIndex: 0, content: out },
  })
  return out
}

const MEMORY_TOOL = 'mcp__memory_console__search_memory'

/** 一次工具调用：start -> update -> end，并把调用与结果写回会话历史 */
async function runMemoryTool(session: ChatSession, toolCallId: string, query: string, memories: MockMemory[]) {
  const args = { query, top_k: 5 }
  broadcast(session, 'tool_execution_start', {
    type: 'tool_execution_start',
    toolCallId,
    toolName: MEMORY_TOOL,
    args,
  })
  await sleep(280)
  if (session.abort) return

  broadcast(session, 'tool_execution_update', {
    type: 'tool_execution_update',
    toolCallId,
    toolName: MEMORY_TOOL,
    partialResult: { query, dream_memory_detail_list: [], memory_detail_list: [] },
  })
  await sleep(220)
  if (session.abort) return

  const result = memoryResult(memories)
  broadcast(session, 'tool_execution_end', {
    type: 'tool_execution_end',
    toolCallId,
    toolName: MEMORY_TOOL,
    result,
    isError: false,
    durationMs: 42,
  })
  session.messages.push(
    {
      role: 'assistant',
      content: [{ type: 'toolCall', id: toolCallId, name: MEMORY_TOOL, arguments: args }],
      timestamp: Date.now(),
    },
    {
      role: 'toolResult',
      toolCallId,
      toolName: MEMORY_TOOL,
      content: memoryContent(memories),
      isError: false,
      timestamp: Date.now(),
    },
  )
}

async function streamAssistantReply(projectId: string, userText: string) {
  const session = ensureChatSession(projectId)
  session.running = true
  session.abort = false
  // 提到“记忆/偏好/检索”就演示 thinking + 三次 search_memory（两次命中、一次空结果）
  const search = /记忆|偏好|检索|查找|搜索|memory|prefer|search/i.test(userText)

  broadcast(session, 'agent_start', {})
  broadcast(session, 'turn_start', {})

  try {
    const thinking = search
      ? '用户想从记忆里找和这句话相关的偏好与约定。先检索项目记忆，按相关性挑出几条，再总结给用户。'
      : '这是一个普通问题，不需要查记忆。直接用一句话回答，保持简洁。'
    const intro = search ? '我先检索一下项目记忆。' : '好的，我来回答。'

    broadcast(session, 'message_start', { message: { role: 'assistant', content: [] } })
    const thinkingText = await streamThinking(session, thinking)
    const introText = session.abort ? '' : await streamText(session, intro, false)
    const content = [
      ...(thinkingText ? [{ type: 'thinking', thinking: thinkingText }] : []),
      ...(introText ? [{ type: 'text', text: introText }] : []),
    ]
    broadcast(session, 'message_end', { message: { role: 'assistant', content } })
    if (content.length > 0) {
      session.messages.push({ role: 'assistant', content, timestamp: Date.now() })
    }

    broadcast(session, 'turn_end', {})
    broadcast(session, 'agent_end', {})

    if (search && !session.abort) {
      const rounds: { query: string; memories: MockMemory[] }[] = [
        {
          query: userText,
          memories: [
            {
              memory_id: 'mem_seed_1',
              memory_value: '回答偏好结论先行，给出可量化的数字。',
              memory_key: '回答偏好',
              relativity: 0.93,
              create_time: ago(2 * 3600_000),
            },
            {
              memory_id: 'mem_live_1',
              memory_value: '回答里先给结论，再列依据；不要只给一段笼统的说明。',
              memory_key: '回答结构',
              relativity: 0.88,
              create_time: ago(5 * 86_400_000),
            },
          ],
        },
        {
          query: '团队约定',
          memories: [
            {
              memory_id: 'mem_live_2',
              memory_value: '团队约定：提交信息使用中文。',
              memory_key: '协作约定',
              relativity: 0.9,
              create_time: ago(12 * 86_400_000),
            },
            {
              memory_id: 'mem_live_3',
              memory_value: '评审变更时必须附影响面说明。',
              memory_key: '评审约定',
              relativity: 0.76,
              create_time: ago(30 * 86_400_000),
            },
            {
              memory_id: 'mem_live_4',
              memory_value: '代码评审先看正确性，再看风格。',
              memory_key: '评审顺序',
              relativity: 0.71,
              create_time: ago(45 * 86_400_000),
            },
            {
              memory_id: 'mem_live_5',
              memory_value: '不要提交调试日志和临时文件。',
              memory_key: '提交约束',
              relativity: 0.68,
              create_time: ago(60 * 86_400_000),
            },
            {
              memory_id: 'mem_live_6',
              memory_value: '接口变更要同步更新 openapi.json。',
              memory_key: '接口约定',
              relativity: 0.64,
              create_time: ago(90 * 86_400_000),
            },
            {
              memory_id: 'mem_live_7',
              memory_value: '部署前先跑一遍全量测试。',
              memory_key: '发布约定',
              relativity: 0.59,
              create_time: ago(120 * 86_400_000),
            },
            {
              memory_id: 'mem_live_8',
              memory_value: '文档只写结论和约束，不写决策叙事。',
              memory_key: '文档约定',
              relativity: 0.52,
              create_time: ago(150 * 86_400_000),
            },
          ],
        },
        { query: '不存在的偏好', memories: [] },
      ]

      for (const round of rounds) {
        if (session.abort) break
        broadcast(session, 'agent_start', {})
        broadcast(session, 'turn_start', {})
        await runMemoryTool(session, newId('call'), round.query, round.memories)
        broadcast(session, 'turn_end', {})
        broadcast(session, 'agent_end', {})
      }
    }

    if (!session.abort) {
      broadcast(session, 'agent_start', {})
      broadcast(session, 'turn_start', {})
      const reply = search
        ? '根据检索到的记忆：回答偏好是先给结论再列依据；团队约定里提交信息使用中文，评审要附影响面说明。最后一次查询没有命中记忆。'
        : `已收到：「${userText}」。这是 mock 后端的逐字回复，用于验证流式渲染。`
      const text = await streamText(session, reply)
      if (text) session.messages.push({ role: 'assistant', content: [{ type: 'text', text }], timestamp: Date.now() })
      broadcast(session, 'turn_end', {})
    }
  } finally {
    const aborted = session.abort
    session.running = false
    session.abort = false
    broadcast(session, 'agent_end', {})
    // 整轮结束只发一次；前端以它为准收起停止按钮
    broadcast(session, 'agent_settled', { aborted })
  }
}

function handleChatStream(res: http.ServerResponse, query: URLSearchParams) {
  const projectId = str(query.get('projectId'))
  const session = ensureChatSession(projectId)

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
    ...CORS,
  })

  session.clients.add(res)
  session.seq += 1
  res.write(
    `data: ${JSON.stringify({
      seq: session.seq,
      type: 'chat_ready',
      data: {
        sessionId: `chat_${projectId}`,
        contextWindow: 131_072,
        autoCompact: true,
        running: session.running,
      },
    })}\n\n`,
  )

  const timer = setInterval(() => {
    if (res.writableEnded) return
    session.seq += 1
    try {
      res.write(`data: ${JSON.stringify({ seq: session.seq, type: 'ping', data: {} })}\n\n`)
    } catch {
      clearInterval(timer)
      session.clients.delete(res)
    }
  }, 15_000)

  res.on('close', () => {
    clearInterval(timer)
    session.clients.delete(res)
  })
}

/* ------------------------------------------------------------ 小工具 */

const str = (v: unknown, fallback = '') => (v === undefined || v === null ? fallback : String(v))
const num = (v: unknown, fallback: number) => {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

/** 分页切片，返回切片与总数 */
function paginate<T>(rows: T[], pageNum: number, pageSize: number) {
  const start = (pageNum - 1) * pageSize
  return { items: rows.slice(start, start + pageSize), total: rows.length }
}

const byProject = (rows: Row[], projectId: string) => (projectId ? rows.filter((r) => !r.projectId || r.projectId === projectId) : rows)

const descBy = (field: string) => (a: Row, b: Row) => (String(a[field]) < String(b[field]) ? 1 : -1)

/* ------------------------------------------------------- 提示词工作台的小工具 */

/** mock 里「登录账号」的显示名。真实控制面从会话里取，不信任浏览器传来的值 */
const PREVIEW_AUTHOR = 'demo-user'

/** 试跑与 AI 改写的限流桶，按项目分。数值与控制面默认值一致 */
const testBuckets = new Map<string, () => number>()
const generateBuckets = new Map<string, () => number>()

function takeToken(map: Map<string, () => number>, key: string, burst: number, refill: number): number {
  let bucket = map.get(key)
  if (!bucket) {
    bucket = makeBucket(burst, refill)
    map.set(key, bucket)
  }
  return bucket()
}

function requireProject(body: Record<string, unknown>): string {
  const id = str(body.projectId)
  if (!id) throw new HttpError(40000, '缺少 projectId', 400)
  return id
}

function requirePrompt(key: string): PromptDef {
  const def = findPrompt(key)
  if (!def) throw new HttpError(40400, `未知的提示词：${key}`, 404)
  return def
}

/** 注册项出参：内置默认值单独走 `defaults`，不混在注册项里 */
function registryOf(def: PromptDef): Omit<PromptDef, 'defaults'> {
  const { defaults: _defaults, ...rest } = def
  return rest
}

function nextPromptVersionId(): number {
  return db.promptVersions.reduce((n, v) => Math.max(n, Number(v.versionId) || 0), 0) + 1
}

function versionView(v: Row) {
  return {
    versionId: Number(v.versionId),
    lang: String(v.lang),
    kind: String(v.kind),
    body: String(v.body ?? ''),
    note: String(v.note ?? ''),
    author: String(v.author ?? ''),
    createdAt: String(v.createdAt ?? ''),
    parentVersionId: v.parentVersionId === null || v.parentVersionId === undefined ? null : Number(v.parentVersionId),
  }
}

/* -------------------------------------------------------------- 路由 */

interface Ctx {
  body: Record<string, unknown>
  query: URLSearchParams
  /** 路径里的 :参数 */
  params: Record<string, string>
  url: URL
}

type Handler = (ctx: Ctx) => unknown

const ROUTES: Record<string, Handler> = {
  /* ---------------------------------------------------- 项目（工作区） */

  'GET /api/dashboard/project/dict': ({ query }) => {
    // 按语言返回名字，这里只有中文，原样返回
    void query
    return db.projects.map((p) => ({
      id: p.id,
      name: p.name,
      status: p.status,
      role: p.role,
      member: p.member,
    }))
  },

  'POST /api/dashboard/project/list': ({ body }) => {
    const { items, total } = paginate(db.projects, num(body.pageNum, 1), num(body.pageSize, 10))
    return { records: items, total }
  },

  'POST /api/dashboard/project/create': ({ body }) => {
    const name = str(body.name).trim()
    if (!name) throw new HttpError(40000, '项目名称不能为空')
    if (db.projects.some((p) => p.name === name)) throw new HttpError(40000, '已存在同名项目')
    const row: Row = {
      id: newId('proj'),
      name,
      description: str(body.description),
      isMultiView: Number(body.isMultiView) ? 1 : 0,
      status: 'DISABLED',
      createTime: iso(new Date()),
    }
    db.projects.push(row)
    save(DB_FILE, db)
    return row
  },

  'POST /api/dashboard/project/update': ({ body }) => {
    const row = db.projects.find((p) => p.id === body.id)
    if (!row) throw new HttpError(40001, '项目不存在')
    // 与当前控制面一致：current 选择会话，不走 status 的项目启停语义。
    if (body.current === true) for (const project of db.projects) project.status = project.id === row.id ? 'ENABLED' : 'DISABLED'
    // 切换当前项目：body 只有 { id, status }；编辑：body 带 name
    if (body.name !== undefined) {
      row.name = str(body.name).trim()
      row.description = str(body.description)
      row.isMultiView = Number(body.isMultiView) ? 1 : 0
    }
    if (body.current === true) {
      for (const p of db.projects) {
        if (p.id === row.id) p.status = 'ENABLED'
        else if (p.status === 'ENABLED') p.status = 'DISABLED'
      }
    }
    if (body.status !== undefined) {
      if (body.status === 'ENABLED') for (const p of db.projects) p.status = 'DISABLED'
      row.status = str(body.status)
    }
    save(DB_FILE, db)
    return row
  },

  'POST /api/dashboard/project/revoke/:id': ({ params }) => {
    const i = db.projects.findIndex((p) => p.id === params.id)
    if (i < 0) throw new HttpError(40001, '项目不存在')
    db.projects.splice(i, 1)
    save(DB_FILE, db)
    return { ok: true }
  },

  /* ------------------------------------------------------------ 密钥 */

  'POST /api/dashboard/keys/list': ({ body }) => {
    const rows = byProject(db.keys, str(body.projectId)).sort(descBy('createTime'))
    const { items, total } = paginate(rows, num(body.pageNum, 1), num(body.pageSize, 10))
    return { records: items.map(publicKey), total }
  },

  'POST /api/dashboard/keys/create': ({ body }) => {
    const projectId = str(body.projectId)
    const keyName = str(body.keyName).trim()
    if (!projectId) throw new HttpError(40000, '缺少 projectId')
    if (!keyName) throw new HttpError(40000, '密钥名称不能为空')
    const row: Row = { id: newId('key'), projectId, keyName, apiKey: newSecret(), createTime: iso(new Date()) }
    db.keys.push(row)
    save(DB_FILE, db)
    return { ...publicKey(row), apiKey: row.apiKey }
  },

  'POST /api/dashboard/keys/revoke/:id': ({ params }) => {
    const i = db.keys.findIndex((k) => k.id === params.id)
    if (i < 0) throw new HttpError(40001, '密钥不存在')
    db.keys.splice(i, 1)
    save(DB_FILE, db)
    return { ok: true }
  },

  'POST /api/dashboard/keys/copy': ({ body }) => {
    const row = db.keys.find((k) => k.id === body.apiKeyId)
    if (!row) throw new HttpError(40001, '密钥不存在')
    return { apiKey: row.apiKey }
  },

  /* ------------------------------------------------------------ 对话 */

  'GET /api/chat/feedback': ({ query }) => ({
    vote: answerFeedback.get(str(query.get('projectId')) + ':' + str(query.get('responseId'))) || 0,
  }),
  'PUT /api/chat/feedback': ({ body }) => {
    const vote = Number(body.vote)
    answerFeedback.set(str(body.projectId) + ':' + str(body.responseId), vote)
    return { vote }
  },
  'GET /api/chat/state': ({ query }) => chatStateFor(str(query.get('projectId'))),

  /* ------------------------------------------------------------ 本地记忆镜像 */

  // 与控制面一致：通道默认关闭，三条读接口统一 503 + 50300
  'GET /api/dashboard/local-sync/devices': localSyncOff,
  'GET /api/dashboard/local-sync/summary': localSyncOff,
  'GET /api/dashboard/local-sync/memories': localSyncOff,

  /* ------------------------------------------------------------ 平台指引进度 */

  'GET /api/auth/onboarding': ({ query }) => onboardingFor(str(query.get('projectId'))),

  'PUT /api/auth/onboarding': ({ body }) => {
    const state = onboardingFor(str(body.projectId))
    if (Number(body.revision) !== state.revision) throw new HttpError(40900, '进度已更新，请重试', 409)
    const action = str(body.action)
    if (action === 'start') state.status = 'active'
    else if (action === 'replay') Object.assign(state, { status: 'active', step: 0, chat: undefined })
    else if (action === 'dismiss') state.status = 'dismissed'
    else if (action === 'advance') {
      if (state.status !== 'active' || state.step >= 6) throw new HttpError(40000, '请先开始指引')
      state.step++
      if (state.step === 6) state.status = 'completed'
    } else if (action === 'back') {
      if (state.status !== 'active' || state.step <= 0 || state.step > 5) throw new HttpError(40000, '当前步骤无法返回')
      state.step--
    } else if (action === 'checkpoint') {
      if (state.status !== 'active' || state.step !== 3) throw new HttpError(40000, '请先进入对话指引')
      state.chat = body.checkpoint as OnboardingState['chat']
    } else throw new HttpError(40000, '操作无效')
    state.revision++
    return state
  },

  'GET /api/chat/config': ({ query }) => chatConfigFor(str(query.get('projectId'))),

  'PUT /api/chat/config': ({ body }) => {
    const projectId = str(body.projectId)
    if (!projectId) throw new HttpError(40000, '缺少 projectId')
    if (!db.projects.some((p) => p.id === projectId)) throw new HttpError(40001, '项目不存在')

    const session = ensureChatSession(projectId)
    const baseUrl = str(body.baseUrl).trim()
    const model = str(body.model).trim()
    const apiKey = str(body.apiKey).trim()
    if (!baseUrl) throw new HttpError(40000, '上游地址不能为空')
    if (!model) throw new HttpError(40000, '模型不能为空')
    // 空 apiKey 表示保留原值
    const nextApiKey = apiKey || session.config.apiKey
    if (!nextApiKey) throw new HttpError(40000, '上游 API Key 不能为空')

    session.config = { baseUrl, model, apiKey: nextApiKey }
    return { ok: true }
  },

  'GET /api/chat/messages': ({ query }) => {
    const session = ensureChatSession(str(query.get('projectId')))
    const rawLimit = Number(query.get('limit') ?? 10)
    const limit = Number.isFinite(rawLimit) ? Math.min(Math.max(Math.trunc(rawLimit), 1), 50) : 10
    const all = session.messages
    const rawBefore = query.get('before')
    const beforeValue = rawBefore === null ? Number.NaN : Number(rawBefore)
    const before = Number.isFinite(beforeValue) ? Math.min(Math.max(Math.trunc(beforeValue), 0), all.length) : all.length

    let start = Math.max(0, before - limit)
    // 页首落在工具结果上时向前多带一条，和后端行为一致
    if (start > 0 && str(all[start]?.role) === 'toolResult') start -= 1

    // 页尾落在工具调用上时向后多带工具结果，相邻页因此会有轻微重叠
    let end = before
    while (end < all.length && hasPendingToolResult(all, start, end)) end += 1

    return {
      messages: all.slice(start, Math.max(start, end)),
      start,
      total: all.length,
      hasMore: start > 0,
    }
  },

  'POST /api/chat/abort': ({ body }) => {
    const projectId = str(body.projectId)
    if (!projectId) throw new HttpError(40000, '缺少 projectId')
    const session = ensureChatSession(projectId)
    session.abort = true
    // 没有在跑的一轮时（例如刚重连过）也回结束事件，让前端收起停止按钮
    if (!session.running) {
      broadcast(session, 'agent_end', {})
      broadcast(session, 'agent_settled', { aborted: true })
    }
    return { ok: true }
  },

  /* -------------------------------------------------------- 调用日志 */

  'POST /api/dashboard/log/list': ({ body }) => {
    let rows = byProject(db.logs, str(body.projectId))
    const q = str(body.query).toLowerCase()
    if (q) rows = rows.filter((r) => String(r.traceId).toLowerCase().includes(q) || String(r.userId).toLowerCase().includes(q))
    if (body.eventType) rows = rows.filter((r) => r.eventType === body.eventType)
    if (body.status) rows = rows.filter((r) => r.status === body.status)
    if (body.subjectType && body.subjectType !== 'all' && body.subjectId) {
      const id = str(body.subjectId)
      rows = rows.filter((r) => (body.subjectType === 'user' ? r.userId === id : r.agentId === id))
    }
    rows = [...rows].sort(descBy('createTime'))
    // 列表响应在这个接口叫 logs，不是 records
    const { items, total } = paginate(rows, num(body.pageNum, 1), num(body.pageSize, 50))
    return { logs: items.map(listLog), total }
  },

  'POST /api/dashboard/log/detail': ({ body }) => {
    const row = db.logs.find((l) => l.id === body.logId)
    if (!row) throw new HttpError(40001, '日志不存在')
    return row
  },

  /* ------------------------------------------------------------ 记忆 */

  'POST /api/dashboard/project/memory/list': ({ body }) => {
    let rows = byProject(db.memories, str(body.projectId))
    if (body.memoryDetailType) rows = rows.filter((r) => r.memoryDetailType === body.memoryDetailType)
    if (body.knowledgeBaseId) rows = rows.filter((r) => r.knowledgeBaseId === body.knowledgeBaseId)
    if (body.externalUserId) rows = rows.filter((r) => r.externalUserId === body.externalUserId)
    if (body.subjectType) rows = rows.filter((r) => r.subjectType === body.subjectType)
    const q = str(body.memSearchContent).toLowerCase()
    if (q) rows = rows.filter((r) => String(r.memoryContent).toLowerCase().includes(q) || ''.toLowerCase().includes(q))
    rows = [...rows].sort(descBy('createTime'))
    const pageNum = num(body.pageNum, 1)
    const pageSize = num(body.pageSize, 50)
    if (body.projectId === 'proj_default' && body.memoryDetailType === 'text_mem') rows = [CHAT_MEMORY as unknown as Row, ...rows]
    const { items, total } = paginate(rows, pageNum, pageSize)
    // 列表响应在这个接口叫 memListItemResponseList，分页字段是 current
    return { memListItemResponseList: items.map(publicMemory), total, current: pageNum }
  },

  'POST /api/dashboard/project/memory/activity': ({ body }) => {
    const ids = Array.isArray(body.callIds) && body.callIds.length ? body.callIds : body.after ? [] : ['mock-call-write']
    const records = ids.map((callId) => ({
      callId,
      status: callId === 'mock-call-write' && body.projectId === 'proj_default' ? 'succeeded' : 'not_found',
      completedAt: CHAT_MEMORY_COMPLETED_AT,
      memories: callId === 'mock-call-write' && body.projectId === 'proj_default' ? [CHAT_MEMORY] : [],
      total: callId === 'mock-call-write' && body.projectId === 'proj_default' ? 1 : 0,
      truncated: false,
    }))
    return { records, total: records.length, cursor: CHAT_MEMORY_COMPLETED_AT }
  },

  'POST /api/dashboard/project/memory/detail': ({ body }) => {
    if (body.memoryId === CHAT_MEMORY.memoryId && body.projectId === 'proj_default') return CHAT_MEMORY
    const row = db.memories.find((m) => m.memoryId === body.memoryId || m.id === body.memoryId)
    if (!row) throw new HttpError(40001, '记忆不存在')
    return {
      ...publicMemory(row),
      version: CHAT_MEMORY.version,
      history: CHAT_MEMORY.history,
      sources: CHAT_MEMORY.sources,
      updateTime: CHAT_MEMORY.updateTime,
    }
  },

  'POST /api/dashboard/project/memory/delete': ({ body }) => {
    const ids = Array.isArray(body.memory_ids) ? body.memory_ids.map(String) : []
    const before = db.memories.length
    db.memories = db.memories.filter((m) => !ids.includes(String(m.memoryId)) && !ids.includes(String(m.id)))
    save(DB_FILE, db)
    return { deleted: before - db.memories.length }
  },

  /* ---------------------------------------------------------- 知识库 */

  'POST /api/dashboard/knowledgebase/list': ({ body }) => {
    let rows = [...db.knowledgeBases]
    const name = str(body.name).toLowerCase()
    if (name) rows = rows.filter((k) => String(k.knowledgebaseName).toLowerCase().includes(name))
    if (body.projectId) rows = rows.filter((k) => (k.projectIds as string[])?.includes(str(body.projectId)))
    const { items, total } = paginate(rows, num(body.pageNum, 1), num(body.pageSize, 10))
    return { records: items, total }
  },

  'POST /api/dashboard/knowledgebase/create': ({ body }) => {
    const name = str(body.knowledgebaseName).trim()
    if (!name) throw new HttpError(40000, '知识库名称不能为空')
    const row: Row = {
      id: newId('kb'),
      kbId: '',
      knowledgebaseName: name,
      knowledgebaseDescription: str(body.knowledgebaseDescription),
      projectIds: Array.isArray(body.projectIds) ? body.projectIds.map(String) : [],
      createTime: iso(new Date()),
    }
    row.kbId = row.id
    db.knowledgeBases.push(row)
    save(DB_FILE, db)
    return row
  },

  'POST /api/dashboard/knowledgebase/update': ({ body }) => {
    const row = db.knowledgeBases.find((k) => k.kbId === body.id || k.id === body.id)
    if (!row) throw new HttpError(40001, '知识库不存在')
    row.knowledgebaseName = str(body.knowledgebaseName).trim()
    row.knowledgebaseDescription = str(body.knowledgebaseDescription)
    if (Array.isArray(body.projectIds)) row.projectIds = body.projectIds.map(String)
    save(DB_FILE, db)
    return row
  },

  'POST /api/dashboard/knowledgebase/detail': ({ body }) => {
    const row = db.knowledgeBases.find((k) => k.kbId === body.knowledgeBaseId || k.id === body.knowledgeBaseId)
    if (!row) throw new HttpError(40001, '知识库不存在')
    return row
  },

  'POST /api/dashboard/knowledgebase/dict': ({ body }) => {
    const name = str(body.name).toLowerCase()
    const rows = name ? db.knowledgeBases.filter((k) => String(k.knowledgebaseName).toLowerCase().includes(name)) : db.knowledgeBases
    return rows.map((k) => ({ id: k.kbId, name: k.knowledgebaseName }))
  },

  'POST /api/dashboard/knowledgebase/project/associate': ({ body }) => {
    const ids = Array.isArray(body.knowledgeBaseIds) ? body.knowledgeBaseIds.map(String) : []
    const projectId = str(body.projectId)
    for (const k of db.knowledgeBases) {
      const list = (k.projectIds as string[]) ?? []
      const has = list.includes(projectId)
      if (ids.includes(String(k.kbId)) && !has) k.projectIds = [...list, projectId]
      if (!ids.includes(String(k.kbId)) && has) k.projectIds = list.filter((p) => p !== projectId)
    }
    save(DB_FILE, db)
    return { ok: true }
  },

  'POST /api/dashboard/knowledgebase/project/remove': ({ body }) => {
    const projectId = str(body.projectId)
    const kbId = str(body.knowledgeBaseId)
    const row = db.knowledgeBases.find((k) => k.kbId === kbId)
    if (row) row.projectIds = ((row.projectIds as string[]) ?? []).filter((p) => p !== projectId)
    if (body.deleteAll) db.knowledgeBases = db.knowledgeBases.filter((k) => k.kbId !== kbId)
    save(DB_FILE, db)
    return { ok: true }
  },

  'POST /api/dashboard/knowledgebase/file/list': () => ({ records: [], total: 0 }),
  'POST /api/dashboard/knowledgebase/file/delete': () => ({ ok: true }),
  'POST /api/dashboard/knowledgebase/file/log/list': () => ({ records: [], total: 0 }),
  'POST /api/dashboard/knowledgebase/file/log/detail': () => ({}),

  'POST /api/dashboard/knowledgebase/memory/list': ({ body }) => {
    const rows = db.memories.filter((m) => m.knowledgeBaseId === body.knowledgeBaseId)
    const { items, total } = paginate(rows, num(body.pageNum, 1), num(body.pageSize, 10))
    return { records: items.map(publicMemory), total, size: num(body.pageSize, 10) }
  },

  'POST /api/dashboard/knowledgebase/memory/detail': ({ body }) => {
    const row = db.memories.find((m) => m.id === body.updateLogId)
    if (!row) throw new HttpError(40001, '记忆更新记录不存在')
    return publicMemory(row)
  },

  /* -------------------------------------------------------- 画像模板 */

  'POST /api/dashboard/profileTemplate/list': ({ body }) => {
    const rows = byProject(db.templates, str(body.projectId))
    const pageNum = num(body.pageNum, 1)
    const pageSize = num(body.pageSize, 10)
    const { items, total } = paginate(rows, pageNum, pageSize)
    // 列表响应在这个接口叫 list，且带 pages
    return { list: items, total, current: pageNum, size: pageSize, pages: Math.max(1, Math.ceil(total / pageSize)) }
  },

  'GET /api/dashboard/profileTemplate/detail': ({ query }) => {
    const row = db.templates.find((t) => t.templateId === query.get('templateId'))
    if (!row) throw new HttpError(40001, '模板不存在')
    return row
  },

  'POST /api/dashboard/profileTemplate/create': ({ body }) => {
    const name = str(body.templateName).trim()
    if (!name) throw new HttpError(40000, '模板名称不能为空')
    const row: Row = {
      id: newId('tpl'),
      templateId: '',
      templateName: name,
      bindingCount: 0,
      updatedAt: iso(new Date()),
      projectId: str(body.projectId, 'proj_default'),
    }
    row.templateId = row.id
    // 模板字段：body 里除了元信息以外的键都算分组
    for (const [k, v] of Object.entries(body)) if (!META_KEYS.has(k)) row[k] = v
    db.templates.push(row)
    save(DB_FILE, db)
    return row
  },

  'POST /api/dashboard/profileTemplate/update': ({ body }) => {
    const row = db.templates.find((t) => t.templateId === body.templateId)
    if (!row) throw new HttpError(40001, '模板不存在')
    for (const [k, v] of Object.entries(body)) if (k !== 'templateId' && k !== 'projectId') row[k] = v
    row.updatedAt = iso(new Date())
    save(DB_FILE, db)
    return row
  },

  'POST /api/dashboard/profileTemplate/delete': ({ body }) => {
    const i = db.templates.findIndex((t) => t.templateId === body.templateId)
    if (i < 0) throw new HttpError(40001, '模板不存在')
    db.templates.splice(i, 1)
    save(DB_FILE, db)
    return { ok: true }
  },

  /* ------------------------------------------------------------ 用量 */

  'POST /api/dashboard/usage/query': ({ body }) => {
    // 按天聚合调用日志。真后端从审计流水算，这里从数据集算，形状一致
    const byDay = new Map<string, Record<string, { calls?: number; totalTokens?: number }>>()
    for (const r of byProject(db.logs, str(body.projectId))) {
      const day = String(r.createTime).slice(0, 10)
      const bucket = byDay.get(day) ?? {}
      const key = String(r.eventType)
      const cur = bucket[key] ?? { calls: 0, totalTokens: 0 }
      cur.calls = (cur.calls ?? 0) + 1
      cur.totalTokens = (cur.totalTokens ?? 0) + Number(r.requestTokens ?? 0) + Number(r.responseTokens ?? 0)
      bucket[key] = cur
      byDay.set(day, bucket)
    }
    const days: { date: string }[] = []
    const start = new Date(str(body.startDate) || Date.now() - 13 * 86400000)
    const end = new Date(str(body.endDate) || Date.now())
    for (let d = new Date(start); d <= end && days.length < 120; d.setDate(d.getDate() + 1)) {
      days.push({ date: iso(d).slice(0, 10) })
    }
    const dailyStats = days.map(({ date }) => {
      const b = byDay.get(date) ?? {}
      return {
        date,
        addMessage: b.addMessage ?? { calls: 0, totalTokens: 0 },
        searchMemory: b.searchMemory ?? { calls: 0, totalTokens: 0 },
        chat: { requestTokens: b.chat?.totalTokens ?? 0, responseTokens: 0, totalTokens: b.chat?.totalTokens ?? 0 },
        extractMemory: { requestTokens: 0, responseTokens: 0, totalTokens: 0 },
        rerank: { requestTokens: 0, totalTokens: 0 },
      }
    })
    return { dailyStats }
  },

  /* ------------------------------------------------------ 自定义抽取 */

  'POST /api/dashboard/prompt-config/detail': ({ body }) => {
    const row = db.promptConfigs.find((p) => p.projectId === str(body.projectId, 'proj_default')) ?? db.promptConfigs[0]
    return row ?? {}
  },

  'POST /api/dashboard/prompt-config/save': ({ body }) => {
    const projectId = str(body.projectId, 'proj_default')
    const row = db.promptConfigs.find((p) => p.projectId === projectId)
    if (row) {
      Object.assign(row, body, { updatedAt: iso(new Date()) })
    } else {
      db.promptConfigs.push({ id: newId('pc'), projectId, ...body, updatedAt: iso(new Date()) })
    }
    save(DB_FILE, db)
    return { ok: true }
  },

  'POST /api/dashboard/prompt-config/generate': ({ body }) => ({
    prompt: `根据「${str(body.description, '当前描述')}」生成的抽取规则：\n\n1. 只保留跨任务仍然成立的内容\n2. 一次性的任务流水不写入\n3. 个案结论不写入`,
  }),

  'POST /api/dashboard/prompt-config/reset-default': () => ({
    prompt:
      '你是一个记忆抽取器。从下面的对话里抽取值得长期保留的信息。\n\n只保留跨任务、跨会话仍然成立的内容；一次性的任务流水与个案结论不要写入。',
  }),

  'POST /api/dashboard/prompt-config/debug/extract': ({ body }) => ({
    extractionTypes: Array.isArray(body.extractionTypes) ? body.extractionTypes : ['text_mem'],
    results: [
      { type: 'text_mem', content: '用户所在团队负责长期记忆服务的检索链路' },
      { type: 'pref_mem', content: '偏好：结论先行，给出可量化的数字' },
    ],
  }),

  /* ---------------------------------------------------------- 提示词工作台 */

  'POST /api/dashboard/prompts/list': ({ body }) => {
    const projectId = requireProject(body)
    return {
      prompts: PROMPTS.map((p) => ({
        key: p.key,
        group: p.group,
        title: p.title,
        summary: p.summary,
        languages: p.languages,
        editable: p.editable,
        state: stateOf(db.promptVersions, projectId, p),
      })),
    }
  },

  'POST /api/dashboard/prompts/detail': ({ body }) => {
    const projectId = requireProject(body)
    const def = requirePrompt(str(body.key))
    const defaults: Record<string, string> = {}
    const effective: Record<string, unknown> = {}
    for (const lang of def.languages) {
      defaults[lang] = def.defaults[lang] ?? ''
      const slot = readSlot(db.promptVersions, projectId, def, lang)
      effective[lang] = {
        body: slot.body,
        source: slot.source,
        versionId: slot.versionId,
        versionNo: slot.versionNo,
        updatedAt: slot.updatedAt,
        author: slot.author,
      }
    }
    return { ...registryOf(def), defaults, effective }
  },

  'POST /api/dashboard/prompts/versions': ({ body }) => {
    const projectId = requireProject(body)
    const def = requirePrompt(str(body.key))
    const lang = str(body.lang)
    if (!def.languages.includes(lang)) throw new HttpError(40000, `这条提示词没有「${lang}」版本`, 400)
    const limit = Math.min(100, num(body.limit, 20))
    const before = Number(body.beforeVersionId)
    let rows = db.promptVersions.filter((v) => v.projectId === projectId && v.promptKey === def.key && v.lang === lang)
    if (Number.isFinite(before) && before > 0) rows = rows.filter((v) => Number(v.versionId) < before)
    rows = [...rows].sort((a, b) => Number(b.versionId) - Number(a.versionId))
    return {
      versions: rows
        .slice(0, limit)
        .map((v) => ({ ...versionView(v), versionNo: versionNoOf(db.promptVersions, projectId, def, lang, Number(v.versionId)) })),
      hasMore: rows.length > limit,
    }
  },

  'POST /api/dashboard/prompts/validate': ({ body }) => {
    // 始终 200：校验结果本身就是响应内容
    const def = requirePrompt(str(body.key))
    return validatePrompt(def, str(body.lang), str(body.body))
  },

  'POST /api/dashboard/prompts/save': ({ body }) => {
    const projectId = requireProject(body)
    const def = requirePrompt(str(body.key))
    const lang = str(body.lang)
    if (!def.languages.includes(lang)) throw new HttpError(40000, `这条提示词没有「${lang}」版本`, 400)
    const note = str(body.note)
    if (note.length > LIMITS.note) throw new HttpError(40000, `备注不超过 ${LIMITS.note} 字`, 400)
    if (str(body.body).length > LIMITS.body) {
      throw new HttpError(41300, `草稿正文最多 ${LIMITS.body} 字，请精简后再试`, 413)
    }

    const current = readSlot(db.promptVersions, projectId, def, lang)
    const base = Number(body.baseVersionId ?? 0)
    if (base !== current.versionId) {
      // 真实响应只给这两个字段；当前槽位被还原成默认时它们是 0，不是缺失
      throw new HttpError(40900, '提示词已被他人更新，请刷新后再保存。', 409, {
        currentVersionId: current.versionId,
        currentVersionNo: current.versionNo,
      })
    }

    let nextBody = str(body.body)
    let kind = 'save'
    const restoreFrom = Number(body.restoreFromVersionId)
    if (Number.isFinite(restoreFrom) && restoreFrom > 0) {
      const target = db.promptVersions.find(
        (v) => v.projectId === projectId && v.promptKey === def.key && v.lang === lang && Number(v.versionId) === restoreFrom,
      )
      if (!target) throw new HttpError(40001, '这个历史版本不存在，请刷新版本历史后再试', 404)
      nextBody = String(target.body)
      // 回滚也是「以旧内容追加一版」，但历史里要能与手动编辑区分
      kind = 'rollback'
    }

    const validation = validatePrompt(def, lang, nextBody)
    if (!validation.ok)
      throw new HttpError(40001, validation.errors[0]?.message ?? '提示词没有通过校验', 400, { errors: validation.errors })

    // 与当前生效内容完全一致：不新增版本
    if (nextBody === current.body) {
      return {
        versionId: current.versionId,
        versionNo: current.versionNo,
        createdAt: current.updatedAt,
        effective: {
          body: current.body,
          source: current.source,
          versionId: current.versionId,
          versionNo: current.versionNo,
          updatedAt: current.updatedAt,
          author: current.author,
        },
      }
    }

    const versionId = nextPromptVersionId()
    const createdAt = iso(new Date())
    db.promptVersions.push({
      id: newId('pv'),
      versionId,
      projectId,
      promptKey: def.key,
      lang,
      kind,
      body: nextBody,
      note,
      author: PREVIEW_AUTHOR,
      parentVersionId: current.versionId || null,
      createdAt,
    })
    save(DB_FILE, db)
    const versionNo = versionNoOf(db.promptVersions, projectId, def, lang, versionId)
    return {
      versionId,
      versionNo,
      createdAt,
      effective: { body: nextBody, source: 'project', versionId, versionNo, updatedAt: createdAt, author: PREVIEW_AUTHOR },
    }
  },

  'POST /api/dashboard/prompts/reset': ({ body }) => {
    const projectId = requireProject(body)
    const key = str(body.key)
    const defs = key ? [requirePrompt(key)] : PROMPTS
    const lang = str(body.lang)
    const now = iso(new Date())
    const reset: { key: string; lang: string; versionId: number; versionNo: number }[] = []
    for (const def of defs) {
      const langs = lang ? [lang] : def.languages
      for (const l of langs) {
        if (!def.languages.includes(l)) continue
        const slot = readSlot(db.promptVersions, projectId, def, l)
        if (slot.source === 'default') continue
        const versionId = nextPromptVersionId()
        db.promptVersions.push({
          id: newId('pv'),
          versionId,
          projectId,
          promptKey: def.key,
          lang: l,
          kind: 'reset',
          body: '',
          note: '',
          author: PREVIEW_AUTHOR,
          parentVersionId: slot.versionId,
          createdAt: now,
        })
        reset.push({ key: def.key, lang: l, versionId, versionNo: versionNoOf(db.promptVersions, projectId, def, l, versionId) })
      }
    }
    if (reset.length) save(DB_FILE, db)
    return { reset }
  },

  'POST /api/dashboard/prompts/test': ({ body }) => {
    const projectId = requireProject(body)
    const wait = takeToken(testBuckets, projectId, 3, 6)
    if (wait > 0) throw new HttpError(42900, `试跑太频繁，请 ${wait} 秒后再试。`, 429, { retryAfterSec: wait })

    const def = requirePrompt(str(body.key))
    const lang = str(body.lang)
    if (!def.languages.includes(lang)) throw new HttpError(40000, `这条提示词没有「${lang}」版本`, 400)
    if (Buffer.byteLength(JSON.stringify(body), 'utf8') > LIMITS.requestBytes) {
      throw new HttpError(41300, '试跑内容过大，请减少输入后再试。', 413)
    }

    const current = readSlot(db.promptVersions, projectId, def, lang)
    const draft = str(body.body) || current.body
    if (draft.length > LIMITS.body) throw new HttpError(41300, `提示词超过 ${LIMITS.body} 字符，请删减后再试。`, 413)
    const inputError = checkInput(def, body.input)
    if (inputError) throw new HttpError(40000, inputError, 400)

    const runs = [
      simulateRun(def, draft, body.input, 'draft', {
        key: def.key,
        lang,
        source: 'draft',
        ...(current.source === 'project' ? { versionId: current.versionId, versionNo: current.versionNo } : {}),
      }),
    ]
    if (body.compareDefault === true) {
      runs.push(simulateRun(def, def.defaults[lang] ?? '', body.input, 'default', { key: def.key, lang, source: 'default' }))
    }
    return { runs }
  },

  'POST /api/dashboard/prompts/generate': ({ body }) => {
    const projectId = requireProject(body)
    const wait = takeToken(generateBuckets, projectId, 2, 4)
    if (wait > 0) throw new HttpError(42900, `改写太频繁，请 ${wait} 秒后再试。`, 429, { retryAfterSec: wait })

    const def = requirePrompt(str(body.key))
    const lang = str(body.lang)
    const instruction = str(body.instruction).trim()
    if (!instruction) throw new HttpError(40000, '请先写一句改写要求。', 400)
    if (instruction.length > LIMITS.instruction) {
      throw new HttpError(41300, `改写要求最多 ${LIMITS.instruction} 字，请精简后再试`, 413)
    }
    const current = readSlot(db.promptVersions, projectId, def, lang)
    const base = str(body.baseBody) || current.body
    if (base.length > LIMITS.body) {
      throw new HttpError(41300, `用作改写的原文最多 ${LIMITS.body} 字，请精简后再试`, 413)
    }
    const next = `${base}\n\n${instruction}`
    return { body: next, validation: validatePrompt(def, lang, next) }
  },

  /* -------------------------------------------------------------- 账号 */

  'GET /api/user/quota/summary': () => ({
    memoryCount: db.memories.length,
    knowledgebaseCount: db.knowledgeBases.length,
    knowledgebaseCapacity: 1_000_000,
    allowedKnowledgebaseCount: 10,
  }),

  /* -------------------------------------------------------------- 模型 */

  'GET /api/dashboard/models': () => ({
    records: [
      { role: '记忆读取', model: 'qwen-max', spec: 'dashscope.aliyuncs.com', source: 'MEMRADER_MODEL', group: 'memory' },
      { role: '记忆写入 / 对话', model: 'qwen-plus', spec: 'dashscope.aliyuncs.com', source: 'MOS_CHAT_MODEL', group: 'chat' },
      { role: '向量化', model: 'text-embedding-v3', spec: '1536 维', source: 'MOS_EMBEDDER_MODEL', group: 'memory' },
      { role: '重排', model: 'bge-reranker-large', spec: '后端 bge', source: 'MOS_RERANKER_MODEL', group: 'memory' },
    ],
    total: 4,
  }),

  /* ------------------------------------------ 图谱 */

  'GET /api/dashboard/graph/memories': ({ query }) => {
    const tenant = str(query.get('tenant'), 'proj_default')
    const list = db.memories.filter((m) => !m.projectId || m.projectId === tenant)
    const GRAPH_TYPES = ['LongTermMemory', 'PreferenceMemory', 'InsightMemory', 'Context', 'UserMemory']
    const nodes = list.map((m, idx) => ({
      id: m.id,
      label: str(m.memoryText || m.content, '记忆节点'),
      labelTruncated: false,
      memoryType: GRAPH_TYPES[idx % GRAPH_TYPES.length],
      createdAt: str(m.createdAt, iso(new Date())),
      sessionId: `session_${idx % 4}`,
      clusterId: `session:session_${idx % 4}`,
      userId: str(m.userId, 'user_default'),
      sourceIds: [],
    }))
    const links: { source: string; target: string; kind: 'session' | 'parent' | 'merged' | 'derived' }[] = []
    for (let i = 1; i < nodes.length; i++) {
      if (nodes[i].sessionId === nodes[i - 1].sessionId) {
        links.push({ source: nodes[i - 1].id, target: nodes[i].id, kind: 'session' })
      }
    }
    const typeCount = new Map<string, number>()
    for (const n of nodes) typeCount.set(n.memoryType, (typeCount.get(n.memoryType) ?? 0) + 1)
    const types = [...typeCount.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count)

    return {
      nodes,
      links,
      stats: {
        nodes: nodes.length,
        links: links.length,
        sessions: 4,
        clusters: 4,
        orphans: 0,
        truncated: false,
        types,
      },
    }
  },

  'GET /api/dashboard/graph/memory': ({ query }) => {
    const id = str(query.get('id'))
    const row = db.memories.find((m) => m.id === id)
    return { id, memory: str(row?.memoryText || row?.content, '记忆全文内容') }
  },

  /* ------------------------------------------ 本控制台的扩展 */

  'GET /api/meta': () => ({
    mode: 'sample',
    // 控制台自带的示例后端；接真后端时由对方返回 mode=live
    service: '',
    health: null,
    keyPrefix: KEY_PREFIX,
    tools: TOOLS,
    // 打开鉴权，页面才有登录主体和项目角色可读；身份由 mock 直接给出，不校验凭据
    authEnabled: true,
    authMode: 'external',
  }),

  /* ------------------------------------------------------------ 登录主体 */

  'GET /api/auth/me': () => {
    const enabled = db.projects.find((p) => p.status === 'ENABLED') ?? db.projects[0]
    return {
      accountId: 'acct_demo',
      username: 'demo',
      displayName: '演示账号',
      platformAdmin: false,
      currentProjectId: enabled ? String(enabled.id) : '',
      projects: db.projects.map((p) => ({
        id: String(p.id),
        name: String(p.name),
        role: ROLE_BY_PROJECT[String(p.id)] ?? 'editor',
        member: true,
        kind: 'team',
      })),
    }
  },
}

function localSyncOff(): never {
  throw new HttpError(50300, 'local-sync 能力未启用', 503)
}

/** 平台指引进度：按项目存在内存里，状态迁移与控制面 service/onboarding 一致 */
interface OnboardingState {
  status: 'pending' | 'active' | 'dismissed' | 'completed'
  step: number
  revision: number
  version: number
  chat?: { stage: string; first: string; second?: string }
}
const onboarding = new Map<string, OnboardingState>()
function onboardingFor(projectId: string): OnboardingState {
  if (!projectId) throw new HttpError(40000, '缺少 projectId', 400)
  let state = onboarding.get(projectId)
  if (!state) {
    state = { status: 'pending', step: 0, revision: 0, version: 1 }
    onboarding.set(projectId, state)
  }
  return state
}

/** 演示用角色分配：默认项目可编辑，demo-agent 是只读账号视角，demo-user 可编辑。
 *  切换当前项目就等于切换角色，不需要改源码或额外登录。 */
const ROLE_BY_PROJECT: Record<string, 'viewer' | 'editor'> = {
  proj_default: 'editor',
  proj_alpha: 'viewer',
  proj_beta: 'editor',
}

/** 模板接口里这些键是元信息，其余键都当成分组数据 */
const META_KEYS = new Set(['templateId', 'templateName', 'projectId', 'bindingCount', 'updatedAt', 'id'])

/* ---------------------------------------------------------- 出参整形 */

function publicKey(row: Row) {
  // mock 与真实列表契约一致：列表只返回掩码，复制时才读取单条测试密钥。
  const raw = str(row.apiKey)
  return {
    id: row.id,
    keyName: row.keyName,
    apiKey: raw ? `${raw.slice(0, 8)}…${raw.slice(-4)}` : '',
    canCopy: Boolean(raw),
    createTime: row.createTime,
  }
}

function listLog(row: Row) {
  return {
    id: row.id,
    createTime: row.createTime,
    eventType: row.eventType,
    userIds: row.userIds,
    agentIds: row.agentIds,
    status: row.status,
    duration: row.duration,
  }
}

function publicMemory(row: Row) {
  const out: Record<string, unknown> = {
    memoryId: row.memoryId,
    memoryContent: row.memoryContent,
    subjectType: row.subjectType,
    externalUserId: row.externalUserId,
    createTime: row.createTime,
  }
  for (const key of ['memoryKey', 'memoryType', 'status', 'tags', 'updateTime']) if (row[key] !== undefined) out[key] = row[key]
  if (row.profileTemplateId !== undefined) out.profileTemplateId = row.profileTemplateId
  if (row.profileInstanceId !== undefined) out.profileInstanceId = row.profileInstanceId
  return out
}

class HttpError extends Error {
  code: number
  /** HTTP 状态码。提示词接口要区分 400/404/409/413/429；其余接口沿用 200 + code */
  status: number
  /** 错误响应里的 data：冲突带当前版本号，限流带等待秒数 */
  data: unknown

  constructor(code: number, message: string, status = 200, data: unknown = null) {
    super(message)
    this.code = code
    this.status = status
    this.data = data
  }
}

/* -------------------------------------------------------------- 分发 */

/** 'POST /api/keys/revoke/:id' 与 '/api/keys/revoke/key_ab' 匹配，返回 { id: 'key_ab' } */
function match(routeKey: string, method: string, path: string): Record<string, string> | null {
  const [rm, ...rparts] = routeKey.split(' ')
  if (rm !== method) return null
  const rpath = rparts.join(' ').split('/')
  const ppath = path.split('/')
  if (rpath.length !== ppath.length) return null
  const params: Record<string, string> = {}
  for (let i = 0; i < rpath.length; i++) {
    if (rpath[i].startsWith(':')) params[rpath[i].slice(1)] = decodeURIComponent(ppath[i])
    else if (rpath[i] !== ppath[i]) return null
  }
  return params
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', `http://localhost:${PORT}`)
  const method =
    req.method === 'GET'
      ? 'GET'
      : req.method === 'PUT'
        ? 'PUT'
        : req.method === 'DELETE'
          ? 'DELETE'
          : req.method === 'OPTIONS'
            ? 'OPTIONS'
            : 'POST'

  if (req.method === 'OPTIONS') return json(res, 204, null)

  // SSE 长连接：不走 JSON 信封，也不能等 readBody
  if (method === 'GET' && url.pathname === '/api/chat/stream') {
    return handleChatStream(res, url.searchParams)
  }

  // 退出登录：真实后端清会话后跳回首页，mock 没有会话，直接跳回去
  if (url.pathname === '/__logout') {
    res.writeHead(302, { Location: '/' })
    return res.end()
  }

  const body = await readBody(req)

  // 202 受理后由 streamAssistantReply 往同一会话的 SSE 推事件
  if (method === 'POST' && url.pathname === '/api/chat/messages') {
    try {
      const projectId = str(body.projectId)
      const text = str(body.text).trim()
      if (!projectId) throw new HttpError(40000, '缺少 projectId')
      if (!text) throw new HttpError(40000, '消息不能为空')
      if (!db.projects.some((p) => p.id === projectId)) throw new HttpError(40001, '项目不存在')

      const session = ensureChatSession(projectId)
      if (session.running) throw new HttpError(40900, '上一轮还在进行中')
      session.messages.push({ role: 'user', content: text, timestamp: Date.now() })
      accepted(res, { ok: true })
      void streamAssistantReply(projectId, text)
      return
    } catch (e) {
      if (e instanceof HttpError) return fail(res, e.code, e.message)
      console.error('[POST /api/chat/messages]', e)
      return fail(res, 50000, e instanceof Error ? e.message : '内部错误', 500)
    }
  }

  for (const [key, handler] of Object.entries(ROUTES)) {
    const params = match(key, method, url.pathname)
    if (!params) continue
    try {
      return ok(res, handler({ body, query: url.searchParams, params, url }))
    } catch (e) {
      if (e instanceof HttpError) return fail(res, e.code, e.message, e.status, e.data)
      console.error(`[${key}]`, e)
      return fail(res, 50000, e instanceof Error ? e.message : '内部错误', 500)
    }
  }

  fail(res, 40001, `接口不存在：${method} ${url.pathname}`, 404)
})

server.on('error', (e: NodeJS.ErrnoException) => {
  if (e.code === 'EADDRINUSE') {
    console.error(`端口 ${PORT} 已被占用。换一个：MOCK_PORT=5281 pnpm mock`)
    process.exit(1)
  }
  throw e
})

server.listen(PORT, () => {
  const tables = Object.keys(COLLECTIONS).length
  console.log(`mock 后端在 http://localhost:${PORT}（${Object.keys(ROUTES).length} 个接口，${tables} 张表，落盘 ${DB_FILE}）`)
})
