import { useT } from '../lib/store'

/** 品牌图形，字标为中性名称 */
export function Logo({ compact = false }: { compact?: boolean }) {
  const t = useT()
  if (compact) return <img src="/logo.svg?v=hugegraph" width={40} height={40} alt="" className="block shrink-0" />
  return (
    <span className="flex items-center gap-[6px]">
      <img src="/logo.svg?v=hugegraph" width={40} height={40} alt="" className="block shrink-0" />
      <span className="text-[19px] font-bold leading-none tracking-[-0.2px] text-ink">{t('brand.name')}</span>
    </span>
  )
}
