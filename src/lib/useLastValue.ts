import { useEffect, useState } from 'react'

/**
 * 保留最后一个非空值。
 *
 * 弹窗关闭时，调用方通常直接把待确认对象置空（`setPending(null)`），而弹窗还有
 * 一段退场动画（~150ms）。动画期间 children 仍会渲染，于是带插值的文案会变成
 * 「确定删除“”？」——看起来像文案错乱或信息丢了。
 *
 * 打开时返回当前值，关闭时返回上一次的值，动画里内容保持完整。
 */
export function useLastValue<T>(value: T | null | undefined): T | null {
  const [last, setLast] = useState<T | null>(null)
  useEffect(() => {
    // 值变了要记下来，供关闭后那一帧继续显示（见上面的说明）。
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (value != null) setLast(value)
  }, [value])
  return value ?? last
}
