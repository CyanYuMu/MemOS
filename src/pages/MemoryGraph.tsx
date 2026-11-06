/** 记忆星座图页面。
 *
 *  数据来自后端的 `GET /api/dashboard/graph/memories`（上游 `/product/get_memory_graph`），
 *  画布在 components/Constellation.tsx。
 *
 *  ⚠️ 画布**走亮色**，跟控制台其余页面同一套。2026-10-19 之前它是固定深色
 *  （#070b16 星空底），理由是「星图这个隐喻在亮底上不成立」——那个判断是错的：
 *  控制台只有亮色主题，一块黑画布嵌在中间非常突兀（用户报的「整体主题是 light，
 *  你这个是 dark」）。改亮之后点的辨识度靠**饱和度 + 尺寸**，见 Constellation.tsx。
 *
 *  这一页另外承担「探索」的入口：搜索框、结果列表、关系聚焦。它们都放在画布**外面**
 *  的 DOM 里，而不是画到 canvas 上——键盘和读屏只能操作 DOM，画在 canvas 上的输入框
 *  对辅助技术等于不存在。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type GraphNode } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { useLang, useProject, useT } from '../lib/store'
import { DICTS, type TKey } from '../lib/i18n'
import { PageHeader } from '../components/ui'
import { EmptyState } from '../components/ui/empty-state'
import { Constellation, type GraphFocus, type RevealRequest } from '../components/Constellation'
import { typeColor } from '../lib/constellation'
import { countWithin, hopDepths, indexEdges, matchNodes } from '../lib/graph-explore'
import { errText } from '../lib/i18n'
import { BTN_DIALOG_SECONDARY, BTN_PAGER, BTN_TEXT_LINK } from '../lib/cls'
import { Icon } from '../components/Icon'

const fmtTime = (iso: string) => (iso ? iso.slice(0, 19).replace('T', ' ') : '-')

/** 搜索框里最多列出多少条。再多的结果对「找到那一条」没有帮助，只会变成滚动条。 */
const SEARCH_LIMIT = 40

/** 加载态里那几颗「正在长出来的星」。位置避开正中，给转圈和文字留出空间。 */
const SKELETON_DOTS = [
  { x: 22, y: 24, size: 10, delay: 0 },
  { x: 31, y: 38, size: 7, delay: 180 },
  { x: 18, y: 52, size: 12, delay: 360 },
  { x: 38, y: 66, size: 8, delay: 120 },
  { x: 55, y: 20, size: 9, delay: 300 },
  { x: 68, y: 30, size: 6, delay: 60 },
  { x: 62, y: 50, size: 11, delay: 420 },
  { x: 74, y: 62, size: 8, delay: 240 },
  { x: 26, y: 76, size: 6, delay: 480 },
  { x: 47, y: 80, size: 9, delay: 200 },
  { x: 83, y: 44, size: 7, delay: 340 },
  { x: 78, y: 74, size: 10, delay: 90 },
]

