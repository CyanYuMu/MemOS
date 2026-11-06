import { Icon } from './Icon'

/** 彩色语言标识不能走单色 mask，否则形状和配色会丢失。 */
export function AccessIcon({ name, className = '' }: { name: string; className?: string }) {
  return name.startsWith('/') ? (
    <img src={name} alt="" aria-hidden="true" className={`access-logo ${className}`} />
  ) : (
    <Icon name={name} className={className} />
  )
}
