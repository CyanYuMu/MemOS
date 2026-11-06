import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { DUR, EASE_IN_FAST, EASE_OUT_SOFT } from './lib/motion'
import { PlatformGuideProvider } from './lib/onboarding'
import { AppShell } from './components/AppShell'
import { LangProvider, MetaProvider, ToastProvider, ProjectProvider } from './lib/store'
import { ThemeProvider } from './lib/theme'
import { TooltipProvider } from './components/ui/tooltip'
import { ApiKeys } from './pages/ApiKeys'
import { Chat } from './pages/Chat'
import { QuickStart } from './pages/QuickStart'
import { Jobs } from './pages/Jobs'
import { Requests } from './pages/Requests'
import { Memories } from './pages/Memories'
import { LocalMemories } from './pages/LocalMemories'
import { MemoryGraph } from './pages/MemoryGraph'
import { Models } from './pages/Models'
import { KnowledgeBase } from './pages/KnowledgeBase'
import { ProfileTemplates } from './pages/ProfileTemplates'
import { Extractions } from './pages/Extractions'
import { Usage } from './pages/Usage'
import { Docs } from './pages/Docs'
import { About } from './pages/About'
import { Projects } from './pages/Projects'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 1000 * 30, // 30 seconds
    },
  },
})

function AnimatedRoutes() {
  const location = useLocation()
  const reduce = useReducedMotion()

  /* 页面切换：新页从下方 8px 淡入，旧页向反方向 6px 淡出。
   *
   *  两段用不同曲线——进场 `EASE_OUT_SOFT` 让内容「落定」，出场 `EASE_IN_FAST`
   *  让旧页迅速让位；反过来会变成「慢慢消失 + 突然出现」，切换显得拖沓。
   *
   *  `mode="wait"` 下两段是串行的，所以出场必须比进场短，否则每次导航都要等
   *  两个完整的时长。减少动态效果及后台页面直接切换，不等待动画。
   */
  const enter = { opacity: 0, y: 8 }
  const exit = { opacity: 0, y: -6, transition: { duration: DUR.fast, ease: EASE_IN_FAST } }

  const routes = (
    <Routes location={location}>
      <Route path="/" element={<Navigate to="/cn/quickstart" replace />} />
      <Route path="/cn/quickstart" element={<QuickStart />} />
      <Route path="/cn/apikeys" element={<ApiKeys />} />
      <Route path="/cn/chat" element={<Chat />} />
      <Route path="/cn/requests" element={<Requests />} />
      <Route path="/cn/knowledgeBase" element={<KnowledgeBase />} />
      <Route path="/cn/memoryList" element={<Memories />} />
      <Route path="/cn/localMemories" element={<LocalMemories />} />
      <Route path="/cn/memoryGraph" element={<MemoryGraph />} />
      <Route path="/cn/models" element={<Models />} />
      <Route path="/cn/profileTemplate" element={<ProfileTemplates />} />
      <Route path="/cn/prompts" element={<Extractions />} />
      <Route path="/cn/customExtraction" element={<Navigate to="/cn/prompts" replace />} />
      <Route path="/cn/projects" element={<Projects />} />
      <Route path="/cn/usage" element={<Usage />} />
      <Route path="/cn/jobs" element={<Jobs />} />
      <Route path="/cn/docs" element={<Docs />} />
      <Route path="/cn/about" element={<About />} />
      <Route path="*" element={<Navigate to="/cn/quickstart" replace />} />
    </Routes>
  )

  // 后台页面的动画帧可能暂停，路由切换不能依赖退场动画完成。
  if (reduce || document.visibilityState !== 'visible') return <div className="flex-1 flex flex-col min-h-0">{routes}</div>

  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={location.pathname}
        initial={enter}
        animate={{ opacity: 1, y: 0 }}
        exit={exit}
        transition={{ duration: DUR.page, ease: EASE_OUT_SOFT }}
        className="flex-1 flex flex-col min-h-0"
      >
        {routes}
      </motion.div>
    </AnimatePresence>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <TooltipProvider delayDuration={320}>
          <LangProvider>
            <MetaProvider>
              <ProjectProvider>
                <ToastProvider>
                  <PlatformGuideProvider>
                    <AppShell>
                      <AnimatedRoutes />
                    </AppShell>
                  </PlatformGuideProvider>
                </ToastProvider>
              </ProjectProvider>
            </MetaProvider>
          </LangProvider>
        </TooltipProvider>
      </ThemeProvider>
    </QueryClientProvider>
  )
}
