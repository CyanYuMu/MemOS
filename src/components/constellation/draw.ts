/** 星座图的绘制层：只碰 canvas，不碰 React。
 *
 *  拆出来的原因：`Constellation.tsx` 原来一个 `animate()` 里同时做坐标投影、命中、
 *  hover 状态和五种图元，改一处颜色要在 300 行里找上下文。这里全部是**纯函数**，
 *  输入是布局 + 相机 + 这一帧的坐标，输出只落在 canvas 上。
 *
 *  1000 节点 / 3000 边时的三处关键优化（都在这一层）：
 *    1. **复用 Frame 缓冲**：`projectNodes` 写入调用方给的 TypedArray，不再每帧
 *       分配三个数组（60fps 下是持续不断的 GC 压力）；
 *    2. **视口裁剪**：点用 `visible` 标志；边先看两端屏幕坐标；区域色块先看
 *       包围圆。区域凸包在 `buildLayout` 里只算一次（存的是成员下标），
 *       这里不再每帧排序；
 *    3. **按颜色合并描边**：静止态把几千条边按「种类 + 是否压暗」分成最多 8 组，
 *       每组一条路径、一次 `stroke()`，而不是几千次。
 */

import type { TFn } from '../../lib/store'
import { hexToRgba, type Layout } from '../../lib/constellation'

/** 画布上会用到的那几个颜色。全部集中在这里，换主题只改这一处。
 *
 *  ⚠️ 画布是**直接往 canvas 上写颜色**的，不受 Tailwind 管，改了主题别忘了同步
 *  `scripts/verify-graph.mjs` 的像素判据——那份判据原来是「找亮像素」，
 *  在亮色底下会把**整个背景**都算成点（白底就是最亮的），断言直接失效。 */
export const INK = {
  bg: '#ffffff',
  /** 画布左下角那行状态文字 */
  hud: '#94a3b8',
  label: '#64748b',
  labelHover: '#1e293b',
  /** 选中点的描边。深色底下是白圈，亮色底下必须反过来 */
  ring: '#1e293b',
  /** 搜索命中 / 聚焦起点的描边。跟控制台主色一致，不跟记忆类型抢色相。 */
  matchRing: '#4f46e5',
  /** 区域胶囊上的字：胶囊是实色饱和块，字用近黑 */
  pillText: '#0b1120',
  miniDot: 'rgba(100,116,139,0.45)',
  miniView: 'rgba(64,68,237,0.85)',
} as const

export type CanvasTheme = 'light' | 'dark'

/** 暗色画布。画布是直接往 canvas 上写颜色的，Tailwind 管不到，
 *  所以主题切换要显式换一组值。亮色底上的粉彩色在深色底会发灰，反之亦然。 */
export const INK_DARK = {
  bg: '#0b0e14',
  hud: '#64718a',
  label: '#8b97ad',
  labelHover: '#e9edf5',
  // 亮色底下描边要深、暗色底下要浅，否则选中点看不见
  ring: '#e9edf5',
  matchRing: '#a5b4fc',
  // 胶囊仍是饱和色块，字保持近黑
  pillText: '#0b1020',
  miniDot: 'rgba(139,151,173,0.45)',
  miniView: 'rgba(129,140,248,0.9)',
} as const

export const palette = (theme: CanvasTheme) => (theme === 'dark' ? INK_DARK : INK)

/** 环境漂移幅度（世界坐标）。原版 16，这里按比例给——我们的场半径约 700，同量级合适。 */
const DRIFT_AMP = 16

/** 缩略图边长 */
export const MINIMAP = 132

/** 每帧最多画多少个文字标签。高倍缩放下否则会尝试放置上千个。 */
const MAX_LABELS = 140

export interface Camera {
  panX: number
  panY: number
  zoom: number
  tPanX: number
  tPanY: number
  tZoom: number
  mouseX: number
  mouseY: number
  dragging: boolean
  dragStartX: number
  dragStartY: number
  panStartX: number
  panStartY: number
  hoverIndex: number
  prevHoverIndex: number
  W: number
  H: number
  dpr: number
  fit: number
}

/** 这一帧所有节点的屏幕坐标。点击命中与绘制共用同一份，避免各算一遍。 */
export interface Frame {
  sx: Float32Array
  sy: Float32Array
  visible: Uint8Array
  n: number
}

