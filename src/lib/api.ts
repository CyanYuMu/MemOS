/** 接口封装，权威定义见仓库根目录 openapi.json。换真后端时前端不动。 */

import { z } from 'zod'
import { API_BASE } from './config'
import type { TKey } from './i18n'
import type {
  Account,
  OnboardingAction,
  OnboardingCheckpoint,
  OnboardingProgress,
  AdminAccount,
  ApiKey,
  ChatApiKeyRef,
  ChatConfig,
  ChatEvent,
  ChatEventType,
  ChatMessage,
  ChatMessagesResponse,
  ChatState,
  ChatSubmission,
  DataSource,
  GraphLink,
  GraphLinkKind,
  GraphNode,
  CreatedInvite,
  Job,
  KnowledgeBase,
  KnowledgeFile,
  LocalDevice,
  LocalMemory,
  LocalMemoryDetail,
  LocalMemoryLayer,
  LocalSyncQuota,
  LocalSyncResult,
  LocalSyncSummary,
  LogDetail,
  MemberCandidate,
  Memory,
  MemoryActivity,
  MemoryEvent,
  MemoryLogActivity,
  MemoryGraph,
  Meta,
  ModelRecord,
  ProfileTemplate,
  Project,
  ProjectInvite,
  ProjectMember,
  Records,
  RequestLog,
  ToolInfo,
  UsageDailyStat,
} from '../../shared/contract'

/** 页面照旧从 `lib/api` 取这些类型；定义只有 `shared/contract.ts` 一份 */
export type {
  Account,
  OnboardingAction,
  OnboardingCheckpoint,
  OnboardingProgress,
  AdminAccount,
  ApiKey,
  ChatApiKeyRef,
  ChatConfig,
  ChatEvent,
  ChatEventType,
  ChatMessage,
  ChatMessagesResponse,
  ChatState,
  ChatSubmission,
  DataSource,
  GraphLink,
  GraphLinkKind,
  GraphNode,
  CreatedInvite,
  Job,
  KnowledgeBase,
  KnowledgeFile,
  LocalDevice,
  LocalMemory,
  LocalMemoryDetail,
  LocalMemoryLayer,
  LocalSyncQuota,
  LocalSyncResult,
  LocalSyncSummary,
  LogDetail,
  MemberCandidate,
  Memory,
  MemoryActivity,
  MemoryEvent,
  MemoryLogActivity,
  MemoryGraph,
  Meta,
  ModelRecord,
  ProfileTemplate,
  Project,
  ProjectInvite,
  ProjectMember,
  Records,
  RequestLog,
  ToolInfo,
  UsageDailyStat,
}

/* ------------------------------------------------------------ 请求层 */

export class ApiError extends Error {
  status: number
  code?: number
  /** **本地生成**的错误：文案键 + 插值参数，展示时按当前语言取词（见 `errText`）。
   *
   *  ⚠️ 上游返回的 message 不带它，那条路原样显示服务端原文，不翻译。
   *  所以这里的 `message` 只是一句给日志看的英文/技术描述，**不是给用户看的文案**。 */
  msg?: { key: TKey; args: unknown[] }
  /** 错误响应里的 `data`。版本冲突要带当前版本号、限流要带等待秒数，都从这里取 */
  data?: unknown

  constructor(message: string, status: number, code?: number, msg?: { key: TKey; args: unknown[] }, data?: unknown) {
    super(message)
    this.status = status
    this.code = code
    this.msg = msg
    this.data = data
  }
}

interface CallOpts {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Record<string, unknown>
}

const qs = (params: Record<string, unknown>) => {
  const u = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') u.set(k, String(v))
  const s = u.toString()
  return s ? `?${s}` : ''
}

