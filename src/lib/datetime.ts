/** 时间显示：接口给的是 UTC ISO，界面一律按北京时间显示到分钟。
 *
 *  `2026-10-28T19:53:35.075376+00:00` → `2026-10-29 03:53`。
 *  无时区的字符串（mock 的 `2026-10-22 15:40:00`）按原样截断到分钟，避免被当成 UTC 再平移一次。
 */

const HAS_ZONE = /(?:[zZ]|[+-]\d{2}:?\d{2})$/

export function formatBeijingTime(iso: string | null | undefined): string {
  const s = (iso ?? '').trim()
  if (!s) return ''

  if (!HAS_ZONE.test(s)) {
    const naive = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
    return naive ? `${naive[1]} ${naive[2]}` : s
  }

  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s

  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(d)
  const pick = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  return `${pick('year')}-${pick('month')}-${pick('day')} ${pick('hour')}:${pick('minute')}`
}