export const emptyFrame = (): Frame => ({ sx: new Float32Array(0), sy: new Float32Array(0), visible: new Uint8Array(0), n: 0 })

/** 搜索 / 聚焦的渲染状态。长度都跟当前布局的节点数一致，由调用方复用。 */
export interface Overlay {
  /** 搜索命中：1 = 命中 */
  match: Uint8Array
  /** 是否处于搜索态（决定要不要压暗未命中节点） */
  searching: boolean
  /** 当前激活的搜索结果下标；-1 = 没有 */
  active: number
  /** 聚焦跳数：0 = 起点，正数 = 跳数，-1 = 范围外/不可达；null = 没开聚焦 */
  depths: Int16Array | null
  /** 聚焦的最大跳数 */
  depth: number
  /** 聚焦起点下标；-1 = 起点不在当前画布上 */
  root: number
}

/** 世界坐标 → 屏幕坐标。漂移直接烘进屏幕坐标，连线从同一组坐标取值，于是线会跟着点一起动。
 *
 *  `out` 用来复用缓冲：节点数没变时不要再分配数组（见文件头）。 */
export function projectNodes(L: Layout, s: Camera, time: number, out?: Frame): Frame {
  const { W, H, zoom, panX, panY } = s
  const cx = W / 2 + panX
  const cy = H / 2 + panY
  const margin = 60
  const n = L.nodes.length
  const frame: Frame =
    out && out.sx.length === n && out.sy.length === n && out.visible.length === n
      ? out
      : { sx: new Float32Array(n), sy: new Float32Array(n), visible: new Uint8Array(n), n }
  const { sx, sy, visible } = frame
  for (let i = 0; i < n; i++) {
    const nd = L.nodes[i]
    const dx = DRIFT_AMP * Math.sin(time * 0.6 + nd.phase)
    const dy = DRIFT_AMP * Math.cos(time * 0.5 + nd.phase * 1.3)
    const px = cx + (nd.x + dx) * zoom
    const py = cy + (nd.y + dy) * zoom
    sx[i] = px
    sy[i] = py
    visible[i] = px > -margin && px < W + margin && py > -margin && py < H + margin ? 1 : 0
  }
  frame.n = n
  return frame
}

/** 找离 (x,y) 最近的点。缩放大时判定半径放宽（点看着大，手感也要跟着大）。 */
export function hitTest(frame: Frame, x: number, y: number, zoom: number): number {
  let best = zoom > 1.5 ? 80 : 30
  let idx = -1
  for (let i = 0; i < frame.n; i++) {
    if (!frame.visible[i]) continue
    const d = Math.hypot(x - frame.sx[i], y - frame.sy[i])
    if (d < best) {
      best = d
      idx = i
    }
  }
  return idx
}

/** 哪些节点属于「悬停点的直接邻居」。每帧只算一次，避免在节点循环里逐个 `.some()`。 */
function neighborMask(L: Layout, hoverIndex: number, n: number): Uint8Array | null {
  if (hoverIndex < 0) return null
  const mask = new Uint8Array(n)
  for (const li of L.linksByNode[hoverIndex] ?? []) {
    const lk = L.links[li]
    mask[lk.a] = 1
    mask[lk.b] = 1
  }
  mask[hoverIndex] = 1
  return mask
}

interface DrawArgs {
  ctx: CanvasRenderingContext2D
  L: Layout
  s: Camera
  frame: Frame
  time: number
  hoverIndex: number
  selectedId?: string | null
  t: TFn
  theme: CanvasTheme
  overlay?: Overlay
}