/** 发一次请求并解包。code === 0 返回 data，否则抛 ApiError。 */
async function call<T>(path: string, opts: CallOpts = {}): Promise<T> {
  const { method = 'POST', body, query } = opts
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}${query ? qs(query) : ''}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    // 唯一没有 HTTP 状态码的失败路径：BFF 正在重启（Recreate 部署会短暂没有 Pod）、
    // DNS 解析失败、连接被重置。用 status=0 标出来，`useAsync` 据此重试。
    // 其它错误都走下面的 HTTP 分支，带着真实状态码。
    throw new ApiError('network error', 0, undefined, { key: 'error.network', args: [] })
  }

  const text = await res.text()
  let parsed: unknown
  try {
    parsed = text ? JSON.parse(text) : null
  } catch {
    // 413 常见于网关直接挡下（返回 HTML），这时按「内容过长」说，比「不是 JSON」可操作
    throw new ApiError(
      `HTTP ${res.status}: response is not JSON`,
      res.status,
      undefined,
      res.status === 413 ? { key: 'error.tooLarge', args: [] } : { key: 'error.notJson', args: [res.status] },
    )
  }

  const env = parsed as { code?: number; data?: unknown; message?: string } | null
  // 上游给了 message 就用它（服务端原文，不翻译）；没给才回落到本地文案。
  // 例外：413 的「请求体过大」没有可操作信息，换成本地文案；带具体上限数字的保留服务端原文。
  const tooLarge = res.status === 413 || env?.code === 41300
  const keepServerMessage = Boolean(env?.message) && !(tooLarge && !/\d/.test(env?.message ?? ''))
  if (!res.ok) {
    throw new ApiError(
      env?.message || `HTTP ${res.status}`,
      res.status,
      env?.code,
      keepServerMessage ? undefined : (localMsgFor(env?.code, res.status) ?? { key: 'error.requestFailed', args: [res.status] }),
      env?.data,
    )
  }
  if (env && typeof env.code === 'number' && env.code !== 0) {
    const tooLargeCode = env.code === 41300
    const keepMsg = Boolean(env.message) && !(tooLargeCode && !/\d/.test(env.message ?? ''))
    throw new ApiError(
      env.message || `code=${env.code}`,
      res.status,
      env.code,
      keepMsg ? undefined : (localMsgFor(env.code, res.status) ?? { key: 'error.code', args: [env.code] }),
      env.data,
    )
  }
  return (env && 'data' in env ? env.data : parsed) as T
}

/** 信封错误码 → 本地文案。只有模型失败与超时两个码需要专属说法（控制面用 50200 / 50400，
 *  见 spec 第 7 节），其余仍按 HTTP 状态码兜底。上游带了 message 时不走这里，原样显示。 */
function localMsgFor(code?: number, status?: number): { key: TKey; args: unknown[] } | undefined {
  // 输入过长：控制面统一 413（信封码 41300）。要和 400 的普通校验失败分开说清楚
  if (status === 413 || code === 41300) return { key: 'error.tooLarge', args: [] }
  if (code === 50200) return { key: 'error.llmError', args: [] }
  if (code === 50400) return { key: 'error.llmTimeout', args: [] }
  return undefined
}

/** 发请求并校验响应形状。形状对不上说明前后端版本不一致，直接报错而不是让页面带着
 *  `undefined` 渲染成「暂无数据」——那种空屏在日志里看不出任何异常。 */
async function callValidated<T>(schema: z.ZodType<T>, path: string, opts: CallOpts = {}): Promise<T> {
  const data = await call<unknown>(path, opts)
  const parsed = schema.safeParse(data)
  if (!parsed.success) {
    throw new ApiError(`response does not match contract: ${parsed.error.message}`, 200, undefined, {
      key: 'error.contract',
      args: [],
    })
  }
  return parsed.data
}

/* -------------------------------------------------------------- 类型 */

/* 传输形状在 `shared/contract.ts`，这里只留**页面查询参数**。 */

/** 调用日志的事件类型。与后端 6 个网关工具一一对应；未知值后端返回 400。 */
export const EVENT_TYPES = ['addMessage', 'searchMemory', 'getMemory', 'updateMemory', 'deleteMemory', 'addFeedback'] as const

