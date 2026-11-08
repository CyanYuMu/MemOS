/** 前后端共用的接口契约。
 *
 *  这里是**唯一**的字段定义处：前端 `src/lib/api.ts` 直接再导出这些类型，后端
 *  按同一形状实现。以前两边各写一份（`RequestLog` vs
 *  `ConsoleLogRow`、`MemoryGraph` 两处、`ModelRecord` 两处），改一边忘另一边时
 *  TypeScript 不会报错，只有页面上少一列才看得出来。
 *
 *  ⚠️ 只放**跨进程传输的形状**。查询参数、页面内部状态、上游原始结构不要放进来。
 */

/* ------------------------------------------------------------ 信封 */

/** 前端统一从 `data` 里取；列表接口的 `data` 就是这个形状 */
export interface Records<T> {
  records: T[]
  total: number
  current?: number
  size?: number
}

/* ------------------------------------------------------------ Skill 管理（P3） */

/** candidate 保留 Python 规范的 snake_case；管理接口使用 camelCase。 */
export interface SkillRule {
  rule_id: string
  condition: string
  observation: string
  action: string
  expected_result: string
  basis: string
  branches: { when: string; target: string }[]
  evidence_links: Record<string, unknown>[]
  validation_status?: string
}

export interface SkillCandidate {
  skill_id: string
  revision: string
  title: string
  goal: string
  purpose: string
  tags: string[]
  applicability: string[]
  exclusions: string[]
  prerequisites: string[]
  constraints: string[]
  acceptance: string[]
  limitations: string[]
  entry_rule: string
  exits: Record<string, string>
  rules: SkillRule[]
  semantic_review: string
  independent_validation: string
  content_hash: string
  source_refs: Record<string, unknown>[]
  host_limits: string[]
  requires: unknown[]
}

export interface SkillReadiness {
  status: string
  reasons: string[]
}

export interface SkillSummary {
  skillId: string
  revision: string
  title: string
  purpose: string
  tags: string[]
  state: string
  sequence: number
  readiness: SkillReadiness
  ruleCount: number
  validationCount: number
}

export interface SkillManagement {
  state: string
  sequence: number
  readiness: SkillReadiness
  challenges: unknown[]
}

export interface SkillJob {
  jobId: string
  status: string
  skillId?: string
  revision?: string
  caseId?: string
  mode?: string
  taskResult?: { status: string; accepted: boolean; receiptMatches: boolean } | null
  skillUsage?: { loading: string; capture: string; usageReview: string; declaredRuleIds: string[]; endedAdmission?: string } | null
  validationId?: string | null
  errorCode?: string | null
}

export interface SkillDetail {
  projectId: string
  candidate: SkillCandidate
  management: SkillManagement
  validations: Record<string, unknown>[]
  jobs: SkillJob[]
  releases: Record<string, unknown>[]
  caseOptions: { caseId: string; title: string }[]
}

export interface SkillReviewPackage {
  projectId?: string
  packageId: string
  packageHash: string
  sequence: number
  skillId: string
  revision: string
  readiness: SkillReadiness
  validationIds: string[]
  sourceWatermark: string[]
  contentHash: string
  manifestId?: string
  exportHash?: string | null
  actor?: string
  preparedAt?: string
  reviewChecklist: { ruleId: string; branchCount: number; coveredBranches: string[] }[]
}

export interface SkillRef {
  projectId: string
  skillId: string
  revision: string
}

export interface SkillMutation extends SkillRef {
  expectedSequence: number
  operationId: string
  reason: string
}

export interface SkillMutationResult {
  state?: string
  sequence: number
  releaseId?: string
}

export interface SkillLoadPreview {
  allowed: boolean
  reasons?: string[]
  reason?: string
  errorCode?: string
  [key: string]: unknown
}

/* ------------------------------------------------------------ 项目 */

export interface Project {
  id: string
  name: string
  description?: string
  /** "ENABLED" 的那条是当前项目 */
  status?: string
  createTime?: string
  isMultiView?: number
  /** 当前账号在这个项目里的角色；项目列表接口才带 */
  role?: string
  /** 是否为真实成员（不含平台管理员的隐含权限）；预览普通用户视角用它裁项目 */
  member?: boolean
  /** 项目已被管理员停用；和「不是当前项目」的 status=DISABLED 不是一件事 */
  disabled?: boolean
  /** personal 是账号的个人空间，team 是普通项目 */
  kind?: 'team' | 'personal'
  /** 已归档（软删除）；项目列表接口会带出，可恢复 */
  archived?: boolean
  archivedAt?: string
}