/** 画一帧：连线 → 区域色块 → 点 → 区域标签 → HUD。层叠顺序是刻意的，见各段注释。 */
export function drawScene({ ctx, L, s, frame, time, hoverIndex, selectedId, t, theme, overlay }: DrawArgs): void {
  const C = palette(theme)
  const { W, H, zoom } = s
  const { sx, sy, visible, n } = frame
  const margin = 60
  const focusOn = overlay != null && overlay.depths != null && overlay.root >= 0 && overlay.depth >= 0
  const inFocus = (i: number) => !focusOn || (overlay!.depths![i] >= 0 && overlay!.depths![i] <= overlay!.depth)
  const searchOn = overlay != null && overlay.searching
  const isMatch = (i: number) => overlay != null && overlay.match[i] === 1

  ctx.setTransform(s.dpr, 0, 0, s.dpr, 0, 0)
  ctx.clearRect(0, 0, W, H)

  const neighbors = neighborMask(L, hoverIndex, n)
  const hovered = new Set<number>()
  if (hoverIndex >= 0) for (const li of L.linksByNode[hoverIndex] ?? []) hovered.add(li)

  /* ---- 连线 ---- */
  if (hoverIndex >= 0) {
    ctx.lineWidth = 1.5
    for (const li of hovered) {
      const lk = L.links[li]
      const ax = sx[lk.a]
      const ay = sy[lk.a]
      const bx = sx[lk.b]
      const by = sy[lk.b]
      const mx = (ax + bx) / 2 + (by - ay) * 0.08
      const my = (ay + by) / 2 - (bx - ax) * 0.08
      ctx.strokeStyle = lk.color
      ctx.globalAlpha = 0.5
      ctx.beginPath()
      ctx.moveTo(ax, ay)
      ctx.quadraticCurveTo(mx, my, bx, by)
      ctx.stroke()

      // 光珠：沿曲线从悬停点向外跑，相位按边的序号错开，读起来像信号在流动
      const from = lk.a === hoverIndex
      const raw = (time * 0.22 + (li % 13) / 13) % 1
      const u = from ? raw : 1 - raw
      const iu = 1 - u
      const px = iu * iu * ax + 2 * iu * u * mx + u * u * bx
      const py = iu * iu * ay + 2 * iu * u * my + u * u * by
      ctx.globalAlpha = 0.9 * (0.4 + 0.6 * Math.sin(u * Math.PI))
      ctx.fillStyle = lk.color
      ctx.beginPath()
      ctx.arc(px, py, 2, 0, Math.PI * 2)
      ctx.fill()
    }
    ctx.globalAlpha = 1
  } else {
    // 静止态：整张网极淡地一起呼吸（一个全局正弦，不是逐边——逐边会闪得眼疼）
    const shimmer = 1 + 0.18 * Math.sin(time * 0.6)
    // 白底上同样的不透明度看起来比黑底**淡一档**（底色本身亮），所以整体抬高一点
    const alpha = (0.16 + Math.min(zoom * 0.1, 0.18)) * shimmer
    ctx.lineWidth = 0.5
    // 按「种类 + 是否压暗」分组，一组一条路径、一次描边。3000 条边的场里
    // 这是单帧最大的一笔节省（几千次 stroke → 最多 8 次）。
    const buckets = new Map<string, { color: string; alpha: number; pts: number[] }>()
    for (const lk of L.links) {
      const ax = sx[lk.a]
      const ay = sy[lk.a]
      const bx = sx[lk.b]
      const by = sy[lk.b]
      if ((ax < -margin && bx < -margin) || (ax > W + margin && bx > W + margin)) continue
      if ((ay < -margin && by < -margin) || (ay > H + margin && by > H + margin)) continue
      // 聚焦时只让「两端都在范围内」的边保持正常亮度，其余压到几乎看不见。
      // 这样「从某条记忆出发能连到谁」是一眼能读出来的，而不是靠数节点。
      const dim = focusOn && !(inFocus(lk.a) && inFocus(lk.b))
      const a = (lk.kind === 'derived' ? alpha * 0.72 : alpha) * (dim ? 0.14 : 1)
      const key = `${lk.kind}:${dim ? 1 : 0}`
      let bucket = buckets.get(key)
      if (!bucket) {
        bucket = { color: lk.color, alpha: a, pts: [] }
        buckets.set(key, bucket)
      }
      const mx = (ax + bx) / 2 + (by - ay) * 0.08
      const my = (ay + by) / 2 - (bx - ax) * 0.08
      bucket.pts.push(ax, ay, mx, my, bx, by)
    }
    for (const b of buckets.values()) {
      ctx.strokeStyle = b.color
      ctx.globalAlpha = b.alpha
      ctx.beginPath()
      for (let i = 0; i < b.pts.length; i += 6) {
        ctx.moveTo(b.pts[i], b.pts[i + 1])
        ctx.quadraticCurveTo(b.pts[i + 2], b.pts[i + 3], b.pts[i + 4], b.pts[i + 5])
      }
      ctx.stroke()
    }
    ctx.globalAlpha = 1
  }

  /* ---- 区域色块（画在点下面）---- */
  const pad = 13
  /* 区域标签的重叠检测。65 个区域挤在一起时，不做这层过滤就是一屏叠在一起的胶囊。
   * 只跟已放下的标签比矩形，O(n²) 也就几千次比较。 */
  const placedLabels: Array<[number, number, number, number]> = []
  /** 本帧要画的区域标签。点画完之后才真正落到画布上，见下面的说明。 */
  const pendingLabels: Array<{ x: number; y: number; w: number; cx: number; ty: number; text: string; color: string; alpha: number }> = []
  // 悬停时色块跟着一起暗，否则会出现「点都暗了、色块还亮着」的割裂感
  const hullDim = hoverIndex >= 0 ? 0.35 : 1
  if (L.clusters.length) {
    ctx.lineJoin = 'round'
    for (const c of L.clusters) {
      // 视口裁剪：先看区域的包围圆，整块都在屏外就不做后面的坐标收集。
      const ccx = W / 2 + s.panX + c.x * zoom
      const ccy = H / 2 + s.panY + c.y * zoom
      const crad = (c.spread + DRIFT_AMP) * zoom + pad
      if (ccx + crad < -margin || ccx - crad > W + margin || ccy + crad < -margin || ccy - crad > H + margin) continue

      let dim = hullDim
      if (focusOn) {
        let any = false
        for (const m of c.members) {
          if (inFocus(m)) {
            any = true
            break
          }
        }
        if (!any) dim *= 0.18
      }
      if (searchOn) {
        let any = false
        for (const m of c.members) {
          if (isMatch(m)) {
            any = true
            break
          }
        }
        if (!any) dim *= 0.3
      }

      const hullIds = c.hull
      const drawPoly = hullIds.length >= 3
      const pts: Array<[number, number]> = drawPoly ? hullIds.map((i) => [sx[i], sy[i]]) : []
      let mx: number
      let my: number
      if (drawPoly) {
        mx = 0
        my = 0
        for (const [px, py] of pts) {
          mx += px
          my += py
        }
        mx /= pts.length
        my /= pts.length
      } else {
        mx = ccx
        my = ccy
      }

      ctx.fillStyle = hexToRgba(c.color, 0.13 * dim)
      ctx.strokeStyle = hexToRgba(c.color, 0.5 * dim)
      ctx.lineWidth = 1.25
      ctx.beginPath()
      let rad = pad
      if (!drawPoly) {
        // 1~2 个成员的会话没有凸包，画一个圆。半径在建布局时算好（`spread`），
        // 不再逐帧遍历成员。
        rad = Math.max(pad, c.spread * zoom + pad)
        ctx.arc(ccx, ccy, rad, 0, Math.PI * 2)
      } else {
        // 凸包顶点再沿质心方向外推 pad，得到圆润的色块边界
        pts.forEach(([hx, hy], j) => {
          const dx = hx - mx
          const dy = hy - my
          const d = Math.hypot(dx, dy) || 1
          const ex = hx + (dx / d) * pad
          const ey = hy + (dy / d) * pad
          if (j === 0) ctx.moveTo(ex, ey)
          else ctx.lineTo(ex, ey)
        })
        ctx.closePath()
      }
      ctx.fill()
      ctx.stroke()

      /* 区域标签：顶部一颗实色胶囊。缩放太小时不画——会变成一屏小色块。
       *
       * ⚠️ 这里**只登记不画**：点是在区域之后画的，胶囊若也在这一步画，就会被
       * 后画的点盖住（实测有些标签的「#」正好被一颗点压掉，看着像糊了）。
       * 所以层叠顺序是「区域 → 点 → 标签」，胶囊留到点画完再补。 */
      if (zoom > s.fit * 0.9) {
        let topY = my
        if (drawPoly) for (const [, py] of pts) topY = Math.min(topY, py)
        else topY = ccy - rad
        const ly = topY - pad - 12
        ctx.font = '600 11px ui-sans-serif, system-ui, sans-serif'
        // 胶囊文案走字典：「会话 a62293 · 7 条记忆」。原来这里画的是布局层拼好的
        // `#a62293 · 7`——一个没有单位、也没说这是会话的编号（用户 2026-10-19 没看懂）。
        const text = t('graph.clusterLabel')(c.sessionTail, c.count)
        const tw = ctx.measureText(text).width
        const pw = tw + 16
        const lx0 = mx - pw / 2
        const ly0 = ly - 9.5
        // 跟已放下的标签比矩形，压上了就不画这一颗（宁可少几个标签，也不要一屏叠字）
        const hit = placedLabels.some(([x, y, w, h]) => lx0 < x + w && lx0 + pw > x && ly0 < y + h && ly0 + 19 > y)
        if (!hit) {
          placedLabels.push([lx0, ly0, pw, 19])
          // 胶囊跟所在区域一起压暗：搜索/聚焦时范围外的「会话 · 条数」不应该比点还亮
          pendingLabels.push({ x: lx0, y: ly0, w: pw, cx: mx, ty: ly + 0.5, text, color: c.color, alpha: 0.92 * dim })
        }
      }
    }
  }

  /* ---- 点与标签 ---- */
  const CELL = 90
  const grid = new Set<string>()
  const canPlace = (lx: number, ly: number, lw: number, lh: number) => {
    for (let c = Math.floor(lx / CELL); c <= Math.floor((lx + lw) / CELL); c++)
      for (let r = Math.floor(ly / CELL); r <= Math.floor((ly + lh) / CELL); r++) if (grid.has(`${c},${r}`)) return false
    return true
  }
  const claim = (lx: number, ly: number, lw: number, lh: number) => {
    for (let c = Math.floor(lx / CELL); c <= Math.floor((lx + lw) / CELL); c++)
      for (let r = Math.floor(ly / CELL); r <= Math.floor((ly + lh) / CELL); r++) grid.add(`${c},${r}`)
  }

  /** 暗的、不重要的点先画，亮的后画。否则一个被压暗的点落在高亮点上会把它啃掉一块。 */
  const dimmed = new Uint8Array(n)
  for (let i = 0; i < n; i++) {
    if (!visible[i]) continue
    if (hoverIndex >= 0) dimmed[i] = neighbors![i] || i === hoverIndex ? 0 : 1
    else if (searchOn) dimmed[i] = isMatch(i) ? 0 : 1
    else if (focusOn) dimmed[i] = inFocus(i) ? 0 : 1
    // 选中的点永远最后画：它可能不在搜索/聚焦范围内，但抽屉正开着，
    // 被一圈亮着的点盖住会让人以为选错了。
    if (selectedId != null && L.nodes[i].id === selectedId) dimmed[i] = 0
  }

  const order: number[] = []
  const bright: number[] = []
  for (let i = 0; i < n; i++) {
    if (!visible[i] || i === hoverIndex) continue
    if (dimmed[i]) order.push(i)
    else bright.push(i)
  }
  order.push(...bright)
  // 悬停点最后画（压在最上层）
  if (hoverIndex >= 0 && visible[hoverIndex]) order.push(hoverIndex)

  let labels = 0
  let shown = 0
  ctx.font = '11px ui-sans-serif, system-ui, sans-serif'
  for (const i of order) {
    const nd = L.nodes[i]
    const px = sx[i]
    const py = sy[i]
    shown++
    const isHover = i === hoverIndex
    const isNeighbor = hoverIndex >= 0 && neighbors![i] === 1
    const isSel = selectedId != null && nd.id === selectedId
    const match = isMatch(i)
    const isActive = overlay != null && overlay.active === i
    const isRoot = focusOn && overlay!.root === i

    // 大小随关系数微增（星等），再乘一个错相的呼吸
    const pulse = 1 + 0.13 * Math.sin(time * 1.05 + nd.phase)
    // 低缩放下也要看得见：`min(zoom,2)` 单独用会让自适应缩放的初始视角里
    // 每个点只有 1.5px，整张图看着像噪点。给一个下限 + 抬高的基准。
    const r = Math.max(1.7, nd.r * pulse * (0.75 + Math.min(zoom, 2) * 0.6))
    const twinkle = 0.82 + 0.18 * Math.sin(time * 1.4 + nd.phase * 2.1)
    // ⚠️ 亮色底上「越亮越重要」这个表达**不成立**（白底已经是最亮），
    // 所以基准不透明度整体抬高、把层次让给尺寸：暗底那版是 0.62 起步，
    // 白底上会淡成一片灰。改亮后实测点是靠饱和度被辨认出来的。
    const baseAlpha = (0.74 + Math.min(nd.linkCount * 0.03, 0.26)) * twinkle

    // 关系多的点先铺一层外辉光，hub 才有「亮星」的感觉
    if (nd.linkCount > 3 && !isHover && hoverIndex < 0) {
      const gl = 1 + 0.25 * Math.sin(time * 0.9 + nd.phase * 1.7)
      ctx.beginPath()
      ctx.arc(px, py, r * 2.2, 0, Math.PI * 2)
      ctx.fillStyle = nd.color
      ctx.globalAlpha = (0.035 + Math.min(nd.linkCount * 0.003, 0.045)) * gl
      ctx.fill()
    }

    ctx.beginPath()
    ctx.arc(px, py, r, 0, Math.PI * 2)
    ctx.fillStyle = nd.color
    // 悬停时其余点压到 0.16 而不是原版的 0.08：我们一屏有 1000+ 个点，
    // 压到 0.08 之后整张图看着像渲染失败，而不是「聚光灯」。
    // 搜索时未命中的点压到 0.2，聚焦范围外的压到 0.08——三者的优先级：
    // 悬停 > 搜索 > 聚焦。搜索是用户刚输入的动作，应该能立刻看到结果在哪。
    let alpha: number
    if (isHover || isSel) alpha = 1
    else if (isNeighbor) alpha = 0.95
    else if (hoverIndex >= 0) alpha = 0.16
    else if (searchOn) alpha = match ? Math.max(baseAlpha, 0.95) : 0.2
    else if (focusOn) alpha = inFocus(i) ? Math.max(baseAlpha, 0.9) : 0.08
    else alpha = baseAlpha
    ctx.globalAlpha = alpha
    if (isHover || isNeighbor || isSel || isActive) {
      ctx.shadowColor = nd.color
      ctx.shadowBlur = isHover ? 20 : isActive ? 14 : 10
    }
    ctx.fill()
    ctx.shadowBlur = 0
    ctx.globalAlpha = 1

    // 选中点画一圈描边，跟抽屉里的那条对上
    if (isSel) {
      ctx.strokeStyle = C.ring
      ctx.lineWidth = 1.5
      ctx.beginPath()
      ctx.arc(px, py, r + 4, 0, Math.PI * 2)
      ctx.stroke()
    }
    // 搜索命中：主色细环；当前激活的那一条再粗一圈。颜色不跟记忆类型混，
    // 因为「命中」和「类型」是两个正交的维度。悬停把其它点压暗时，环也跟着隐去，
    // 否则「点全暗了、几十个圈还亮着」会很割裂。
    const showRing = match && (hoverIndex < 0 || isHover || isNeighbor || isSel)
    if (showRing) {
      ctx.strokeStyle = C.matchRing
      ctx.lineWidth = isActive ? 2 : 1
      ctx.beginPath()
      ctx.arc(px, py, r + (isActive ? 6 : 3.5), 0, Math.PI * 2)
      ctx.stroke()
    }
    // 聚焦起点：即使它没被选中，也画一圈虚线，告诉用户「这次聚焦是从哪儿出发的」
    if (isRoot && !isSel) {
      ctx.strokeStyle = C.matchRing
      ctx.lineWidth = 1
      ctx.setLineDash([3, 3])
      ctx.beginPath()
      ctx.arc(px, py, r + 4, 0, Math.PI * 2)
      ctx.stroke()
      ctx.setLineDash([])
    }

    /* 标签。悬停点无条件显示（跳过去重网格）；其余在放大到自适应缩放 1.8 倍
     * 以上、或它是悬停点的邻居时才尝试放，且要过网格去重。 */
    let wantLabel = isHover || isNeighbor || isSel || zoom > s.fit * 1.8
    // 搜索态只给命中的点放标签（外加悬停/选中）——否则结果会被一堆无关标签淹没。
    if (searchOn) wantLabel = isHover || isSel || match
    // 聚焦态同理：范围外的点不占标签位置。
    else if (focusOn) wantLabel = isHover || isSel || inFocus(i)
    if (wantLabel && labels < MAX_LABELS) {
      const lw = 168
      const lh = 16
      const lx = px + r + 5
      const ly = py - 8
      if (isHover || canPlace(lx, ly, lw, lh)) {
        if (!isHover) claim(lx, ly, lw, lh)
        ctx.font = '11px ui-sans-serif, system-ui, sans-serif'
        ctx.fillStyle = isHover ? C.labelHover : C.label
        ctx.globalAlpha = isHover ? 1 : isNeighbor ? 0.9 : isActive || match ? 0.95 : Math.min(1, (zoom / s.fit - 0.9) * 1.6)
        const text = nd.label || nd.id.slice(0, 12)
        ctx.fillText(text.length > 30 ? `${text.slice(0, 30)}…` : text, lx, py + 4)
        ctx.globalAlpha = 1
        labels++
      }
    }
  }

  /* ---- 区域标签（最后画：压在点和区域之上，否则会被后画的点盖掉）---- */
  if (pendingLabels.length) {
    ctx.font = '600 11px ui-sans-serif, system-ui, sans-serif'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    for (const lb of pendingLabels) {
      // ⚠️ 必须跟着 `hullDim` 一起暗。胶囊是全画布**最亮**的一层（实色 92% 不透明），
      // 漏掉它的话悬停时会出现「点全暗了、几十个彩色胶囊还满亮」的割裂感——
      // 实测这一层有 1.1 万+ 纯饱和像素，比所有点加起来还抢眼。
      ctx.fillStyle = hexToRgba(lb.color, lb.alpha)
      ctx.beginPath()
      if (typeof ctx.roundRect === 'function') ctx.roundRect(lb.x, lb.y, lb.w, 19, 9.5)
      else ctx.rect(lb.x, lb.y, lb.w, 19)
      ctx.fill()
      ctx.fillStyle = C.pillText
      ctx.fillText(lb.text, lb.cx, lb.ty)
    }
    ctx.textAlign = 'left'
    ctx.textBaseline = 'alphabetic'
  }

  /* ---- HUD（画在画布上，不走 React：每帧都在变，走 state 就是每秒 60 次重渲染）---- */
  ctx.font = '11px "SF Mono", ui-monospace, Consolas, monospace'
  ctx.fillStyle = C.hud
  ctx.textAlign = 'left'
  // ⚠️ `zoom` 在这里就格式化好：词条只管排版，千分位/小数位属于调用侧的事
  ctx.fillText(t('graph.hud')({ total: L.nodes.length, shown, labels, links: L.links.length, zoom: zoom.toFixed(2) }), 12, H - 12)
}