export interface MemoryListQuery {
  projectId: string
  memoryDetailType: string
  pageNum?: number
  pageSize?: number
  knowledgeBaseId?: string
  startTime?: string
  endTime?: string
  externalUserId?: string
  subjectType?: string
  memSearchContent?: string
}

export interface LogListQuery {
  projectId: string
  pageNum?: number
  pageSize?: number
  /** all（默认）= 网关调用 + 控制台操作；gateway 只看 6 个记忆工具；console 只看控制台操作 */
  scope?: 'all' | 'gateway' | 'console'
  subjectId?: string
  subjectType?: string
  eventType?: string
  /** 调用入口：mcp / rest / dashboard / admin / auth；空值不过滤 */
  source?: string
  query?: string
  status?: string
  startTime?: string
  endTime?: string
}

/* ------------------------------------------------ 提示词工作台的接口形状
 *
 *  与控制面的约定见 `docs/tasks/project-prompts/spec.md` 第 6、7 节：信封字段是 camelCase，
 *  但以下内容原样透传、不做键名转换：
 *    注册项的 `inputSchema`（JSON Schema 的关键字）、`sampleInputs[].input`、
 *    `runs[].output`，以及 `defaults` / `effective` 里以语言代码为键的那一层。
 */

import {
  promptDetailSchema,
  promptEffective,
  promptGenerateResponse,
  promptIssue,
  promptListItem,
  promptListResponse,
  promptOutputContract,
  promptPlaceholder,
  promptRegistry,
  promptResetResponse,
  promptRun,
  promptSampleInput,
  promptSaveResponse,
  promptSlotState,
  promptTestResponse,
  promptValidation,
  promptVersion,
  promptVersionsResponse,
} from './promptSchemas'
export type PromptGroup = 'extract' | 'process' | 'dream'
export type PromptSource = 'default' | 'project'
export type PromptLang = 'zh' | 'en'
export type PromptSlotState = z.infer<typeof promptSlotState>
export type PromptEffective = z.infer<typeof promptEffective>
export type PromptListItem = z.infer<typeof promptListItem>
export type PromptListResponse = z.infer<typeof promptListResponse>
export type PromptPlaceholder = z.infer<typeof promptPlaceholder>
export type PromptOutputContract = z.infer<typeof promptOutputContract>
export type PromptSampleInput = z.infer<typeof promptSampleInput>
export type PromptRegistry = z.infer<typeof promptRegistry>
export type PromptDetail = z.infer<typeof promptDetailSchema>
export type PromptVersion = z.infer<typeof promptVersion>
export type PromptVersionsResponse = z.infer<typeof promptVersionsResponse>
export type PromptIssue = z.infer<typeof promptIssue>
export type PromptValidation = z.infer<typeof promptValidation>
export type PromptSaveResponse = z.infer<typeof promptSaveResponse>
export type PromptResetResponse = z.infer<typeof promptResetResponse>
export type PromptRun = z.infer<typeof promptRun>
export type PromptTestResponse = z.infer<typeof promptTestResponse>
export type PromptGenerateResponse = z.infer<typeof promptGenerateResponse>

export interface PromptSaveRequest {
  projectId: string
  key: string
  lang: string
  /** 与 `restoreFromVersionId` 二选一 */
  body?: string
  restoreFromVersionId?: number
  note?: string
  /** 客户端上次看到的当前版本；默认状态为 0 */
  baseVersionId?: number
}

export interface PromptTestRequest {
  projectId: string
  key: string
  lang: string
  /** 草稿；缺省用当前生效值 */
  body?: string
  input: unknown
  compareDefault?: boolean
}

export interface PromptGenerateRequest {
  projectId: string
  key: string
  lang: string
  instruction: string
  baseBody?: string
}

/** 版本冲突（409）响应里的当前版本。优先读 `data.current`（含 versionNo），
 *  老形状只给 `data.currentVersionId`。 */
