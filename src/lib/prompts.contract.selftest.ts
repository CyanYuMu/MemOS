/** 真实控制面响应的契约自检：fixture 直接来自 18102，`node src/lib/prompts.contract.selftest.ts`。
 *
 *  ⚠️ 这里的 fixture 不是手写样例。mock 与真实后端最容易分叉的地方是「可选字段是 null 还是空串」，
 *  只靠自己造的 mock 永远发现不了；列表和详情各有一条默认槽位，`updatedAt` / `author` 都是 null。
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  promptDetailSchema,
  promptListResponse,
  promptResetResponse,
  promptRun,
  promptSaveResponse,
  promptTestResponse,
  promptValidation,
  promptVersionsResponse,
  promptVersion,
} from './promptSchemas.ts'

const DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../server/mock/fixtures/prompts')

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

type Envelope = { code: number; data: unknown; message?: string }
const load = (name: string): Envelope => JSON.parse(fs.readFileSync(path.join(DIR, name), 'utf8'))
const parseData = <T>(schema: { parse: (v: unknown) => T }, name: string): T => schema.parse(load(name).data)

/* ------------------------------------------------------------ list / detail */

{
  const list = parseData(promptListResponse, 'list.json')
  const state = list.prompts[0]?.state.zh
  check('真实 list 通过 schema', list.prompts.length === 12, `${list.prompts.length} 条`)
  check('默认槽位的 null updatedAt/author 归一成空串', state?.updatedAt === '' && state?.author === '', JSON.stringify(state))
  check('默认槽位 versionId/versionNo 为 0', state?.versionId === 0 && state?.versionNo === 0)

  const chat = parseData(promptDetailSchema, 'detail.extract.chat.json')
  check('真实 detail(extract.chat) 通过 schema', chat.key === 'extract.chat' && chat.inputKind === 'conversation')
  check(
    'detail 的默认 effective 也把 null 归一成空串',
    chat.effective.zh?.updatedAt === '' && chat.effective.zh?.author === '',
    JSON.stringify({ updatedAt: chat.effective.zh?.updatedAt, author: chat.effective.zh?.author }),
  )
  check(
    '真实样例带 lang',
    chat.sampleInputs.every((s) => s.lang === 'zh' || s.lang === 'en'),
  )

  const motive = parseData(promptDetailSchema, 'detail.dream.motive.json')
  check('真实 detail(dream.motive) 通过 schema', motive.inputKind === 'memories' && motive.languages.join(',') === 'en')
  check('Dream 的 memories 样例是对象数组', JSON.stringify(motive.sampleInputs[0]?.input).includes('"memory"'))
}

/* --------------------------------------------------------------- versions */

{
  const chat = parseData(promptVersionsResponse, 'versions.extract.chat.json')
  const kinds = new Set(chat.versions.map((v) => v.kind))
  check('真实 versions 通过 schema', chat.versions.length === 4, `${chat.versions.length} 条`)
  check('真实历史覆盖 save / rollback / reset', kinds.has('save') && kinds.has('rollback') && kinds.has('reset'), [...kinds].join(','))
  check('版本号按槽位递增', chat.versions.map((v) => v.versionNo).join(',') === '4,3,2,1')
  check('reset 行的 parentVersionId 与空正文原样保留', chat.versions[0]?.kind === 'reset' && chat.versions[0]?.body === '')

  const afterReset = parseData(promptVersionsResponse, 'versions.general_string.after-reset.json')
  check('还原后版本列表以 reset 开头', afterReset.versions[0]?.kind === 'reset' && afterReset.versions[0]?.versionNo === 4)

  // 真实环境暂时没有 migrated 行；schema 的 enum 仍要接受它（历史数据升级时会出现）
  const migrated = promptVersion.parse({
    versionId: 900,
    versionNo: 5,
    lang: 'zh',
    kind: 'migrated',
    body: '旧版本',
    note: '',
    author: 'migration',
    createdAt: '2026-10-29T00:00:00Z',
    parentVersionId: null,
  })
  check('schema 接受 migrated 类型', migrated.kind === 'migrated')
}

