import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

/** 主题偏好。`system` 跟随操作系统的深浅色，切换时实时响应。 */
export type ThemePref = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

const THEME_KEY = 'console.theme'

/** 首帧的类名由 index.html 里的内联脚本先打上（避免白闪），
 *  这里只负责之后的切换与持久化。两处的键名与判定必须一致。 */
function readPref(): ThemePref {
  const saved = localStorage.getItem(THEME_KEY)
  return saved === 'light' || saved === 'dark' ? saved : 'system'
}

function systemPrefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function resolve(pref: ThemePref): ResolvedTheme {
  return pref === 'system' ? (systemPrefersDark() ? 'dark' : 'light') : pref
}

interface ThemeCtx {
  pref: ThemePref
  /** 实际生效的主题（`pref === 'system'` 时由系统决定） */
  resolved: ResolvedTheme
  setPref: (p: ThemePref) => void
}

const ThemeContext = createContext<ThemeCtx | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [pref, setPrefState] = useState<ThemePref>(readPref)
  const [resolved, setResolved] = useState<ResolvedTheme>(() => resolve(pref))

  useEffect(() => {
    const apply = () => {
      const next = resolve(pref)
      setResolved(next)
      document.documentElement.classList.toggle('dark', next === 'dark')
    }
    apply()
    localStorage.setItem(THEME_KEY, pref)

    // 只有跟随系统时才需要监听；手动选定的主题不受系统变化影响。
    if (pref !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [pref])

  const setPref = useCallback((p: ThemePref) => setPrefState(p), [])
  const value = useMemo<ThemeCtx>(() => ({ pref, resolved, setPref }), [pref, resolved, setPref])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const c = useContext(ThemeContext)
  if (!c) throw new Error('useTheme 必须在 ThemeProvider 内使用')
  return c
}
