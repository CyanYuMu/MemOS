/** 提示词工作台接口的响应 schema。
 *
 *  单独放在这里是为了让 fixture 自检（`node src/lib/prompts.contract.selftest.ts`）能直接
 *  `schema.parse()` 真实控制面响应，而不必经过 `api.ts` 里的 i18n / JSON import。
 *  ⚠️ 可选字段用 `nullable().optional().transform(...)`：`.default()` 只处理 undefined，
 *  真实接口的 Go 指针 / Python None 会给 null。
 */

import { z } from 'zod'

/** 以语言代码为键的双语文本。控制面不转换这一层的键名 */
export const langText = z.record(z.string(), z.string())

/** 一个槽位（项目 × 提示词 × 语言）的当前状态，不带正文 */
export const promptSlotState = z.object({
  source: z.enum(['default', 'project']),
  /** 全局自增 id，只用于接口调用 */
  versionId: z.number(),
  /** 槽位内从 1 开始的序号，界面显示它；接口没上线前可能缺 */
  versionNo: z.number().optional(),
  // 默认槽位这两个字段是 null（spec 第 6 节）；缺失、null、字符串都在边界归一成空串
  updatedAt: z
    .string()
    .nullable()
    .optional()
    .transform((v) => v ?? ''),
  author: z
    .string()
    .nullable()
    .optional()
    .transform((v) => v ?? ''),
})

/** 当前生效值：槽位状态 + 正文 */
export const promptEffective = promptSlotState.extend({ body: z.string() })

export const promptListItem = z.object({
  key: z.string(),
  /** extract / process / dream；新分组也照原样渲染，不让页面因未知值整块失败 */
  group: z.string(),
  title: langText,
  summary: langText,
  languages: z.array(z.string()),
  editable: z.boolean(),
  state: z.record(z.string(), promptSlotState),
})

export const promptListResponse = z.object({ prompts: z.array(promptListItem) })

export const promptPlaceholder = z.object({
  name: z.string(),
  required: z.boolean(),
  description: langText,
})

export const promptOutputContract = z.object({
  format: z.string(),
  requiredKeys: z
    .array(z.string())
    .nullable()
    .optional()
    .transform((v) => v ?? []),
  itemKeys: z
    .array(z.string())
    .nullable()
    .optional()
    .transform((v) => v ?? []),
  description: langText,
})

export const promptSampleInput = z.object({
  id: z.string(),
  title: langText,
  /** 样例输入本身的语言（`zh` / `en`）；老数据可能没有 */
  lang: z.string().optional(),
  /** 按 `inputKind` 定的输入，原样透传 */
  input: z.unknown(),
})

export const promptRegistry = z.object({
  key: z.string(),
  group: z.string(),
  title: langText,
  summary: langText,
  languages: z.array(z.string()),
  /** 替换方式由注册表决定（`dollar` / `format` / `brace`）。按字符串收，
   *  新增取值时页面仍能显示，高亮规则按「非 dollar 一律认 {name}」处理 */
  syntax: z.string(),
  placeholders: z.array(promptPlaceholder).default([]),
  /** 代码在用户提示词之后自动追加的文本，只读展示 */
  lockedSuffix: z.string().nullable().default(null),
  inputKind: z.string(),
  /** 该输入的 JSON Schema，原样透传 */
  inputSchema: z.unknown(),
  outputContract: promptOutputContract,
  sampleInputs: z.array(promptSampleInput).default([]),
  editable: z.boolean(),
})

export const promptDetailSchema = promptRegistry.extend({
  defaults: z.record(z.string(), z.string()),
  effective: z.record(z.string(), promptEffective),
})

export const promptVersion = z.object({
  versionId: z.number(),
  /** 槽位内从 1 开始的序号 */
  versionNo: z.number().optional(),
  lang: z.string(),
  kind: z.enum(['save', 'reset', 'rollback', 'migrated']),
  body: z.string(),
  note: z
    .string()
    .nullable()
    .optional()
    .transform((v) => v ?? ''),
  author: z
    .string()
    .nullable()
    .optional()
    .transform((v) => v ?? ''),
  createdAt: z.string(),
  parentVersionId: z.number().nullable().default(null),
})

export const promptVersionsResponse = z.object({
  versions: z.array(promptVersion),
  hasMore: z.boolean(),
})

export const promptIssue = z.object({ code: z.string(), message: z.string() })

export const promptValidation = z.object({
  ok: z.boolean(),
  errors: z.array(promptIssue).default([]),
  warnings: z.array(promptIssue).default([]),
})

export const promptSaveResponse = z.object({
  versionId: z.number(),
  versionNo: z.number().optional(),
  createdAt: z.string(),
  effective: promptEffective,
})

export const promptResetResponse = z.object({
  reset: z.array(z.object({ key: z.string(), lang: z.string(), versionId: z.number(), versionNo: z.number().optional() })).default([]),
})

export const promptSlotUse = z.object({
  key: z.string(),
  lang: z.string(),
  /** draft = 本次草稿；project = 项目保存的版本；default = 内置默认 */
  source: z.enum(['draft', 'project', 'default']),
  versionId: z.number().optional(),
  versionNo: z.number().optional(),
})

export const promptRun = z.object({
  label: z.enum(['draft', 'default']),
  ok: z.boolean(),
  parseOk: z.boolean(),
  /** 为真表示真实抽取会把原文整段存成 UserMemory */
  fallbackTriggered: z.boolean(),
  contract: z.object({
    ok: z.boolean(),
    missing: z
      .array(z.string())
      .nullable()
      .optional()
      .transform((v) => v ?? []),
  }),
  /** 按 key 解析后的结构，字段原样透传 */
  output: z.unknown(),
  rawOutput: z
    .string()
    .nullable()
    .optional()
    .transform((v) => v ?? ''),
  truncated: z.boolean().default(false),
  latencyMs: z.number().default(0),
  model: z
    .string()
    .nullable()
    .optional()
    .transform((v) => v ?? ''),
  /** 用量字段原样透传；上游可能少给某一项，也可能整个不给 */
  usage: z
    .object({ promptTokens: z.number().default(0), completionTokens: z.number().default(0) })
    .passthrough()
    .nullable()
    .optional(),
  error: z.object({ code: z.string(), message: z.string() }).optional(),
  /** 这次试跑实际用到的模板；数据面这一轮新增，缺省时界面不显示这一块 */
  usedSlots: z.array(promptSlotUse).optional(),
  /** 数据面给出的提示，例如 `draft_not_used`（草稿没用上） */
  warnings: z.array(promptIssue).optional(),
})

export const promptTestResponse = z.object({ runs: z.array(promptRun) })

export const promptGenerateResponse = z.object({ body: z.string(), validation: promptValidation })
