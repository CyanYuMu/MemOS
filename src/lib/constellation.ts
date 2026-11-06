/** 记忆星座图的布局与配色。**纯函数，不碰 DOM** —— 画布在 components/Constellation.tsx。
 *
 *  做法参照 Hindsight 的 `constellation.tsx`（`vectorize-io/hindsight`，MIT，
 *  hindsight-control-plane/src/components/constellation.tsx）。**照搬的是它的核心判断：
 *  这里不做力导向模拟。**
 *
 *  为什么不用 d3-force：力导向要迭代收敛，1000+ 节点每帧重算会掉帧，而收敛之后
 *  布局仍在微动（点会漂）。Hindsight 用的是「**确定性播种 + 环境漂移**」：
 *    - 位置由 id 哈希和簇归属一次算定（同一份数据每次画出来完全一样）；
 *    - 视觉上的「活」来自叠加在固定位置上的正弦漂移 / 呼吸 / 闪烁，不改变拓扑。
 *  于是首帧就是稳定星图，缩放平移也不重排。代价是没有「点之间互相推开」的效果，
 *  用一次性的松弛迭代（只跑一遍、不在渲染循环里）替代。
 */

import type { GraphLink, GraphNode, MemoryGraph } from './api'

/* ------------------------------------------------------------------ 类型 */

export type LinkKind = GraphLink['kind']

export interface PlacedNode {
  id: string
  label: string
  memoryType: string
  sessionId: string
  clusterIndex: number
  /** 世界坐标（渲染时再乘缩放、加漂移） */
  x: number
  y: number
  /** 基础半径（未乘缩放与脉动） */
  r: number
  color: string
  linkCount: number
  /** 该节点的环境漂移相位，让呼吸/闪烁错开而不是整片同步 */
  phase: number
}

export interface PlacedLink {
  a: number
  b: number
  kind: LinkKind
  color: string
}

export interface PlacedCluster {
  key: string
  /** 会话 id 的**尾 6 位**（`conv_<16 位十六进制>` 整条塞不进胶囊）。
   *
   *  ⚠️ 这里只带原料、**不拼文案**：这一层拿不到语言。原来它在布局里拼成
   *  `#a62293 · 7` 画到画布上，2026-10-19 用户的第一反应是
   *  「可视化图谱上的这个编号是啥意思？比如说 #a62293·7，没看懂」——
   *  没有单位、也没说这是个会话。文案现在在 Constellation.tsx 走字典取词
   *  （`graph.clusterLabel`）。 */
  sessionTail: string
  color: string
  members: number[]
  x: number
  y: number
  r: number
  count: number
  /** 凸包的**成员下标**（不是坐标），建布局时算一次。
   *
   *  ⚠️ 原来每帧都对可见点重算一次凸包（凸包要先排序，1000 个节点、几百个
   *  会话时是每帧最大的开销之一）。点的漂移只有 ±16 世界像素，凸包的**顶点集合**
   *  几乎不会变，所以只算一次、渲染时按这些点的当帧屏幕坐标连线即可。
   *  这样区域色块还会跟着每个点各自的漂移走，不会和点脱开。 */
  hull: number[]
  /** 成员到区域中心的最大世界距离。成员 <3 时画圆用它，不再逐帧遍历成员。 */
  spread: number
}

export interface Layout {
  nodes: PlacedNode[]
  links: PlacedLink[]
  /** 每个点关联的边下标。悬停时要「只画这个点的线」，逐帧全表扫描太浪费 */
  linksByNode: number[][]
  clusters: PlacedCluster[]
  /** 最远节点到原点的距离，用来算初始自适应缩放 */
  maxR: number
}

/* ------------------------------------------------------------------ 配色 */

/**
 * 记忆类型 → 颜色。这六个类型是全库实测出来的（`memory_type` 的取值集合）。
 *
 * ⚠️ 色阶是按**白底**选的。原来的六个值（#7c8cff / #3fc7c0 / #f5a742 / #b18cf5 /
 * #f472b6 / #94a3b8）是给深色星空底配的粉彩色，压在白底上会淡成一片（2026-10-19
 * 画布改亮色后实测的），所以整体压深约两成——色相不动，图例、悬停卡、抽屉徽标
 * 共用这一份，改一处全都跟着变。
 */