/* ------------------------------------------------------------ 账号与成员 */

export interface AdminAccount {
  id: string
  username: string
  display_name: string
  email?: string
  platform_admin: boolean
  disabled: boolean
  created_at: string
  last_login_at?: string
}

export interface ProjectMember {
  account_id: string
  username: string
  display_name: string
  disabled: boolean
  role: string
  created_at: string
}

/** `@` 搜索的可加入候选；member 表示已在项目里 */
export interface MemberCandidate {
  accountId: string
  username: string
  displayName: string
  disabled: boolean
  member: boolean
}

/** 项目邀请链接。明文 token 只在创建响应里出现一次（path），列表不含 token */
export interface ProjectInvite {
  id: string
  tenantId: string
  role: string
  status: 'active' | 'used' | 'expired' | 'revoked'
  createdAt: string
  expiresAt: string
  usedAt?: string
  usedByAccountId?: string
  revokedAt?: string
}

export interface CreatedInvite {
  id: string
  tenantId: string
  role: string
  path: string
  createdAt: string
  expiresAt: string
}

/* ------------------------------------------------------------ 密钥 */

export interface ApiKey {
  id: string
  keyName: string
  /** 列表里**只有前缀掩码**（`memos_c8…`）；完整明文要按 id 调 `keys/copy` 取 */
  apiKey: string
  /**
   * 能不能复制：为真表示点「复制」时 `keys/copy` 会返回明文（项目写权限 + 库里留有明文）。
   * 历史密钥（迁移 0024 之前签发、旧网关导入）没有明文，为 false。
   */
  canCopy?: boolean
  createTime: string
  /** 上次被使用的时间；从没调用过就没有这个字段 */
  lastUsedAt?: string
}

/* ------------------------------------------------------------ 调用日志 */

/** 调用日志列表行 */
export interface RequestLog {
  id: string
  /** 异步 add 的请求行与后台终态行的关联 ID；同步/历史行为空 */
  callId?: string
  createTime: string
  /** 事件类型，见前端 `EVENT_TYPES` */
  eventType: string
  /**
   * 调用入口：`mcp`（MCP 客户端）/ `plugin`（Hermes 记忆插件）/
   * `rest`（直连 `/memory/*` 的脚本）/ `dashboard` / `admin` / `auth`。
   * 工具名回答“调了什么”，来源回答“从哪进来的”——插件与脚本走同一批端点，
   * 靠客户端自报的标识区分。MCP 与 REST 的 add 都是异步入队，入口差异只体现在
   * 来源上。迁移 `0023` 之前的历史行没有这个字段。
   */
  source?: string
  userIds?: string[]
  agentIds?: string[]
  status: string
  /** 毫秒。异步 add 还没跑完时是 null（页面留空），不是 0。 */
  duration?: number | null
  /** 模型 token。上游没有回报时是 undefined，页面留空。 */
  requestTokens?: number
  responseTokens?: number
}

/**
 * 异步任务（`/admin/jobs`）。写记忆走队列时不阻塞对话，任务失败了就是「死信」，
 * 由平台管理员在「异步任务」页重放。
 *
 * 字段是后端原样下发的 snake_case（运维接口，不套前端驼峰），别在这里改名。
 */
export interface Job {
  id: string
  kind: string
  /** pending / running / succeeded / failed */
  status: string
  tenant_id: string
  user_id: string
  attempts: number
  last_error: string
  created_at: string
  updated_at: string
  next_attempt_at?: string
}

/** 调用日志详情。`requestBody` / `responseBody` 是 JSON 字符串，前端要二次 parse */
export interface LogDetail extends Omit<RequestLog, 'userIds' | 'agentIds'> {
  traceId?: string
  requestId?: string
  userId?: string
  agentId?: string
  requestCode?: number
  responseCode?: number
  requestBody?: string
  responseBody?: string
  /** 上游 add 回报的 LLM 调用次数；没有回报时不下发 */
  llmCalls?: number
}

/* ------------------------------------------------------------ 记忆 */

export interface MemoryVersion {
  version: number
  content: string
  createdAt: string
  updateType?: string
  validFrom?: string
  validTo?: string
}
export interface MemorySource {
  type?: string
  role?: string
  content?: string
  time?: string
  url?: string
}

