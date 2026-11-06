/** 页面共用的展示格式化。只做排版，不参与业务判断。 */

/** 字节数 → 人类可读。保留一位小数（≥10 时取整），单位逐级进位。 */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '-'
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = n / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`
}

/** 服务端时间戳 → 本地时间。解析不了就原样显示，不假装补一个日期。 */
export function formatDateTime(value: string | undefined, lang: 'zh' | 'en', fallback = '-'): string {
  if (!value) return fallback
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US')
}

/** `<input type="datetime-local">` 给的是**本地时间且不带时区**（`2026-10-24T10:00`）。
 *
 *  后端只接受 RFC3339 或 `YYYY-MM-DD`，直接把前者发过去会被判 400。这里按浏览器时区
 *  解析成真实时刻，再转成 UTC 的 RFC3339；解析不了就原样返回，让服务端报真实错误。 */
export function toRFC3339(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toISOString()
}