export function conflictCurrent(e: unknown): { versionId: number | null; versionNo: number | null } {
  const data = (e as { data?: unknown } | null)?.data
  if (!data || typeof data !== 'object') return { versionId: null, versionNo: null }
  const rec = data as { current?: { versionId?: unknown; versionNo?: unknown }; currentVersionId?: unknown }
  const vid =
    typeof rec.current?.versionId === 'number'
      ? rec.current.versionId
      : typeof rec.currentVersionId === 'number'
        ? rec.currentVersionId
        : null
  const vno = typeof rec.current?.versionNo === 'number' ? rec.current.versionNo : null
  return { versionId: vid, versionNo: vno }
}

/** 429 响应里带的等待秒数，没有就返回 null */
export function retryAfterSec(e: unknown): number | null {
  const data = (e as { data?: unknown } | null)?.data
  if (!data || typeof data !== 'object') return null
  const v = (data as { retryAfterSec?: unknown }).retryAfterSec
  return typeof v === 'number' ? v : null
}

/** 本地记忆镜像的列表查询。时间作用于 `sourceUpdatedAt`，后端接受 RFC3339 或
 *  YYYY-MM-DD；空值不发出。 */
export interface LocalMemoryListQuery {
  projectId: string
  layer?: string
  deviceId?: string
  q?: string
  startTime?: string
  endTime?: string
  page?: number
  pageSize?: number
}