export interface Memory {
  /** 该次检索的实际相关度；画像或旧快照可能缺失。 */
  relativity?: number
  memoryId: string
  memoryContent: string
  /** user | agent | public */
  subjectType: string
  externalUserId: string
  createTime: string
  /** 上游的 memory_type 原文（LongTermMemory / UserMemory / Context / InsightMemory …）。
   *  「事件记忆」页签里混着 Context 与 InsightMemory 两种，列表要靠它区分。 */
  memoryType?: string
  memoryKey?: string
  status?: string
  tags?: string[]
  updateTime?: string
  version?: number
  history?: MemoryVersion[]
  sources?: MemorySource[]
  /** profile_mem 专有 */
  profileTemplateId?: string
  profileInstanceId?: string
}

export interface MemoryActivity {
  callId: string
  status: 'pending' | 'succeeded' | 'failed' | 'unavailable' | 'not_found'
  completedAt: string
  memories: Memory[]
  total: number
  truncated: boolean
}

export interface MemoryEvent extends MemoryActivity {
  eventId: string
  sequence: string
  kind: 'recall' | 'add' | 'update' | 'delete'
  sessionId: string
  before: Memory[]
  memoryIds: string[]
}

export type MemoryLogActivity = MemoryActivity | MemoryEvent
/* ------------------------------------------------------------ 本地记忆镜像 */

/** 本地插件上传的层次，与插件的 L1–L4 一一对应 */
export type LocalMemoryLayer = 'trace' | 'policy' | 'world_model'

/** 设备状态。`revoked` 后令牌立即失效，但历史镜像保留。 */
export type LocalDeviceStatus = 'active' | 'revoked'

/** 单项目配额。字节与行数任一超限都算超配额（服务端回 41300）。 */
export interface LocalSyncQuota {
  /** 已落库镜像的压缩字节与行数 */
  usedBytes: number
  limitBytes: number
  usedRows: number
  limitRows: number
  /** 尚未提交批次占用的分块字节与 manifest 声明行数；与 used 相加才是准入口径 */
  pendingBytes?: number
  pendingRows?: number
}

/** 本地镜像的治理视图（只统计控制面副本，不代表插件或云端的真实总量）。 */
export interface LocalSyncSummary {
  /** 未删除行数 */
  total: number
  /** 已软删除、仍保留在库里的行数 */
  deleted: number
  promoted: number
  promotePending: number
  promoteFailed: number
  layers: Partial<Record<LocalMemoryLayer, number>>
  /** 无值时为 "" */
  oldestSyncedAt: string
  newestSyncedAt: string
  quota: LocalSyncQuota
}

/** 已绑定的本地设备。
 *
 *  `id` 是控制面的行 id，吊销接口用的就是它；`deviceId` 是插件侧的稳定标识。
 *  `status` 为 `revoked` 后设备令牌立即失效，但历史镜像仍然保留。
 *
 *  ⚠️ 后端对没有值的字段返回**空串**而不是 null（`lastSeenAt` / `revokedAt`）。 */
export interface LocalDevice {
  id: string
  deviceId: string
  accountId: string
  label: string
  agentKind: string
  status: LocalDeviceStatus
  createdAt: string
  /** 最后一次成功提交的时间；从未同步过时为 "" */
  lastSeenAt: string
  /** 未吊销时为 "" */
  revokedAt: string
}

/** 本地记忆镜像的列表行。正文等大字段不在列表接口里，详情按 id 单取。 */
export interface LocalMemory {
  id: string
  deviceId: string
  /** 来源设备名；手工上传的合成设备为空串 */
  deviceLabel: string
  layer: LocalMemoryLayer
  /** 插件本地表里的行 id，同一设备同一层唯一 */
  sourceId: string
  contentHash: string
  /** 无值时为 "" */
  title: string
  summary: string
  /** 插件侧最后修改时间，增量同步的游标就是它 */
  sourceUpdatedAt: string
  /** 控制面接收这条镜像的时间 */
  syncedAt: string
  /** 是否已经转存成功 */
  promoted: boolean
  /** 已转存成云端记忆时是那条记忆的 id，否则为 "" */
  promotedMemoryId: string
  /** 转存作业状态；没有作业时为空串（服务端原文，不翻译） */
  promoteStatus: string
  /** 转存失败原因；没有失败时为空串（服务端原文，不翻译） */
  promoteError: string
}

