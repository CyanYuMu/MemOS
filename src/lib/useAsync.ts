import { useCallback, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError } from './api'
import { errText } from './i18n'
import { useLang } from './store'

export type AsyncState = 'loading' | 'ready' | 'error'

/** 查询失败后是否重试。
 *
 *  status 0 是 fetch 层失败（`api.ts` 里标出来的），说明 BFF 正在重启：
 *  重试 6 次、退避到 8 秒，总共约 30 秒，覆盖 Recreate 部署的空窗期。
 *  服务端给了信封错误码（能力未启用 50300、无权限、限流……）说明请求已被明确回答，
 *  重试不会让它变好；而且 TanStack Query 在标签页不在前台时会暂停重试，
 *  页面会一直停在加载态，到切回标签页才出结果。这类错误第一次就落到错误态。
 *  其余（网关直接返回的 HTML 5xx 等）保留一次重试。 */
export function shouldRetry(count: number, error: unknown): boolean {
  if (error instanceof ApiError) {
    if (error.status === 0) return count < 6
    if (error.code !== undefined) return false
  }
  return count < 1
}

/** 统一处理「加载中 / 成功 / 失败」，并在依赖变化时重新取数。
 *
 *  已深度接入 TanStack Query v5：
 *    - 自动具备跨页面缓存、并发请求去重与后台静默刷新
 *    - 保留原先对外的简洁接口 { data, state, error, reload }，现有页面零负担升级
 *
 *  `skip` 为真时 query disabled，状态保持 loading 直到 ready。
 *
 *  `refetchInterval` 给需要「自己会刷新」的页面用（目前只有接口文档页的服务状态灯）：
 *  不传就不轮询。切到后台标签页时 TanStack Query 默认会暂停轮询，回来再补一次。 */
export function useAsync<T>(
  fn: () => Promise<T>,
  deps: unknown[],
  { skip = false, key, refetchInterval }: { skip?: boolean; key?: string; refetchInterval?: number } = {},
) {
  const { lang } = useLang()

  const {
    data = null,
    status,
    error,
    refetch,
  } = useQuery<T, unknown>({
    queryKey: key ? ['useAsync', key, ...deps] : ['useAsync', ...deps],
    queryFn: fn,
    enabled: !skip,
    refetchInterval,
    retry: shouldRetry,
    retryDelay: (n) => Math.min(1000 * 2 ** n, 8000),
  })

  const state: AsyncState = skip || status === 'pending' ? 'loading' : status === 'error' ? 'error' : 'ready'

  return {
    data: data ?? null,
    state,
    error: error ? errText(lang, error) : '',
    /** 原始异常。页面需要按 `ApiError.code` 区分能力未开启（50300）等情况时用；
     *  展示仍然走上面的 `error`（已按语言处理）。 */
    rawError: error,
    /** 稳定引用：轮询的 `useEffect` 不会因为每次渲染换函数而反复重建定时器 */
    reload: useCallback(() => {
      refetch()
    }, [refetch]),
  }
}

/** 列表页的翻页 + 搜索状态 */
export function usePaged(pageSize = 10) {
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [draft, setDraft] = useState('')
  return {
    page,
    pageSize,
    q,
    draft,
    setDraft,
    submit: () => {
      setQ(draft.trim())
      setPage(1)
    },
    reset: () => {
      setDraft('')
      setQ('')
      setPage(1)
    },
    setPage,
  }
}