export const api = {
  onboarding: {
    get: (projectId: string) => call<OnboardingProgress>('/api/auth/onboarding', { method: 'GET', query: { projectId } }),
    update: (projectId: string, action: OnboardingAction, revision: number, checkpoint?: OnboardingCheckpoint) =>
      call<OnboardingProgress>('/api/auth/onboarding', { method: 'PUT', body: { projectId, action, revision, checkpoint } }),
  },
  auth: {
    me: () => call<Account>('/api/auth/me', { method: 'GET' }),
  },

  admin: {
    accounts: {
      list: (query = '', page = 1, pageSize = 20) =>
        call<Records<AdminAccount>>('/admin/accounts', { method: 'GET', query: { query, page, pageSize } }),
      update: (id: string, body: { display_name?: string; disabled?: boolean; platform_admin?: boolean }) =>
        call<AdminAccount>(`/admin/accounts/${encodeURIComponent(id)}`, { body }),
      /** 删除账号：个人项目与成员关系一起删；团队 Key/邀请保留但清空创建者 */
      remove: (id: string) => call<{ ok: boolean }>(`/admin/accounts/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    },
    members: {
      list: (tenantId: string) =>
        call<{ records: ProjectMember[]; total: number }>(`/admin/tenants/${encodeURIComponent(tenantId)}/members`, {
          method: 'GET',
        }),
      set: (tenantId: string, body: { account_id?: string; username?: string; role: string }) =>
        call<{ account_id: string; tenant_id: string; role: string }>(`/admin/tenants/${encodeURIComponent(tenantId)}/members`, { body }),
      remove: (tenantId: string, accountId: string) =>
        call<unknown>(`/admin/tenants/${encodeURIComponent(tenantId)}/members/${encodeURIComponent(accountId)}/remove`, {
          body: {},
        }),
      /** 自己退出项目；不需要管理权限，最后一个 owner 会被后端拒绝 */
      leave: (tenantId: string) => call<{ ok: boolean }>(`/admin/tenants/${encodeURIComponent(tenantId)}/members/leave`, { body: {} }),
      /** `@` 搜索可加入的人；只有项目 owner 能调 */
      candidates: (tenantId: string, query: string) =>
        call<Records<MemberCandidate>>(`/admin/tenants/${encodeURIComponent(tenantId)}/member-candidates`, {
          method: 'GET',
          query: { query },
        }),
    },
    invites: {
      list: (tenantId: string) => call<Records<ProjectInvite>>(`/admin/tenants/${encodeURIComponent(tenantId)}/invites`, { method: 'GET' }),
      create: (tenantId: string, body: { role: string; expiresIn?: string }) =>
        call<CreatedInvite>(`/admin/tenants/${encodeURIComponent(tenantId)}/invites`, { body }),
      revoke: (tenantId: string, inviteId: string) =>
        call<unknown>(`/admin/tenants/${encodeURIComponent(tenantId)}/invites/${encodeURIComponent(inviteId)}/revoke`, {
          body: {},
        }),
      setRole: (tenantId: string, inviteId: string, role: string) =>
        call<{ ok: boolean; role: string }>(`/admin/tenants/${encodeURIComponent(tenantId)}/invites/${encodeURIComponent(inviteId)}/role`, {
          body: { role },
        }),
    },
  },

  project: {
    dict: (language: string) => call<Project[]>('/api/dashboard/project/dict', { method: 'GET', query: { language } }),
    list: (pageNum = 1, pageSize = 10) => call<Records<Project>>('/api/dashboard/project/list', { body: { pageNum, pageSize } }),
    create: (body: { name: string; description: string; isMultiView: 0 | 1 }) => call<Project>('/api/dashboard/project/create', { body }),
    update: (body: Partial<Project> & { id: string; current?: boolean }) => call<Project>('/api/dashboard/project/update', { body }),
    /** 切换当前项目：带 current=true，与「启用被停用的项目」（status=ENABLED）分开 */
    revoke: (id: string) => call<unknown>(`/api/dashboard/project/revoke/${encodeURIComponent(id)}`),
    /** 删除团队项目：归档（软删除），可在项目列表恢复 */
    remove: (id: string) => call<{ ok: boolean; id: string; archived: boolean }>(`/api/dashboard/project/delete/${encodeURIComponent(id)}`),
    restore: (id: string) => call<Project>(`/api/dashboard/project/restore/${encodeURIComponent(id)}`),
  },

  /** 对话页。流式事件不走 `call`，见 `ChatSession` 里的 EventSource。 */
  chat: {
    conversations: (projectId: string, offset = 0) =>
      call<Records<ChatConversation>>('/api/chat/conversations', { method: 'GET', query: { projectId, offset } }),
    newConversation: (projectId: string, title = '') => call<ChatConversation>('/api/chat/conversations', { body: { projectId, title } }),
    feedback: (projectId: string, responseId: string) =>
      call<{ vote: number; comment?: string }>('/api/chat/feedback', { method: 'GET', query: { projectId, responseId } }),
    rate: (
      projectId: string,
      responseId: string,
      vote: number,
      details?: { comment: string; conversationId: string; answerExcerpt: string },
    ) => call<{ vote: number }>('/api/chat/feedback', { method: 'PUT', body: { projectId, responseId, vote, ...details } }),
    state: (projectId: string) => call<ChatState>('/api/chat/state', { method: 'GET', query: { projectId } }),
    config: (projectId: string) => call<ChatConfig>('/api/chat/config', { method: 'GET', query: { projectId } }),
    /** apiKey 为空串表示保留已保存的上游密钥 */
    /** `before` 是上一页的 `start`；不传取最后 `limit` 条 */
    messages: (projectId: string, limit = 10, before?: number, conversationId = '') =>
      call<ChatMessagesResponse>('/api/chat/messages', { method: 'GET', query: { projectId, limit, before, conversationId } }),
    send: (projectId: string, text: string, conversationId = '') =>
      call<ChatSubmission>('/api/chat/messages', { body: { projectId, text, conversationId } }),
    retry: (projectId: string, conversationId = '') =>
      call<ChatSubmission>('/api/chat/retry', { method: 'POST', body: { projectId, conversationId } }),
    abort: (projectId: string, conversationId = '') =>
      call<unknown>('/api/chat/abort', { method: 'POST', body: { projectId, conversationId } }),
  },

  keys: {
    list: (projectId: string, pageNum = 1, pageSize = 10) =>
      call<Records<ApiKey>>('/api/dashboard/keys/list', { body: { projectId, pageNum, pageSize } }),
    create: (projectId: string, keyName: string) => call<ApiKey>('/api/dashboard/keys/create', { body: { projectId, keyName } }),
    revoke: (apiKeyId: string) => call<unknown>(`/api/dashboard/keys/revoke/${encodeURIComponent(apiKeyId)}`),
    rename: (apiKeyId: string, keyName: string) =>
      call<ApiKey>(`/api/dashboard/keys/rename/${encodeURIComponent(apiKeyId)}`, { body: { keyName } }),
    /**
     * 按 id 取明文，**只在用户点「复制」时调**：列表接口只给前缀掩码，明文不批量下发。
     * 需要项目写权限；迁移前签发的历史 Key 没有明文，会返回 409。
     */
    copy: (apiKeyId: string) => call<{ apiKey: string }>('/api/dashboard/keys/copy', { body: { apiKeyId } }),
  },

  log: {
    list: (q: LogListQuery) => {
      // 空值不发出去
      const body: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') body[k] = v
      return call<{ logs: RequestLog[]; total: number }>('/api/dashboard/log/list', { body })
    },
    detail: (logId: string) => call<LogDetail>('/api/dashboard/log/detail', { body: { logId } }),
  },

  jobs: {
    /** 平台管理员：死信排查。status 为空表示不过滤 */
    list: (q: { status?: string; kind?: string; limit?: number; offset?: number }) =>
      call<Records<Job>>('/admin/jobs', { method: 'GET', query: q }),
    /**
     * 重放一条失败任务。`force` 只在任务失败在「可能已写入上游」的位置时才需要
     * （后端按 last_error 判，不安全时返回 409）——上游没有幂等键，重放可能写出
     * 第二条记忆，所以这个开关必须由人显式勾。
     */
    retry: (id: string, force = false) =>
      call<{ ok: boolean; job: Job }>(`/admin/jobs/${encodeURIComponent(id)}/retry`, { body: { force } }),
  },

  memory: {
    list: (q: MemoryListQuery) => {
      const body: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') body[k] = v
      return call<{ memListItemResponseList: Memory[]; total: number; current: number }>('/api/dashboard/project/memory/list', { body })
    },
    detail: (projectId: string, memoryId: string, memType: string) =>
      call<Memory>('/api/dashboard/project/memory/detail', { body: { projectId, memoryId, memType } }),
    events: (projectId: string, after?: string, callIds?: string[]) =>
      call<Records<MemoryEvent> & { cursor: string; hasMore: boolean }>('/api/dashboard/project/memory/events', {
        body: { projectId, after, callIds },
      }),
    activity: (projectId: string, callIds: string[] = [], after?: string) =>
      call<Records<MemoryActivity> & { cursor: string }>('/api/dashboard/project/memory/activity', { body: { projectId, callIds, after } }),
    remove: (projectId: string, memory_ids: string[], memType: string) =>
      call<unknown>('/api/dashboard/project/memory/delete', { body: { projectId, memory_ids, memType } }),
  },

  /** 本地插件上传的记忆镜像。只读副本：删除只删云端副本，转存才进入云端检索面。 */
  localSync: {
    devices: (projectId: string) =>
      call<{ devices: LocalDevice[]; total: number; quota: LocalSyncQuota }>('/api/dashboard/local-sync/devices', {
        method: 'GET',
        query: { projectId },
      }),
    revokeDevice: (projectId: string, id: string) =>
      call<unknown>(`/api/dashboard/local-sync/devices/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        query: { projectId },
      }),
    memories: (q: LocalMemoryListQuery) =>
      call<{ records: LocalMemory[]; total: number; pageNum: number; pageSize: number }>('/api/dashboard/local-sync/memories', {
        method: 'GET',
        query: { ...q },
      }),
    /** 治理视图：分层计数、转存状态与时间范围，不含正文 */
    summary: (projectId: string) =>
      call<LocalSyncSummary>('/api/dashboard/local-sync/summary', {
        method: 'GET',
        query: { projectId },
      }),
    memory: (projectId: string, id: string) =>
      call<LocalMemoryDetail>(`/api/dashboard/local-sync/memories/${encodeURIComponent(id)}`, {
        method: 'GET',
        query: { projectId },
      }),
    removeMemory: (projectId: string, id: string) =>
      call<unknown>(`/api/dashboard/local-sync/memories/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        query: { projectId },
      }),
    /** 单条与批量共用；失败逐条返回真实原因，已入队的不会回滚 */
    promote: (projectId: string, ids: string[]) =>
      call<LocalSyncResult>('/api/dashboard/local-sync/memories/promote', {
        method: 'POST',
        query: { projectId },
        body: { ids },
      }),
  },

  knowledgeBase: {
    list: (projectId: string, pageNum = 1, pageSize = 10, name = '') =>
      call<Records<KnowledgeBase>>('/api/dashboard/knowledgebase/list', { body: { projectId, pageNum, pageSize, name } }),
    create: (projectId: string, knowledgebaseName: string, knowledgebaseDescription: string) =>
      call<KnowledgeBase>('/api/dashboard/knowledgebase/create', { body: { projectId, knowledgebaseName, knowledgebaseDescription } }),
    update: (projectId: string, id: string, body: { knowledgebaseName?: string; knowledgebaseDescription?: string }) =>
      call<KnowledgeBase>('/api/dashboard/knowledgebase/update', { body: { projectId, id, ...body } }),
    detail: (projectId: string, knowledgeBaseId: string) =>
      call<KnowledgeBase>('/api/dashboard/knowledgebase/detail', { body: { projectId, knowledgeBaseId } }),
    remove: (projectId: string, knowledgeBaseId: string) =>
      call<{ ok: boolean; id: string }>('/api/dashboard/knowledgebase/delete', { body: { projectId, knowledgeBaseId } }),
    fileList: (projectId: string, knowledgeBaseId: string, pageNum = 1, pageSize = 10) =>
      call<Records<KnowledgeFile>>('/api/dashboard/knowledgebase/file/list', { body: { projectId, knowledgeBaseId, pageNum, pageSize } }),
    fileAdd: (projectId: string, knowledgeBaseId: string, file: { name: string; content: string; type?: string }) =>
      call<Records<KnowledgeFile>>('/api/dashboard/knowledgebase/file/add', { body: { projectId, knowledgeBaseId, file } }),
    fileRemove: (projectId: string, fileIds: string[]) =>
      call<{ ok: boolean; deleted: number }>('/api/dashboard/knowledgebase/file/delete', { body: { projectId, fileIds } }),
    fileDownloadUrl: (projectId: string, fileId: string) =>
      `${API_BASE}/api/dashboard/knowledgebase/file/download?projectId=${encodeURIComponent(projectId)}&fileId=${encodeURIComponent(fileId)}`,
    memoryList: (projectId: string, knowledgeBaseId: string, pageNum = 1, pageSize = 10) =>
      call<Records<Memory>>('/api/dashboard/knowledgebase/memory/list', { body: { projectId, knowledgeBaseId, pageNum, pageSize } }),
  },

  /** 画像模板。注意这个接口的列表键是 list，不是 records */
  profileTemplate: {
    list: (pageNum = 1, pageSize = 10, projectId = '') =>
      call<{ list: ProfileTemplate[]; total: number; current: number; size: number; pages: number }>(
        '/api/dashboard/profileTemplate/list',
        {
          body: { pageNum, pageSize, projectId },
        },
      ),
    detail: (templateId: string, projectId: string) =>
      call<ProfileTemplate>('/api/dashboard/profileTemplate/detail', { method: 'GET', query: { templateId, projectId } }),
    create: (body: Record<string, unknown>) => call<unknown>('/api/dashboard/profileTemplate/create', { body }),
    update: (body: Record<string, unknown>) => call<unknown>('/api/dashboard/profileTemplate/update', { body }),
    remove: (projectId: string, templateId: string) =>
      call<unknown>('/api/dashboard/profileTemplate/delete', { body: { projectId, templateId } }),
  },

  usage: {
    /** projectId 传 "all" 时前端改写成空串再发出，后端按全部处理 */
    query: (q: { projectId?: string; apiKeyId?: string; startDate: string; endDate: string }) =>
      call<{ dailyStats: UsageDailyStat[] }>('/api/dashboard/usage/query', { body: { ...q, includeDailyDetails: true } }),
  },

  promptConfig: {
    detail: (body: Record<string, unknown>) => call<Record<string, unknown>>('/api/dashboard/prompt-config/detail', { body }),
    save: (body: Record<string, unknown>) => call<unknown>('/api/dashboard/prompt-config/save', { body }),
    generate: (body: Record<string, unknown>) => call<Record<string, unknown>>('/api/dashboard/prompt-config/generate', { body }),
    resetDefault: (body: Record<string, unknown>) => call<Record<string, unknown>>('/api/dashboard/prompt-config/reset-default', { body }),
    debugExtract: (body: Record<string, unknown>) => call<Record<string, unknown>>('/api/dashboard/prompt-config/debug/extract', { body }),
  },

  /** 提示词工作台。控制面只代理上游，返回形状见 `docs/tasks/project-prompts/spec.md` 第 7 节 */
  prompts: {
    list: (projectId: string) => callValidated(promptListResponse, '/api/dashboard/prompts/list', { body: { projectId } }),

    detail: (projectId: string, key: string) =>
      callValidated(promptDetailSchema, '/api/dashboard/prompts/detail', { body: { projectId, key } }),

    versions: (body: { projectId: string; key: string; lang: string; limit?: number; beforeVersionId?: number }) =>
      callValidated(promptVersionsResponse, '/api/dashboard/prompts/versions', { body }),

    validate: (body: { projectId: string; key: string; lang: string; body: string }) =>
      callValidated(promptValidation, '/api/dashboard/prompts/validate', { body }),

    save: (body: PromptSaveRequest) => callValidated(promptSaveResponse, '/api/dashboard/prompts/save', { body }),

    reset: (body: { projectId: string; key?: string; lang?: string }) =>
      callValidated(promptResetResponse, '/api/dashboard/prompts/reset', { body }),

    test: (body: PromptTestRequest) => callValidated(promptTestResponse, '/api/dashboard/prompts/test', { body }),

    generate: (body: PromptGenerateRequest) => callValidated(promptGenerateResponse, '/api/dashboard/prompts/generate', { body }),
  },

  user: {
    quotaSummary: (knowledgebaseId?: string) =>
      call<Record<string, unknown>>('/api/user/quota/summary', { method: 'GET', query: { knowledgebaseId } }),
    subscriptionDetails: () =>
      call<{ cycles?: { id: string; cycleStartTime?: string; cycleEndTime?: string }[]; currentCycle?: { id: string } }>(
        '/api/user/subscription/details',
        { body: { cycleId: '', includeAllCycles: true } },
      ),
  },

  /* -------------------------------------------- 以下是本控制台的扩展 */

  /** 记忆星座图。**GET**：只读视图，参数放 query，浏览器能直接点开、能 curl 重放排障。
   *  它不是 openapi.json 里的接口，是展示层自己加的。 */
  graph: {
    memories: (q: { tenant?: string; types?: string; limit?: number }) =>
      call<MemoryGraph>('/api/dashboard/graph/memories', { method: 'GET', query: q }),
    /** 单条记忆的**全文**（抽屉里的「展开全文」）。与节点同源，都来自图库——
     *  洞察记忆在上游 REST 里查不到（`get_memory` 回 not found），所以不能走那条。 */
    memory: (id: string, tenant?: string) =>
      call<{ id: string; memory: string }>('/api/dashboard/graph/memory', { method: 'GET', query: { id, tenant } }),
  },

  /** 模型清单。读的是**上游 Pod 真正在读的那份 ConfigMap**，不是前端的静态副本——
   *  之前那页读 `public/tech-config/*.json`，模型名是编的（用户 2026-10-19 看出来了）。 */
  models: {
    list: () => call<{ records: ModelRecord[] }>('/api/dashboard/models', { method: 'GET' }),
  },

  meta: () => call<Meta>('/api/meta', { method: 'GET' }),
}

export interface ChatConversation {
  id: string
  title: string
  createdAt: string
}
