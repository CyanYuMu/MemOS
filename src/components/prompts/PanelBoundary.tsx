/** 试跑面板的错误边界：任何输入形状或渲染异常都停在面板里，不让整页白屏。
 *
 *  面板拿到的数据来自上游注册表，形状可能随版本变化；这里兜住渲染期异常，
 *  事件里的异常由 `TestPanel` 自己归一+捕获。重开面板（`key` 变化）或点「重开」即可恢复。
 */

import { Component, type ReactNode } from 'react'
import { Icon } from '../Icon'

interface Props {
  children: ReactNode
  title: string
  desc: string
  action: string
  onReset: () => void
}

interface State {
  failed: boolean
}

export class PanelBoundary extends Component<Props, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <section className="studio-test-panel studio-test-panel--expanded" role="alert">
        <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
          <Icon name="i-lucide:alert-circle" className="size-6 text-warning" />
          <p className="text-sm font-medium text-ink">{this.props.title}</p>
          <p className="max-w-md text-xs leading-5 text-ink-muted">{this.props.desc}</p>
          <button type="button" className="studio-link-btn" onClick={this.props.onReset}>
            {this.props.action}
          </button>
        </div>
      </section>
    )
  }
}
