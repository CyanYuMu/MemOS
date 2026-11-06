import { useEffect, useState } from 'react'

const QUERY = '(min-width: 1024px)'

function matches(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia(QUERY).matches
}

/** 宽屏布局：记忆汇总放右侧窄栏，窄屏收成顶部可展开区域 */
export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(matches)

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia(QUERY)
    const onChange = () => setIsDesktop(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return isDesktop
}