/** 详情。`payload` 是服务端解压后的原始 JSON；前端按层做层次化展示，不假设字段。 */
export interface LocalMemoryDetail extends LocalMemory {
  payload?: unknown
}

/** 单条/批量转存的结果。失败逐条给原因，不整批回滚。 */
export interface LocalSyncResult {
  /** 已入队等待 add_message 作业的本地记忆 id */
  queued: string[]
  failed: { id: string; message: string }[]
}

/* ------------------------------------------------------------ 知识库 */

export interface KnowledgeBase {
  kbId: string
  knowledgebaseName: string
  knowledgebaseDescription?: string
  projectIds?: string[]
  fileCount?: number
  totalSize?: number
  createTime?: string
  updateTime?: string
}

/** 知识库文件条目（上游 public_file_view 的投影） */
export interface KnowledgeFile {
  id: string
  fileId: string
  knowledgebaseId: string
  name: string
  type?: string
  status?: string
  size: number
  sizeMB?: number
  createTime?: string
}

/* ------------------------------------------------------------ 画像模板 */

export interface ProfileTemplate {
  templateId: string
  templateName: string
  bindingCount?: number
  updatedAt?: string
  [k: string]: unknown
}

/* ------------------------------------------------------------ 用量 */

export interface UsageDailyStat {
  date: string
  addMessage?: { calls?: number; totalTokens?: number }
  searchMemory?: { calls?: number; totalTokens?: number }
  chat?: { requestTokens?: number; responseTokens?: number; totalTokens?: number }
  extractMemory?: { requestTokens?: number; responseTokens?: number; totalTokens?: number }
  rerank?: { requestTokens?: number; totalTokens?: number }
}

/* ------------------------------------------------------------ 运行信息 */

export type DataSource = 'live' | 'sample'

/** 接入说明与示例命令据此生成 */
/** /api/meta 的 tools 是工具名数组（如 add_message），不是对象。 */
export type ToolInfo = string

export interface Meta {
  mode: DataSource
  service: string
  health: Record<string, unknown> | null
  /** 密钥前缀，新建密钥的展示与校验用它 */
  keyPrefix: string
  tools: ToolInfo[]
  /** 鉴权是否生效。**不是**给人看的文案（那是 `auth`），是给程序判断用的 */
  authEnabled?: boolean
  /** `page` = 控制台登录页；`basic` = HTTP Basic；`external` = 零信任网关注入身份 */
  authMode?: 'page' | 'basic' | 'external'
}

/** 当前登录主体（`/api/auth/me`） */
export interface Account {
  /** 账号 ID；管理 Token 没有账号身份时为空 */
  accountId?: string
  username: string
  displayName: string
  platformAdmin: boolean
  currentProjectId: string
  projects: { id: string; name: string; role: string; member?: boolean; kind?: 'team' | 'personal' }[]
}

/* ------------------------------------------------------------ 模型 */

/** 上游配置里真实生效的一个模型 */
export interface ModelRecord {
  /** 在记忆链路里的位置：记忆读取 / 记忆写入 / 向量化 / 重排 */
  role: string
  model: string
  /** 关键参数（提供方主机名 · 维度 / 后端） */
  spec: string
  /** 取自上游 ConfigMap 的哪个键，排障时用来核对 */
  source: string
  group: 'memory' | 'chat'
}

/* ------------------------------------------------------------ 图谱 */

export type GraphLinkKind = 'session' | 'parent' | 'merged' | 'derived'

export interface GraphNode {
  id: string
  /** 记忆正文的截断（≤120 字）。空串表示这条没正文，画布上退回显示 id 前缀 */
  label: string
  /** `label` 被截断过（BFF 现算的，不是靠「以 … 结尾」猜的）。
   *  图谱响应里只带截断正文——全库正文是 MB 级，这个接口要一次给完整张图。
   *
   *  ⚠️ 前端别用「label 以 `…` 结尾」代替这个字段：正文自己以省略号结尾时会误判成
   *  截断，点「展开」什么都不变，看着就是个坏按钮。 */
  labelTruncated: boolean
  memoryType: string
  createdAt: string
  /** 空串 = 没有会话归属 */
  sessionId: string
  /** 簇 id：`session:<id>` 或 `orphan:<序号>` */
  clusterId: string
  userId: string
  /** 这条记忆归纳自哪些记忆（上游 `source_memory_ids`）。洞察记忆才有，其余为空数组。
   *  它在上游是**节点属性不是边**，画布上的 `derived` 连线由 BFF 补出来。 */
  sourceIds: string[]
}