const TYPE_COLORS: Record<string, string> = {
  PreferenceMemory: '#5b6cf5',
  LongTermMemory: '#17a8a0',
  UserMemory: '#e08b1f',
  Context: '#9469f0',
  InsightMemory: '#ea4d97',
  ToolTrajectoryMemory: '#7c8ca3',
}

export const typeColor = (name: string): string => TYPE_COLORS[name] ?? '#7c8ca3'

/** 连线颜色。三种边语义不同，颜色必须能分开：
 *  `session` 是我们按 `session_id` 聚合出来的星形连线（不是图库里存的边）。 */
export const LINK_COLORS: Record<LinkKind, string> = {
  session: '#4a7fe0',
  parent: '#17a8a0',
  merged: '#9469f0',
  /** 归纳边（`source_memory_ids`）。用比 `merged` 更暗的紫，且画得更淡——
   *  它是**属性补出来的**关系，数量也可能最多，不能和真实存在的关系边抢注意力。 */
  derived: '#7c5cd6',
}

export function hexToRgba(hex: string, alpha: number): string {
  if (!hex.startsWith('#') || hex.length < 7) return hex
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${alpha})`
}

/* ------------------------------------------------------------------ 工具 */

/** 32 位整数哈希（跟 Hindsight 用的是同一个）。用于确定性播种。 */
export function hashStr(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return h
}

/** 哈希 → [0,1) */
const hash01 = (h: number): number => (Math.abs(h) % 1000) / 1000

/** 单调链凸包（逆时针），输入输出都是**节点下标**。用来把一簇点包成一块柔和的区域；
 *  少于 3 个点时没有多边形，调用方改画圆。
 *
 *  画布上的区域色块需要「哪些点在外圈」，而不是凸包坐标本身：位置每帧都会
 *  加上漂移，存下标就能直接取当帧屏幕坐标，既不用每帧重算凸包，也不会和点脱开。 */
function hullIndices(nodes: PlacedNode[], members: number[]): number[] {
  if (members.length < 3) return []
  const pts = members.slice().sort((a, b) => nodes[a].x - nodes[b].x || nodes[a].y - nodes[b].y || a - b)
  const x = (i: number) => nodes[i].x
  const y = (i: number) => nodes[i].y
  const cross = (o: number, a: number, b: number) => (x(a) - x(o)) * (y(b) - y(o)) - (y(a) - y(o)) * (x(b) - x(o))
  const lower: number[] = []
  for (const p of pts) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop()
    lower.push(p)
  }
  const upper: number[] = []
  for (let i = pts.length - 1; i >= 0; i--) {
    const p = pts[i]
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop()
    upper.push(p)
  }
  lower.pop()
  upper.pop()
  return lower.concat(upper)
}

/** 会画成「有标签的区域」的最小规模。1~2 个点的会话画成色块只是噪点。 */
const CLUSTER_MIN = 3
/** 黄金角。用它散布点，任意两个相邻序号的角度都不会重合。 */
const GOLDEN = 2.39996323

/** 一簇（一个会话 / 一颗散点）需要多大的地盘。开方是为了别让大会话把地盘吃光。 */
const groupRadius = (size: number) => 14 + 4.2 * Math.sqrt(size)

const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : a)

/** 螺旋槽位的步长，用来把「按大小排好序」的簇**打散**到整个盘面上。
 *
 *  直接按大小顺序落位会把最大的那几十个簇全塞进圆心附近：螺旋半径是
 *  `sqrt(累计面积)`，于是前 10% 的槽位只占 1% 的面积。而恰好是这些大簇带标签，
 *  结果就是一坨糊在一起的胶囊。取一个与簇数互质的黄金比例步长做跳跃，
 *  相邻的大小就会落到相距很远的槽位上，大小簇在整个盘面上均匀混合。 */
function strideOf(n: number): number {
  if (n <= 2) return 1
  let s = Math.max(1, Math.round(n * 0.618))
  while (s < n && gcd(s, n) !== 1) s++
  return s >= n ? 1 : s
}

/* -------------------------------------------------------------- 主布局 */

export function buildLayout(graph: MemoryGraph): Layout {
  const count = graph.nodes.length
  if (!count) return { nodes: [], links: [], linksByNode: [], clusters: [], maxR: 0 }

  /* 1) 分簇。BFF 已经给好 clusterId（会话，或每颗散点自成一簇），这里只按它归组。 */
  const groups = new Map<string, number[]>()
  graph.nodes.forEach((n, i) => {
    const arr = groups.get(n.clusterId)
    if (arr) arr.push(i)
    else groups.set(n.clusterId, [i])
  })

  /* 2) 地盘与顺序。按规模降序排，但**落位顺序被打散**（见 strideOf）——
   *    排序保证松弛时大地盘先占位，打散保证它们不都堆在圆心。 */
  const items = [...groups.entries()].map(([key, members]) => ({ key, members, r: groupRadius(members.length), x: 0, y: 0 }))
  items.sort((a, b) => b.members.length - a.members.length || (a.key < b.key ? -1 : 1))

  /* 3) 黄金角螺旋 + 面积累加。让每个地盘分到的**面积**正比于它的面积，
   *    这样整片星云是均匀铺开的，不会中间挤成一坨、外面一圈空。 */
  const N = items.length
  const stride = strideOf(N)
  const slotItem: number[] = new Array(N)
  for (let i = 0; i < N; i++) slotItem[(i * stride) % N] = i

  let area = 0
  for (let slot = 0; slot < N; slot++) {
    const g = items[slotItem[slot]]
    const before = area
    area += Math.PI * g.r * g.r
    if (slot === 0) {
      g.x = 0
      g.y = 0
      continue
    }
    const rad = 1.16 * Math.sqrt((before + (Math.PI * g.r * g.r) / 2) / Math.PI)
    const a = slot * GOLDEN
    g.x = Math.cos(a) * rad
    g.y = Math.sin(a) * rad
  }

  /* 4) 松弛：把重叠的地盘推开。**只在建布局时跑一次**，不在渲染循环里。
   *    用格子做邻居查询，613 个簇 × 24 轮也只是一次几十毫秒的一次性开销。 */
  const maxR = items.reduce((m, g) => Math.max(m, g.r), 0)
  const cell = 2 * maxR
  for (let iter = 0; iter < 24; iter++) {
    const grid = new Map<string, number[]>()
    items.forEach((g, i) => {
      const k = `${Math.floor(g.x / cell)},${Math.floor(g.y / cell)}`
      const arr = grid.get(k)
      if (arr) arr.push(i)
      else grid.set(k, [i])
    })
    let moved = 0
    for (let i = 0; i < items.length; i++) {
      const gi = items[i]
      const cxi = Math.floor(gi.x / cell)
      const cyi = Math.floor(gi.y / cell)
      for (let dx = -1; dx <= 1; dx++) {
        for (let dy = -1; dy <= 1; dy++) {
          const bucket = grid.get(`${cxi + dx},${cyi + dy}`)
          if (!bucket) continue
          for (const j of bucket) {
            if (j <= i) continue
            const gj = items[j]
            let ox = gj.x - gi.x
            let oy = gj.y - gi.y
            let d = Math.hypot(ox, oy)
            const need = gi.r + gj.r + 6
            if (d >= need) continue
            if (d < 0.01) {
              // 完全重合时给一个确定的分离方向，别用随机数（会破坏「每次画都一样」）
              ox = 1
              oy = 0
              d = 1
            }
            // 每轮最多挪 6px：一次推太多会让相邻的地盘来回弹，永远收敛不了
            const push = Math.min((need - d) / 2, 6)
            const ux = (ox / d) * push
            const uy = (oy / d) * push
            gi.x -= ux
            gi.y -= uy
            gj.x += ux
            gj.y += uy
            moved++
          }
        }
      }
    }
    if (!moved) break
  }

  /* 5) 连线：先把每条边的两端索引化，再数每个点有几条边（星等靠它定）。 */
  const index = new Map<string, number>()
  graph.nodes.forEach((n, i) => index.set(n.id, i))

  const links: PlacedLink[] = []
  const linkCount = new Int32Array(count)
  const linksByNode: number[][] = Array.from({ length: count }, () => [])
  for (const l of graph.links) {
    const a = index.get(l.source)
    const b = index.get(l.target)
    if (a === undefined || b === undefined || a === b) continue
    linksByNode[a].push(links.length)
    linksByNode[b].push(links.length)
    links.push({ a, b, kind: l.kind, color: LINK_COLORS[l.kind] ?? LINK_COLORS.session })
    linkCount[a]++
    linkCount[b]++
  }

  /* 6) 簇内落点。中心点是该会话最早的那条（BFF 已按时间升序排好），
   *    其余按同心环铺开——环数随规模增长，否则 34 个点挤在一圈上会糊成一坨。 */
  const nodes: PlacedNode[] = new Array(count)
  const clusters: PlacedCluster[] = []

  items.forEach((g, ci) => {
    const size = g.members.length
    const typeCount = new Map<string, number>()
    const rings = size <= 6 ? 1 : size <= 14 ? 2 : size <= 26 ? 3 : 4
    const perRing = new Array(rings).fill(0)
    const seat: Array<{ ring: number; nth: number }> = []
    // 中心点（第 0 个）不占环位，其余按环均分
    for (let k = 1; k < size; k++) {
      const ring = Math.min(rings - 1, Math.floor(((k - 1) * rings) / Math.max(size - 1, 1)))
      seat.push({ ring, nth: perRing[ring]++ })
    }
    g.members.forEach((ni, k) => {
      const node: GraphNode = graph.nodes[ni]
      const seed = hashStr(node.id)
      const h1 = hash01(seed)
      const h2 = hash01(seed >> 7)
      let x = g.x
      let y = g.y
      if (k > 0) {
        const s = seat[k - 1]
        const total = perRing[s.ring]
        const ang = (s.nth / Math.max(total, 1)) * Math.PI * 2 + (h1 - 0.5) * 0.5 + s.ring * 0.7
        // 收紧到 0.84 倍：簇要看起来是「一撮星」，摊得太开就成了一张网
        const rf = rings === 1 ? 0.6 : 0.34 + (0.5 * s.ring) / (rings - 1)
        const rad = g.r * rf * (0.88 + 0.24 * h2)
        x = g.x + Math.cos(ang) * rad
        y = g.y + Math.sin(ang) * rad
      }
      typeCount.set(node.memoryType, (typeCount.get(node.memoryType) ?? 0) + 1)
      nodes[ni] = {
        id: node.id,
        label: node.label,
        memoryType: node.memoryType,
        sessionId: node.sessionId,
        clusterIndex: ci,
        x,
        y,
        r: 2.5 + Math.min(linkCount[ni] * 0.15, 2.5),
        // ⚠️ 颜色**只**编类型，不再往白色提亮。
        //    原来这里叠了一层 `brighten(typeColor, 0.45·√(link/linkMax))`，表达
        //    「关系越多越亮」——那在深色底上成立，白底上是**反的**（越提亮越看不见）。
        //    关系数现在只由画布上的**尺寸 + 极淡辉光**表达，颜色留给类型这一维。
        color: typeColor(node.memoryType),
        linkCount: linkCount[ni],
        phase: h1 * Math.PI * 2,
      }
    })

    if (size >= CLUSTER_MIN) {
      const dominant = [...typeCount.entries()].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))[0][0]
      let spread = 0
      for (const ni of g.members) spread = Math.max(spread, Math.hypot(nodes[ni].x - g.x, nodes[ni].y - g.y))
      clusters.push({
        key: g.key,
        sessionTail: g.key.replace(/^session:/, '').slice(-6),
        color: typeColor(dominant),
        members: g.members,
        x: g.x,
        y: g.y,
        r: g.r,
        count: size,
        hull: hullIndices(nodes, g.members),
        spread,
      })
    }
  })

  /* 7) 以质心为原点。不平移的话整片星云偏在一边，初始自适应缩放会白留一半空白。 */
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const n of nodes) {
    if (n.x < minX) minX = n.x
    if (n.x > maxX) maxX = n.x
    if (n.y < minY) minY = n.y
    if (n.y > maxY) maxY = n.y
  }
  const ox = (minX + maxX) / 2
  const oy = (minY + maxY) / 2
  let far = 0
  for (const n of nodes) {
    n.x -= ox
    n.y -= oy
    far = Math.max(far, Math.hypot(n.x, n.y))
  }
  for (const c of clusters) {
    c.x -= ox
    c.y -= oy
  }

  return { nodes, links, linksByNode, clusters, maxR: far }
}
