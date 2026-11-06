import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { Driver } from 'driver.js'
import { stageShape } from '../../lib/stageShape'
import { useT } from '../../lib/store'
import 'driver.js/dist/driver.css'

/** 高亮由driver管理，动态状态和操作由React渲染；详情打开时让出遮罩与焦点。 */
export function GuideStepOverlay({
  stage,
  title,
  onSkip,
  children,
}: {
  stage: string
  title: string
  onSkip: () => void
  children: ReactNode
}) {
  const t = useT()
  const [target, setTarget] = useState<HTMLElement | null>(null)
  const [decoration, setDecoration] = useState<{ layer: HTMLElement; rect: DOMRect; path: string; width: number; height: number } | null>(
    null,
  )
  const tourRef = useRef<Driver | null>(null)
  useLayoutEffect(() => {
    if (target) tourRef.current?.refresh()
  }, [target, children])
  const skip = useRef(onSkip)
  useEffect(() => {
    skip.current = onSkip
  }, [onSkip])
  useEffect(() => {
    let active = true,
      tour: Driver | undefined,
      detailOpen = false
    const decorate = () => {
      const layer = tour?.getState().popover?.wrapper.parentElement as HTMLElement | undefined
      const from = document.getElementById('chat-current-conversation')?.getBoundingClientRect()
      const to = document.getElementById('chat-thread')?.getBoundingClientRect()
      if (!layer || !from || !to || stage === 'logs' || window.innerWidth < 1100) {
        setDecoration(null)
        return
      }
      const x = from.left + from.width / 2,
        y = from.bottom + 6,
        endX = to.left + 30,
        endY = to.top + 72
      setDecoration({
        layer,
        rect: from,
        path: `M ${x} ${y} C ${x} ${y + 48}, ${endX - 30} ${endY - 24}, ${endX} ${endY}`,
        width: window.innerWidth,
        height: window.innerHeight,
      })
    }
    const highlight = () => {
      detailOpen = !!document.querySelector('.memos-detail-dialog[data-state="open"]')
      if (!tour || !active || detailOpen) return
      const wide = window.matchMedia('(min-width:1100px)').matches
      const element = document.querySelector(stage === 'logs' ? '#chat-log-panel' : '#chat-thread')
      if (!element) return
      // 高亮框贴着面板，圆角取页面上真实的圆角；留白会让它盖住侧栏和相邻面板的圆角。
      // 圆角要等页面结构落定后才量得准，所以每次高亮前重新设置（setConfig 会整体替换，先带上原配置）。
      if (typeof tour.setConfig === 'function') tour.setConfig({ ...tour.getConfig(), ...stageShape(element, 0) })
      tour.highlight({
        element,
        popover: { title, description: ' ', side: wide ? (stage === 'logs' ? 'left' : 'right') : 'bottom', align: 'start' },
      })
    }
    const observer = new MutationObserver(() => {
      const next = !!document.querySelector('.memos-detail-dialog[data-state="open"]')
      if (next === detailOpen) return
      detailOpen = next
      if (next) {
        tour?.destroy()
        setTarget(null)
        setDecoration(null)
      } else highlight()
    })
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented && !document.querySelector('.memos-detail-dialog[data-state="open"]')) {
        event.preventDefault()
        event.stopPropagation()
        skip.current()
      }
    }
    document.addEventListener('keydown', onKey, true)
    window.addEventListener('resize', decorate)
    window.addEventListener('scroll', decorate, true)
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-state'] })
    void import('driver.js').then(({ driver }) => {
      if (!active) return
      tour = driver({
        animate: document.visibilityState === 'visible' && !window.matchMedia('(prefers-reduced-motion:reduce)').matches,
        overlayColor: '#151633',
        overlayOpacity: 0.5,
        stagePadding: 0,
        popoverClass: 'memos-tour memos-guide-step',
        showButtons: ['close'],
        allowClose: true,
        allowKeyboardControl: false,
        overlayClickBehavior: 'none',
        closeBtnLabel: t('common.close'),
        onCloseClick: () => skip.current(),
        onPopoverRender: (popover) => {
          if (active) {
            setTarget(popover.description)
            decorate()
          }
        },
      })
      tourRef.current = tour
      highlight()
    })
    return () => {
      active = false
      observer.disconnect()
      document.removeEventListener('keydown', onKey, true)
      window.removeEventListener('resize', decorate)
      window.removeEventListener('scroll', decorate, true)
      tour?.destroy()
      tourRef.current = null
    }
  }, [stage, title, t])
  return (
    <>
      {target &&
        createPortal(
          <div
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault()
                event.stopPropagation()
                onSkip()
              }
            }}
          >
            {children}
          </div>,
          target,
        )}
      {decoration &&
        createPortal(
          <svg
            className="memos-guide-arrow"
            width={decoration.width}
            height={decoration.height}
            viewBox={`0 0 ${decoration.width} ${decoration.height}`}
            aria-hidden="true"
          >
            <defs>
              <marker id={`memos-guide-arrow-${stage}`} markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
                <path d="M 1 1 L 6 4 L 1 7" fill="none" stroke="white" strokeWidth="1.5" />
              </marker>
            </defs>
            <rect
              x={decoration.rect.x - 5}
              y={decoration.rect.y - 5}
              width={decoration.rect.width + 10}
              height={decoration.rect.height + 10}
              rx="10"
              stroke="white"
              strokeWidth="1.5"
              strokeDasharray="4 4"
              fill="none"
            />
            <path d={decoration.path} stroke="white" strokeWidth="2" fill="none" markerEnd={`url(#memos-guide-arrow-${stage})`} />
          </svg>,
          decoration.layer,
        )}
    </>
  )
}
