import { useState, type ReactNode } from 'react'
import { useAsync, usePaged } from '../lib/useAsync'
import { Pager, Panel, PageHeader, SearchBox, StateRow, Table, TD, TD_BREAK, Th } from './ui'
import { Button } from './ui/button'
import { useT } from '../lib/store'

export interface Col<T> {
  key: string
  label: string
  render: (row: T) => ReactNode
  /** 长文本列才折行；默认不换行，只有个别列显式加了 break-all */
  wrap?: boolean
}

/** 各接口的列表键不同（records / logs / memListItemResponseList / list），
 *  页面在 load 里挑出来，这里只认归一化后的形状。 */
export interface Loaded<T> {
  items: T[]
  total: number
}

/** 列表型页面的通用骨架：标题 + 可选搜索 + 表格 + 分页 */
export function ListPage<T>({
  title,
  desc,
  cols,
  load,
  rowKey,
  searchable = false,
  actions,
  onRowClick,
  /** 有筛选时给一个渲染函数，参数是「关掉浮层」；渲染在搜索框右边的漏斗按钮下 */
  filter,
  /** 搜索框的提示语，各页不同 */
  searchPlaceholder,
  /** 表格外框的额外类名（调用日志要限高、记忆列表要挂自己的样式钩子） */
  panelClassName,
  /** 除翻页与搜索外还要触发重新取数的东西（当前项目、标签页、筛选条件等） */
  deps = [],
  /** 取数条件还没就绪时置真：不请求、停在「加载中」。
   *  典型场景是项目字典还没回来——那时 `deps` 里的 `currentProjectId` 还是空串，
   *  请求会被 BFF 拒（`40000 未知项目：(空)`），页面先闪一段报错再打第二遍。 */
  skip = false,
}: {
  title: string
  desc?: string
  cols: Col<T>[]
  load: (page: number, pageSize: number, q: string) => Promise<Loaded<T>>
  rowKey: (row: T) => string
  searchable?: boolean
  panelClassName?: string
  actions?: ReactNode
  onRowClick?: (row: T) => void
  filter?: (close: () => void) => ReactNode
  searchPlaceholder?: string
  deps?: unknown[]
  skip?: boolean
}) {
  const pg = usePaged(10)
  const t = useT()
  const [filterOpen, setFilterOpen] = useState(false)
  const { data, state, error, reload } = useAsync(() => load(pg.page, pg.pageSize, pg.q), [pg.page, pg.pageSize, pg.q, ...deps], { skip })

  return (
    <>
      <PageHeader
        title={title}
        desc={desc}
        actions={
          <>
            {searchable && (
              <SearchBox
                value={pg.draft}
                onChange={pg.setDraft}
                onSubmit={pg.submit}
                onFilter={filter ? () => setFilterOpen((o) => !o) : undefined}
                onFilterClose={filter ? () => setFilterOpen(false) : undefined}
                filterOpen={filterOpen}
                filter={filter ? filter(() => setFilterOpen(false)) : undefined}
                placeholder={searchPlaceholder}
              />
            )}
            {actions}
          </>
        }
      />

      <div className="flex min-h-0 flex-1 flex-col gap-4">
        <div className="flex-1">
          <div className="flex h-full min-h-0 flex-col gap-3">
            <Panel className={`flex-1 min-h-0 ${panelClassName ?? ''}`}>
              <Table
                head={
                  <>
                    {cols.map((c) => (
                      <Th key={c.key}>{c.label}</Th>
                    ))}
                  </>
                }
              >
                {state !== 'ready' && <StateRow colSpan={cols.length} state={state} error={error} onRetry={reload} />}
                {state === 'ready' && data?.items.length === 0 && (
                  <StateRow
                    colSpan={cols.length}
                    state="ready"
                    emptyTitle={pg.q ? t('common.noMatch') : undefined}
                    emptyAction={
                      pg.q ? (
                        <Button size="sm" variant="secondary" onClick={pg.reset}>
                          {t('common.clearSearch')}
                        </Button>
                      ) : undefined
                    }
                  />
                )}
                {state === 'ready' &&
                  data?.items.map((row) => (
                    <tr
                      key={rowKey(row)}
                      data-selectable="true"
                      tabIndex={onRowClick ? 0 : undefined}
                      onClick={onRowClick ? () => onRowClick(row) : undefined}
                      onKeyDown={
                        onRowClick
                          ? (e) => {
                              // 行内还有按钮（如「查看详情」）。焦点在按钮上时把按键交给按钮，
                              // 否则回车会被行和按钮各触发一次。
                              if (e.target !== e.currentTarget) return
                              if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault()
                                onRowClick(row)
                              }
                            }
                          : undefined
                      }
                      className={`data-[selected=true]:bg-elevated/50 ${onRowClick ? 'cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40' : ''}`}
                    >
                      {cols.map((c) => (
                        <td key={c.key} data-pinned="false" className={c.wrap ? TD_BREAK : TD}>
                          {c.render(row)}
                        </td>
                      ))}
                    </tr>
                  ))}
              </Table>
            </Panel>
            <Pager page={pg.page} pageSize={pg.pageSize} total={data?.total ?? 0} onPage={pg.setPage} />
          </div>
        </div>
      </div>
    </>
  )
}
