/** 对话页共用的出入场与转圈。
 *
 *  曲线和时长只从 `src/lib/motion.ts` 取，和页面切换是同一套手感。
 *  `prefers-reduced-motion` 下只保留透明度，不做位移、缩放或持续旋转。
 */

import { motion, useReducedMotion } from 'motion/react'
import type { ReactNode } from 'react'
import { DUR, EASE_OUT_EXPO } from '../../lib/motion'

/** 淡入 + 轻微位移。用于空态、表单、状态条、错误条这类整块出现的元素。 */
export function ChatFade({ children, className, y = 6 }: { children: ReactNode; className?: string; y?: number }) {
  const reduce = useReducedMotion()
  return (
    <motion.div
      className={className}
      initial={reduce ? { opacity: 0 } : { opacity: 0, y }}
      animate={{ opacity: 1, y: 0 }}
      transition={reduce ? { duration: DUR.fast } : { duration: DUR.base, ease: EASE_OUT_EXPO }}
    >
      {children}
    </motion.div>
  )
}

/** 转圈。减少动效时退化成静态圆点，避免持续旋转。 */
export function ChatSpinner() {
  const reduce = useReducedMotion()
  return reduce ? (
    <span className="size-2 rounded-full bg-ink-faint" aria-hidden="true" />
  ) : (
    <span className="btn-spinner" aria-hidden="true" />
  )
}