/** 右下缩略图：星云整体轮廓 + 当前视口框。 */
export function drawMinimap(mini: HTMLCanvasElement, L: Layout, s: Camera, theme: CanvasTheme, overlay?: Overlay): void {
  const C = palette(theme)
  const mctx = mini.getContext('2d')
  if (!mctx) return
  mctx.setTransform(s.dpr, 0, 0, s.dpr, 0, 0)
  mctx.clearRect(0, 0, MINIMAP, MINIMAP)
  const R = Math.max(L.maxR * 1.08, 1)
  const k = MINIMAP / (2 * R)
  const toMini = (x: number, y: number): [number, number] => [MINIMAP / 2 + x * k, MINIMAP / 2 + y * k]
  mctx.fillStyle = C.miniDot
  for (let i = 0; i < L.nodes.length; i++) {
    const nd = L.nodes[i]
    // 聚焦时范围外的点在缩略图里也压淡：缩略图是「整张图 + 我在哪」，
    // 它得能替代把镜头拉远这一步，所以过滤结果也要反映在它上面。
    const dim = overlay && overlay.depths != null && !(overlay.depths[i] >= 0 && overlay.depths[i] <= overlay.depth)
    mctx.globalAlpha = dim ? 0.25 : 1
    mctx.fillStyle = overlay?.searching && overlay.match[i] ? C.matchRing : C.miniDot
    const [mx, my] = toMini(nd.x, nd.y)
    mctx.fillRect(mx, my, 1.4, 1.4)
  }
  mctx.globalAlpha = 1
  // 视口框：屏幕中心对应的世界点是 (-panX/zoom, -panY/zoom)
  const vw = (s.W / s.zoom) * k
  const vh = (s.H / s.zoom) * k
  const [vcx, vcy] = toMini(-s.panX / s.zoom, -s.panY / s.zoom)
  mctx.strokeStyle = C.miniView
  mctx.lineWidth = 1
  mctx.strokeRect(vcx - vw / 2, vcy - vh / 2, vw, vh)
}

/** 供 `Constellation` 判断「这一帧要不要重画」：相机是否还在向目标缓动。 */
export function cameraSettled(s: Camera, epsilon = 0.05): boolean {
  return Math.abs(s.panX - s.tPanX) <= epsilon && Math.abs(s.panY - s.tPanY) <= epsilon && Math.abs(s.zoom - s.tZoom) <= epsilon * 0.01
}