export function MemoryGraph() {
  const t = useT()
  const { lang } = useLang()
  const { currentProjectId, currentProjectName, ready } = useProject()
  const [selected, setSelected] = useState<string | null>(null)

  const { data, state, error, reload } = useAsync(() => api.graph.memories({ tenant: currentProjectId || undefined }), [currentProjectId], {
    // 没有这一条的话，首帧会先用「无租户」打一次 585 KB（BFF 会退回当前活跃租户，
    // 拿到的就是同一份数据），字典回来后再打一次带 tenant 的——同一张图下载两遍，
    // 星云还要重新布局一次
    skip: !ready,
  })

  /* ---------------------------------------------------------------- 搜索 */

  const searchRef = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [listOpen, setListOpen] = useState(false)
  const [matchIndex, setMatchIndex] = useState(0)

  const nodes = useMemo(() => data?.nodes ?? [], [data])
  // 上游的 memory_type 是开放集合：新类型出现时字典里可能还没有对应词条。
  // 直接 t(`graph.type.${name}`) 会把键名原样打在页面上（例如 graph.type.WorkingMemory），
  // 未知类型统一落到“其他”，已知类型缺失时只影响这一条文案。
  const typeLabel = useCallback(
    (name: string) => {
      const key = `graph.type.${name}` as TKey
      const entry = DICTS[lang][key]
      if (!entry) return t('graph.type.Other')
      return typeof entry === 'string' ? entry : entry.other
    },
    [lang, t],
  )
  const matchIds = useMemo(
    () => matchNodes(nodes, query, { typeLabel, limit: SEARCH_LIMIT }).map((i) => nodes[i].id),
    [nodes, query, typeLabel],
  )
  const activeIndex = matchIds.length ? Math.min(matchIndex, matchIds.length - 1) : -1
  const activeMatchId = activeIndex >= 0 ? matchIds[activeIndex] : null
  const searching = query.trim().length > 0
  const showList = searching && listOpen

  const [reveal, setReveal] = useState<RevealRequest | null>(null)
  const revealNonce = useRef(0)
  const revealNode = useCallback((id: string) => {
    revealNonce.current += 1
    setReveal({ id, nonce: revealNonce.current })
  }, [])

  const openMatch = useCallback(
    (id: string) => {
      setSelected(id)
      revealNode(id)
      setListOpen(false)
      // 打开详情后把焦点交还给页面：输入框还focusing的话，Esc 会被当成
      // 「清空搜索」而不是「关抽屉」，键盘用户会觉得 Esc 失灵。
      searchRef.current?.blur()
    },
    [revealNode],
  )

  const onSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      if (showList) setListOpen(false)
      else if (query) {
        setQuery('')
        setMatchIndex(0)
      } else e.currentTarget.blur()
      return
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!matchIds.length) return
      setListOpen(true)
      setMatchIndex((i) => {
        const cur = Math.min(i, matchIds.length - 1)
        return e.key === 'ArrowDown' ? (cur + 1) % matchIds.length : (cur - 1 + matchIds.length) % matchIds.length
      })
      return
    }
    if (e.key === 'Enter' && activeMatchId) {
      e.preventDefault()
      openMatch(activeMatchId)
    }
  }

  /* ---------------------------------------------------------------- 聚焦 */

  const [focus, setFocus] = useState<GraphFocus | null>(null)
  /** 当前数据里确实存在起点的聚焦。数据和状态是两份来源：图谱刷新后某条记忆可能
   *  已经不在了，直接读 `focus` 会让胶囊上挂着一个指向不存在节点的过滤条件。
   *  在渲染时算一遍比用 effect 清状态少一轮渲染，也不会闪。 */
  const activeFocus = useMemo(() => (focus && data?.nodes.some((n) => n.id === focus.rootId) ? focus : null), [focus, data])
  /** 聚焦范围内的记忆条数。跟画布用同一份 BFS，但这里算的是**全量**图
   *  （画布按类型过滤后的那份在 Constellation 内部），所以即使某些类型被眼下的
   *  图例关掉，抽屉里的数字仍然是「这张图里 N 跳内有多少条」。 */
  const focusCount = useMemo(() => {
    if (!activeFocus || !data) return 0
    const idx = data.nodes.findIndex((n) => n.id === activeFocus.rootId)
    if (idx < 0) return 0
    return countWithin(hopDepths(data.nodes.length, indexEdges(data.nodes, data.links), idx, activeFocus.depth), activeFocus.depth)
  }, [activeFocus, data])

  /* ------------------------------------------------------------ 选择同步 */

  const selectedNode: GraphNode | undefined = useMemo(
    () => (selected ? data?.nodes.find((n) => n.id === selected) : undefined),
    [selected, data],
  )

  /** 选中的洞察记忆「归纳自几条记忆」。
   *
   *  ⚠️ 原来这里还给第二个数——「其中几条**还在这张图上**」——配一句
   *  「没画出来的那些来源记忆已经归档或删除，图上只画还活着的」。
   *  2026-10-19 用户要求去掉，原话是「为啥要写这种文案？自报家门了属于是」：
   *  那句话是在跟业务说「你的记忆被归档/删除了，我们只给你看剩下的」。
   *  留下一个总数就够了——**别再把这个数拆回去**。 */
  const sourceTotal = useMemo(
    () => (selectedNode && selectedNode.sourceIds.length > 0 ? selectedNode.sourceIds.length : null),
    [selectedNode],
  )

  /** 选中点所在会话的规模。抽屉里告诉业务「这条不是孤立的」，比只给一个 id 有用。 */
  const sessionSize = useMemo(() => {
    if (!selectedNode || !data) return 0
    if (!selectedNode.sessionId) return 1
    return data.nodes.filter((n) => n.sessionId === selectedNode.sessionId).length
  }, [selectedNode, data])

  const onSelect = useCallback((id: string | null) => setSelected(id), [])

  // `/` 聚焦搜索框，Esc 关抽屉。输入框里的 Esc 在 onSearchKeyDown 里被截住，
  // 不会再冒泡到这里，否则「清空搜索」会顺手把抽屉也关了。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      const tag = el?.tagName
      const editable = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable === true
      if (editable || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === '/') {
        e.preventDefault()
        searchRef.current?.focus()
      } else if (e.key === 'Escape' && selected) {
        setSelected(null)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selected])

  /* ------------------------------------------------------------ 抽屉全文 */

  /** 抽屉里正文的**展开态**。
   *
   *  图谱接口为了体积只给截断到 120 字的 `label`（后端在返回前截断），
   *  长记忆在抽屉里看起来就是「被截断了」。展开时才去取全文——
   *  不展开不发这次请求（用户 2026-10-19 报的：「记忆详情，记忆被截断了」）。
   *
   *  ⚠️ 三份状态都**按节点 id 存**，不在切换选中时清空：用 effect 清会在切换的那一帧
   *  闪一下上一条的全文。比较 id 就够——显示时对不上自然退回截断正文。 */
  const [fullText, setFullText] = useState<{ id: string; text: string } | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [failText, setFailText] = useState<{ id: string; msg: string } | null>(null)

  const nodeId = selectedNode?.id ?? ''
  const shownText = fullText?.id === nodeId ? fullText.text : null
  const expanding = busyId === nodeId
  const expandFail = failText?.id === nodeId ? failText.msg : ''

  const toggleFull = async () => {
    if (!selectedNode) return
    const id = selectedNode.id
    if (fullText?.id === id) {
      setFullText(null)
      return
    }
    setBusyId(id)
    setFailText(null)
    try {
      // 走**图库**取全文，不走 `/project/memory/detail`：那条是上游 REST，而洞察记忆
      // （dream 归纳出来的）在上游 `get_memory/<id>` 里查不到，会回「记忆不存在」
      // ——用户 2026-10-19 就是这么撞上的。点的是画布上的节点，全文就跟节点同源。
      const m = await api.graph.memory(id, currentProjectId || undefined)
      setFullText({ id, text: m.memory || selectedNode.label })
    } catch (e) {
      setFailText({ id, msg: errText(lang, e) })
    } finally {
      setBusyId(null)
    }
  }

  const hasGraph = state === 'ready' && !!data && data.nodes.length > 0

  return (
    <>
      <div className="flex h-full w-full flex-col gap-y-4">
        <PageHeader
          title={t('graph.title')}
          desc={currentProjectName ? `${t('graph.desc')} · ${currentProjectName}` : t('graph.desc')}
          actions={
            <button type="button" className={BTN_PAGER} onClick={reload} title={t('graph.reload')}>
              <Icon name="i-lucide:rotate-ccw" className="size-4" />
              <span className="text-xs">{t('graph.reload')}</span>
            </button>
          }
        />

        {/* 探索工具条。搜索与聚焦放在画布外：canvas 对键盘和读屏不可操作。 */}
        {hasGraph && (
          <div className="flex flex-wrap items-center gap-2">
            <div className="graph-search">
              <Icon name="i-lucide:search" className="graph-search__icon size-4" />
              <input
                ref={searchRef}
                type="search"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value)
                  // 换关键词后旧的选中项已经没有意义了，从第一条重新开始。
                  setMatchIndex(0)
                  setListOpen(true)
                }}
                onFocus={() => setListOpen(true)}
                onBlur={() => setListOpen(false)}
                onKeyDown={onSearchKeyDown}
                placeholder={t('graph.searchPlaceholder')}
                aria-label={t('graph.searchLabel')}
                role="combobox"
                aria-expanded={showList}
                aria-controls={matchIds.length > 0 ? 'graph-search-list' : undefined}
                aria-autocomplete="list"
                aria-activedescendant={showList && activeIndex >= 0 ? `graph-search-option-${activeIndex}` : undefined}
                className="graph-search__input"
              />
              {query && (
                <button
                  type="button"
                  className="graph-search__clear"
                  aria-label={t('graph.searchClear')}
                  onClick={() => {
                    setQuery('')
                    setMatchIndex(0)
                    setListOpen(false)
                    searchRef.current?.focus()
                  }}
                >
                  <Icon name="i-lucide:x" className="size-3.5" />
                </button>
              )}

              {showList && (
                <div className="graph-search__panel">
                  {matchIds.length > 0 ? (
                    <ul id="graph-search-list" role="listbox" aria-label={t('graph.searchLabel')} className="graph-search__list">
                      {matchIds.map((id, i) => {
                        const node = nodes.find((n) => n.id === id)
                        if (!node) return null
                        return (
                          <li
                            key={id}
                            id={`graph-search-option-${i}`}
                            role="option"
                            aria-selected={i === activeIndex}
                            className="graph-search__option"
                            onMouseDown={(e) => {
                              // 用 mousedown 而不是 click：blur 会先于 click 关掉列表
                              e.preventDefault()
                              openMatch(id)
                            }}
                            onMouseEnter={() => setMatchIndex(i)}
                          >
                            <span className="graph-search__dot" style={{ backgroundColor: typeColor(node.memoryType) }} />
                            <span className="graph-search__label">{node.label || node.id}</span>
                            <span className="graph-search__meta">
                              {t(`graph.type.${node.memoryType}` as 'graph.type.Other')}
                              {node.sessionId ? ` · ${node.sessionId.slice(-6)}` : ''}
                            </span>
                          </li>
                        )
                      })}
                    </ul>
                  ) : (
                    <div className="graph-search__empty">{t('graph.searchEmpty')}</div>
                  )}
                  <div className="graph-search__hint">{t('graph.searchHint')}</div>
                </div>
              )}
            </div>

            {activeFocus && (
              <div className="graph-focus-chip">
                <Icon name="i-lucide:compass" className="size-3.5" />
                <span>{t('graph.focusChip')(activeFocus.depth, focusCount)}</span>
                <button type="button" className="graph-focus-chip__clear" aria-label={t('graph.focusClear')} onClick={() => setFocus(null)}>
                  <Icon name="i-lucide:x" className="size-3.5" />
                </button>
              </div>
            )}

            <div className="sr-only" role="status" aria-live="polite">
              {searching ? (matchIds.length ? t('graph.searchResults')(matchIds.length) : t('graph.searchEmpty')) : ''}
            </div>
          </div>
        )}

        <div className="relative min-h-0 flex-1 overflow-hidden rounded-[12px] border border-line">
          {hasGraph && (
            <Constellation
              graph={data}
              onSelect={onSelect}
              selectedId={selected}
              focus={activeFocus}
              matches={matchIds}
              activeMatchId={activeMatchId}
              reveal={reveal}
            />
          )}

          {state === 'loading' && (
            <div className="graph-skeleton" aria-busy="true" aria-label={t('common.loading')}>
              {/* 星点位置写死：这一屏要表达「图正在长出来」，用随机位置反而每帧都在跳。 */}
              {SKELETON_DOTS.map((d, i) => (
                <span
                  key={i}
                  className="graph-skeleton__dot"
                  style={{ left: `${d.x}%`, top: `${d.y}%`, width: d.size, height: d.size, animationDelay: `${d.delay}ms` }}
                />
              ))}
              <div className="graph-skeleton__label">
                <span className="graph-spinner" aria-hidden="true" />
                <span>{t('common.loading')}</span>
              </div>
            </div>
          )}

          {state === 'error' && (
            <div role="alert" className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface px-8 text-center">
              <div className="text-sm text-warning">{error || t('common.failed')}</div>
              <div className="max-w-[520px] text-xs leading-5 text-ink-muted">{t('graph.errorHint')}</div>
              <button type="button" className={BTN_DIALOG_SECONDARY} onClick={reload}>
                {t('graph.retry')}
              </button>
            </div>
          )}

          {state === 'ready' && (!data || data.nodes.length === 0) && (
            <div className="absolute inset-0 flex items-center justify-center bg-surface">
              <EmptyState
                icon="i-lucide:inbox"
                title={t('graph.empty')}
                desc={t('graph.emptyHint')}
                action={
                  <button type="button" className={BTN_DIALOG_SECONDARY} onClick={reload}>
                    {t('graph.reload')}
                  </button>
                }
              />
            </div>
          )}

          {/* 详情抽屉。**覆盖**在画布上而不是挤压它**：挤压会触发 ResizeObserver，
              每开一次抽屉星云就重排一次，看着像页面在抖。 */}
          {selectedNode && (
            <aside
              role="dialog"
              aria-modal="false"
              aria-labelledby="graph-detail-title"
              className="absolute right-0 top-0 z-20 flex h-full w-[360px] flex-col border-l border-line bg-surface shadow-2xl"
            >
              <div className="flex items-center justify-between border-b border-line px-5 py-4">
                <h3 id="graph-detail-title" className="text-base font-semibold text-ink">
                  {t('graph.detailTitle')}
                </h3>
                <button
                  type="button"
                  className="graph-icon-btn cursor-pointer p-1 text-ink-secondary hover:text-ink"
                  aria-label={t('common.close')}
                  onClick={() => setSelected(null)}
                >
                  <Icon name="i-lucide:x" className="size-4" />
                </button>
              </div>

              <div className="flex-1 overflow-y-auto px-5 py-4">
                <span
                  className="mb-3 inline-flex items-center rounded-[6px] px-2 py-0.5 text-xs font-medium"
                  style={{ color: typeColor(selectedNode.memoryType), backgroundColor: `${typeColor(selectedNode.memoryType)}1f` }}
                >
                  {t(`graph.type.${selectedNode.memoryType}` as 'graph.type.Other')}
                </span>
                <div className="mb-5">
                  <p className="text-sm leading-6 break-words whitespace-pre-wrap text-ink-secondary">
                    {shownText ?? (selectedNode.label || t('graph.noContent'))}
                  </p>
                  {/* 「展开全文」只在**真的被截断**时才出现（BFF 现算的 `labelTruncated`）。
                      正文不长的记忆这里什么都不显示——摆一个点了没反应的按钮比不摆更糟。 */}
                  {selectedNode.labelTruncated && (
                    <div className="mt-1.5 flex items-center gap-3">
                      <button type="button" className={BTN_TEXT_LINK} disabled={expanding} onClick={toggleFull}>
                        {expanding ? t('common.loading') : shownText !== null ? t('graph.collapse') : t('graph.expand')}
                      </button>
                      {expandFail && <span className="text-xs leading-5 text-warning">{expandFail}</span>}
                    </div>
                  )}
                </div>

                <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2.5 text-sm">
                  <dt className="text-ink-muted">{t('memory.col.createdAt')}</dt>
                  <dd className="text-ink-secondary">{fmtTime(selectedNode.createdAt)}</dd>

                  <dt className="text-ink-muted">{t('graph.fieldSession')}</dt>
                  <dd className="font-mono text-xs break-all text-ink-secondary">{selectedNode.sessionId || t('graph.noSession')}</dd>

                  <dt className="text-ink-muted">{t('graph.fieldSessionSize')}</dt>
                  <dd className="text-ink-secondary">{t('graph.sessionSize')(sessionSize)}</dd>

                  {sourceTotal !== null && (
                    <>
                      <dt className="text-ink-muted">{t('graph.fieldSources')}</dt>
                      <dd className="text-ink-secondary">{t('graph.sources')(sourceTotal)}</dd>
                    </>
                  )}

                  <dt className="text-ink-muted">{t('graph.fieldTenant')}</dt>
                  <dd className="break-all text-ink-secondary">{selectedNode.userId || '-'}</dd>

                  <dt className="text-ink-muted">{t('memory.detailId')}</dt>
                  <dd className="font-mono text-xs break-all text-ink-secondary">{selectedNode.id}</dd>
                </dl>

                {/* 关系聚焦：从这条记忆出发，只留下 N 跳内的节点。
                    跳数用 1/2/3 三档而不是滑杆——超过 3 跳在大多数会话里已经是全图，
                    再多的档位只会让人反复试。 */}
                <div className="mt-6 border-t border-line pt-4">
                  <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-muted">{t('graph.focus')}</div>
                  {activeFocus?.rootId === selectedNode.id ? (
                    <>
                      <div className="mb-2 flex items-center gap-1.5" role="group" aria-label={t('graph.focusDepthLabel')}>
                        {[1, 2, 3].map((d) => (
                          <button
                            key={d}
                            type="button"
                            aria-pressed={activeFocus.depth === d}
                            className="graph-depth-btn"
                            onClick={() => setFocus({ rootId: selectedNode.id, depth: d })}
                          >
                            {t('graph.focusDepth')(d)}
                          </button>
                        ))}
                      </div>
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
                        <span>{t('graph.focusRange')(focusCount, activeFocus.depth)}</span>
                        <button type="button" className={BTN_TEXT_LINK} onClick={() => setFocus(null)}>
                          {t('graph.focusClear')}
                        </button>
                      </div>
                    </>
                  ) : (
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <button
                        type="button"
                        className={BTN_TEXT_LINK}
                        onClick={() => {
                          setFocus({ rootId: selectedNode.id, depth: 1 })
                          revealNode(selectedNode.id)
                        }}
                      >
                        <Icon name="i-lucide:compass" className="size-3.5" />
                        {t('graph.focusNeighbors')}
                      </button>
                      {activeFocus && (
                        <button type="button" className={BTN_TEXT_LINK} onClick={() => setFocus(null)}>
                          {t('graph.focusClear')}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              </div>

              <div className="border-t border-line px-5 py-4">
                <button type="button" className={BTN_DIALOG_SECONDARY} onClick={() => setSelected(null)}>
                  {t('common.close')}
                </button>
              </div>
            </aside>
          )}
        </div>

        {/* 这里原来有一行「共 N 条记忆、M 条连线；分布在 K 个会话里……」的统计说明。
            2026-10-19 用户要求删掉：这些数字画布左下角的 HUD 里本来就有（记忆/可见/连线/缩放），
            图例上还有逐类型的条数，再来一行长句只是把画布挤矮。 */}
      </div>
    </>
  )
}