export interface GraphLink {
  source: string
  target: string
  /** `session` 是按 session_id 聚合出来的星形连线，不是图库里存的边 */
  kind: GraphLinkKind
}

export interface MemoryGraph {
  nodes: GraphNode[]
  links: GraphLink[]
  /** 实际生效的租户（BFF 回显，便于确认画的是哪个租户） */
  tenant?: string
  stats: {
    nodes: number
    links: number
    sessions: number
    /** 规模 ≥3、会画成带标签区域的会话数 */
    clusters: number
    orphans: number
    /** 撞到节点上限被截断了 */
    truncated: boolean
    types: { name: string; count: number }[]
  }
}

/* ------------------------------------------------------------ 对话 */

/** 后端自动为项目选中的那把密钥。前端只展示前缀，不拿明文。 */
export interface ChatApiKeyRef {
  id: string
  label: string
  prefix: string
}

/** 对话页的准入状态。三项（项目 / 密钥 / 模型配置）都齐了 `canChat` 才为真。 */
export interface ChatState {
  projectId: string
  projectName: string
  hasProject: boolean
  hasApiKey: boolean
  hasConfig: boolean
  /** 三项（项目 / 密钥 / 模型配置）都齐了才为真 */
  canChat: boolean
  readOnly?: boolean
  /** 后端没有可用密钥时会省略这个字段 */
  apiKey?: ChatApiKeyRef | null
  /** 一个账号在一个项目下只有一个固定会话 */
  sessionId: string
  contextWindow: number
  autoCompact: boolean
  piAvailable: boolean
}

/** BYOK 的上游模型网关配置。`apiKey` 只下发掩码。 */
export interface ChatConfig {
  baseUrl: string
  model: string
  /** 空串表示还没有保存过上游密钥 */
  apiKeyMasked: string
  configured: boolean
}

/** pi 会话里的原始消息。上游结构随版本变化，这里只约束两个稳定字段；
 *  `content` 由前端做防御式解析（字符串 | 内容块数组）。 */
export interface ChatMessage {
  id?: string
  role: string
  content: unknown
  [k: string]: unknown
}

export interface ChatMessagesResponse {
  messages: ChatMessage[]
  /** 本页在原消息数组里的绝对起始下标，用作下一页的 `before` 游标 */
  start?: number
  /** 会话消息总条数（含工具结果） */
  total?: number
  /** 前面还有更早的历史；后端在取到全量时省略这个字段 */
  hasMore?: boolean
}

/** 一条已被服务端受理的消息。`queued` 表示要等当前轮结束才会发送。 */
export interface ChatSubmission {
  id: string
  queued: boolean
}

export type ChatEventType =
  | 'chat_ready'
  | 'chat_status'
  | 'chat_error'
  | 'message_accepted'
  | 'compaction_start'
  | 'compaction_end'
  | 'ping'
  | 'agent_start'
  | 'agent_end'
  /** 整轮结束：一次对话只发一次。agent_end 一轮可能出现多次，不能拿它判终态。 */
  | 'agent_settled'
  | 'turn_start'
  | 'turn_end'
  | 'message_start'
  | 'message_update'
  | 'message_end'
  | 'tool_execution_start'
  | 'tool_execution_update'
  | 'tool_execution_end'

/** SSE 的一帧。`data` 的形状按事件类型不同，调用侧自己收窄。 */
export interface ChatEvent {
  submissionId?: string
  seq: number
  /** 已知事件见 ChatEventType；后端新增事件时前端按未知事件忽略 */
  type: ChatEventType | (string & {})
  data: unknown
}

export type OnboardingAction = 'start' | 'advance' | 'back' | 'dismiss' | 'replay' | 'checkpoint'
export interface OnboardingCheckpoint {
  stage: 'first' | 'logs' | 'recall' | 'finish'
  first: string
  second?: string
}
export interface OnboardingProgress {
  chat?: OnboardingCheckpoint
  status: 'pending' | 'active' | 'dismissed' | 'completed'
  step: number
  revision: number
  version: number
}
