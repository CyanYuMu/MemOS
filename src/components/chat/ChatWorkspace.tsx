import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { BookOpen, CircleHelp, Languages, MessageSquare, Moon, Sun } from 'lucide-react'
import { useLang, useProject, useT } from '../../lib/store'
import { useTheme } from '../../lib/theme'

/** 聊天只提供局部工具栏，项目、账号和导航统一由控制台壳层管理。 */
export function ChatWorkspace({ children, onGuide, guideEnabled }: { children: ReactNode; onGuide: () => void; guideEnabled: boolean }) {
  const t = useT()
  const { lang, setLang } = useLang()
  const { resolved, setPref } = useTheme()
  const { currentProjectName } = useProject()

  return (
    <div className="memos-playground memos-integrated">
      <section className="memos-main">
        <header className="memos-topbar">
          <div className="memos-page-title">
            <MessageSquare size={20} />
            <span>{t('chat.title')}</span>
            <span className="memos-topbar-divider" />
            <span id="chat-project-name" className="memos-project-name">
              {currentProjectName || t('nav.selectProject')}
            </span>
          </div>
          <div className="memos-topbar-actions">
            <button className="memos-guide-button" onClick={onGuide} disabled={!guideEnabled} title={t('playground.guide')}>
              <CircleHelp size={18} />
              <span>{t('playground.guide')}</span>
            </button>
            <Link
              className="memos-icon-button memos-docs-link"
              to="/cn/docs"
              title={t('playground.docs')}
              aria-label={t('playground.docs')}
            >
              <BookOpen size={18} />
            </Link>
            <span className="memos-topbar-divider" />
            <button
              className="memos-icon-button"
              onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}
              aria-label={t('playground.language')}
              title={t('playground.language')}
            >
              <Languages size={19} />
            </button>
            <button
              className="memos-icon-button"
              onClick={() => setPref(resolved === 'dark' ? 'light' : 'dark')}
              aria-label={t('playground.theme')}
              title={t('playground.theme')}
            >
              {resolved === 'dark' ? <Sun size={19} /> : <Moon size={19} />}
            </button>
          </div>
        </header>
        <div className="memos-workspace-body">{children}</div>
      </section>
    </div>
  )
}
