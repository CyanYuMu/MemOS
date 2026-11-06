/** Iconify CSS 模式：类名定义在 styles/icons.css，尺寸由外部 class 决定 */
export function Icon({
  name,
  className = '',
  style,
  ...rest
}: {
  name: string
  className?: string
  style?: React.CSSProperties
} & React.HTMLAttributes<HTMLSpanElement>) {
  return <span className={`iconify ${name} ${className}`.trim()} style={style} aria-hidden="true" {...rest} />
}