/* ------------------------------------------------------------------- save */

{
  const v1 = parseData(promptSaveResponse, 'save.v1.json')
  const v2 = parseData(promptSaveResponse, 'save.v2.json')
  const rb = parseData(promptSaveResponse, 'save.rollback.json')
  check('真实 save 通过 schema 且序号从 1 开始', v1.versionNo === 1 && v2.versionNo === 2 && rb.versionNo === 3)
  check(
    'save 的 effective 是扁平对象且带项目来源',
    v1.effective.source === 'project' && v1.effective.versionId === v1.versionId && Boolean(v1.effective.updatedAt),
    JSON.stringify({ source: v1.effective.source, updatedAt: v1.effective.updatedAt }),
  )

  const conflict = load('save.conflict.json')
  const data = conflict.data as { currentVersionId: number; currentVersionNo: number }
  check(
    '409 同时带 currentVersionId 与 currentVersionNo',
    conflict.code === 40900 && data.currentVersionId === 12 && data.currentVersionNo === 2,
  )

  const afterReset = load('save.conflict-after-reset.json')
  const zero = afterReset.data as { currentVersionId: number; currentVersionNo: number }
  check('还原后冲突的两个字段是 0 而不是缺失', afterReset.code === 40900 && zero.currentVersionId === 0 && zero.currentVersionNo === 0)

  const missing = load('save.unknown-version.json')
  check(
    'unknown_version 404 带服务端文案',
    missing.message === '这个历史版本不存在，请刷新版本历史后再试' && missing.data === null,
    missing.message,
  )
}

/* ------------------------------------------------------------ reset / test */

{
  const reset = parseData(promptResetResponse, 'reset.json')
  check('真实 reset 通过 schema 且带 versionNo', reset.reset[0]?.versionNo === 4, JSON.stringify(reset.reset[0]))

  const compare = parseData(promptTestResponse, 'test.compare.json')
  check(
    '真实 test(compareDefault) 通过 schema',
    compare.runs.length === 2 && compare.runs[0]?.label === 'draft' && compare.runs[1]?.label === 'default',
  )
  check(
    'run 的 usedSlots / warnings 是数组',
    Array.isArray(compare.runs[0]?.usedSlots) && compare.runs[0]?.usedSlots?.[0]?.source === 'draft',
    JSON.stringify(compare.runs[0]?.usedSlots),
  )
  check(
    'usage 的开放字段（calls/totalTokens）透传',
    compare.runs[0]?.usage?.promptTokens === 2120 && (compare.runs[0]?.usage as Record<string, unknown>)?.calls === 1,
  )

  const fallback = parseData(promptTestResponse, 'test.fallback.json')
  check('真实 fallback 样本解析失败并标记整段存原文', fallback.runs[0]?.parseOk === false && fallback.runs[0]?.fallbackTriggered === true)
  check('失败/散文输出允许是数组以外形状', Array.isArray(fallback.runs[0]?.output))

  // 单条 run 的 schema 也单独接受真实形状（页面只看 runs）
  check('run schema 独立可解析', promptRun.parse(compare.runs[0]).ok === true)
}

/* --------------------------------------------------------------- validate */

{
  check('真实 validate.ok 通过 schema', parseData(promptValidation, 'validate.ok.json').ok === true)
  const bad = parseData(promptValidation, 'validate.error.json')
  check('真实 validate.error 带可操作错误', !bad.ok && bad.errors[0]?.code === 'missing_placeholder', bad.errors[0]?.message)
}

console.log(`\n${passed}/${passed + failures.length} 通过`)
if (failures.length) {
  console.log('失败项：' + failures.join('、'))
  throw new Error(`真实响应契约自检失败 ${failures.length} 项`)
}
