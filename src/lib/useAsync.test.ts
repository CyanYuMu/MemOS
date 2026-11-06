import { expect, it } from 'vitest'
import { ApiError } from './api'
import { shouldRetry } from './useAsync'

it('fetch 层失败按重启空窗期重试 6 次', () => {
  const err = new ApiError('network error', 0)
  expect([0, 5, 6].map((n) => shouldRetry(n, err))).toEqual([true, true, false])
})

it('带信封错误码的响应不重试，能力未启用直接落到错误态', () => {
  expect(shouldRetry(0, new ApiError('本地同步 能力未启用', 503, 50300))).toBe(false)
  expect(shouldRetry(0, new ApiError('forbidden', 403, 40300))).toBe(false)
})

it('网关返回的非 JSON 5xx 与未知异常保留一次重试', () => {
  expect([0, 1].map((n) => shouldRetry(n, new ApiError('HTTP 502: response is not JSON', 502)))).toEqual([true, false])
  expect([0, 1].map((n) => shouldRetry(n, new Error('boom')))).toEqual([true, false])
})
