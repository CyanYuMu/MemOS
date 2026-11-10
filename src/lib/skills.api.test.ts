import { afterEach, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})
const reference = { projectId: 'project-a', skillId: 'fixture-repair', revision: 'a'.repeat(64) }
const mutation = { ...reference, expectedSequence: 7, operationId: 'operation-1', reason: '已核对' }
const lifecycleBinding = { ...mutation, packageId: 'b'.repeat(64), packageHash: 'c'.repeat(64), replacementRevision: null }

it('Skill 列表按 items 信封解析，保留项目边界与同源 POST', async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 0, data: { items: [], total: 0 }, message: 'ok' })))
  vi.stubGlobal('fetch', fetcher)
  expect(await api.skills.list({ projectId: reference.projectId, query: 'fixture' })).toEqual({ items: [], total: 0 })
  expect(fetcher).toHaveBeenCalledWith(
    '/api/v1/skills/list',
    expect.objectContaining({ method: 'POST', body: JSON.stringify({ projectId: 'project-a', query: 'fixture' }) }),
  )
})

it('旧 records 形状不能悄悄被当作空 Skill 列表', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 0, data: { records: [], total: 0 } }))))
  await expect(api.skills.list({ projectId: reference.projectId })).rejects.toMatchObject({ msg: { key: 'error.contract' } })
})

it('排队任务中的空结果不是任务失败，也不违反传输契约', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: 0,
          data: { jobId: 'job-1', status: 'queued', taskResult: null, skillUsage: null, validationId: null, errorCode: null },
        }),
      ),
    ),
  )
  expect(await api.skills.job({ projectId: 'project-a', jobId: 'job-1' })).toMatchObject({
    status: 'queued',
    taskResult: null,
    skillUsage: null,
  })
})

it('发布 409 保留安全冲突细节且不自动重试', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValue(
      new Response(
        JSON.stringify({ code: 40900, message: '管理序号已变化', data: { errorCode: 'sequence_conflict', currentSequence: 8 } }),
        { status: 409 },
      ),
    )
  vi.stubGlobal('fetch', fetcher)
  await expect(api.skills.publish({ ...mutation, packageId: 'package-1', packageHash: 'hash-1' })).rejects.toMatchObject({
    status: 409,
    data: { errorCode: 'sequence_conflict', currentSequence: 8 },
  })
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it.each([
  ['detail', () => api.skills.detail(reference)],
  ['evidence', () => api.skills.evidence({ ...reference, ruleId: 'rule-1' })],
  ['evaluations/start', () => api.skills.evaluate({ ...reference, caseId: 'dev-1', operationId: 'op-1' })],
  ['runs/start', () => api.skills.run({ ...reference, caseId: 'dev-1', operationId: 'op-1' })],
  ['jobs/detail', () => api.skills.job({ projectId: 'project-a', jobId: 'job-1' })],
  ['reviews/prepare', () => api.skills.prepareReview(reference)],
  ['reviews/decide', () => api.skills.decide({ ...mutation, decision: 'needs_evidence' })],
  ['publish', () => api.skills.publish({ ...mutation, packageId: 'package-1', packageHash: 'hash-1' })],
  ['status', () => api.skills.status({ ...mutation, action: 'pause' })],
  ['challenges', () => api.skills.challenge(mutation)],
  ['lifecycle/scan', () => api.skills.scanImpact(mutation)],
  ['lifecycle/prepare', () => api.skills.prepareImpact(reference)],
  ['lifecycle/review', () => api.skills.reviewImpact({ ...lifecycleBinding, decision: 'retain', rootIds: [], validationIds: [] })],
  [
    'challenges/resolve',
    () => api.skills.resolveChallenge({ ...lifecycleBinding, decision: 'upheld', challengeId: 'd'.repeat(64), evidenceIds: [] }),
  ],
  ['load-preview', () => api.skills.loadPreview({ ...reference, caseId: 'dev-1', mode: 'normal' })],
] as const)('冻结端点 %s 只发送显式项目和允许的操作参数', async (route, run) => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 40300, message: '无权限' }), { status: 403 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(run()).rejects.toBeInstanceOf(ApiError)
  expect(fetcher).toHaveBeenCalledTimes(1)
  const [path, options] = fetcher.mock.calls[0]
  expect(path).toBe(`/api/v1/skills/${route}`)
  expect(options.method).toBe('POST')
  const body = JSON.parse(options.body as string)
  expect(body.projectId).toBe('project-a')
  expect(body.actor).toBeUndefined()
  expect(body.filePath).toBeUndefined()
  expect(body.capability).toBeUndefined()
})

it.each([
  ['experiments/list', () => api.skills.experiments({ projectId: 'project-a' })],
  ['experiments/detail', () => api.skills.experiment({ projectId: 'project-a', experimentId: 'experiment-1' })],
  ['experiments/start', () => api.skills.startExperiment({ projectId: 'project-a', experimentId: 'experiment-1', operationId: 'start-1' })],
] as const)('实验端点 %s 保留项目与固定 ID，错误不触发自动重试', async (route, run) => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 40900, message: '实验已在执行' }), { status: 409 }))
  vi.stubGlobal('fetch', fetcher)
  await expect(run()).rejects.toBeInstanceOf(ApiError)
  expect(fetcher).toHaveBeenCalledTimes(1)
  const [path, options] = fetcher.mock.calls[0]
  expect(path).toBe(`/api/v1/skills/${route}`)
  const body = JSON.parse(options.body as string)
  expect(body.projectId).toBe('project-a')
  expect(body.path).toBeUndefined()
  expect(body.report).toBeUndefined()
})
it('实验列表不能把缺失的冻结摘要默认为当前版本', async () => {
  const data = {
    items: [
      { experimentId: 'experiment-1', skillId: 'fixture-repair', revision: 'a'.repeat(64), status: 'frozen', planned: 21, completed: 0 },
    ],
    total: 1,
  }
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 0, data }))))
  await expect(api.skills.experiments({ projectId: 'project-a' })).rejects.toMatchObject({ msg: { key: 'error.contract' } })
})
