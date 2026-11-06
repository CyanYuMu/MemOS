import { useEffect, useState } from 'react'

const QUERY = '(prefers-reduced-motion: reduce)'

/** 当前系统是否要求减少动效。SSR/测试环境没有 matchMedia 时按“不减少”处理。 */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(QUERY).matches
}

/**
 * 监听系统「减少动效」偏好。画布这类长驻动画用它决定初始状态：
 * 用户没有主动开启动效时，尊重系统设置，避免眩晕与无谓的 CPU 占用。
 */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(prefersReducedMotion)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(QUERY)
    const onChange = () => setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return reduced
}
