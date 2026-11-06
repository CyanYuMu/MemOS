export interface PublicConfig {
  serviceBase: string
  consoleBase: string
}
function cleanURL(value: string) {
  if (!value) return ''
  try {
    const url = new URL(value)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) return ''
    return value.replace(/\/+$/, '')
  } catch {
    return ''
  }
}
/** 运行时配置优先；显式空值关闭接入，不能落回另一环境的构建默认值。 */
export function publicServiceBase(
  runtime: PublicConfig | undefined,
  buildURL: string | undefined,
  host: { hostname: string; protocol: string },
) {
  if (runtime !== undefined) return cleanURL(runtime.serviceBase)
  if (buildURL) return cleanURL(buildURL)
  if (['localhost', '127.0.0.1', '[::1]'].includes(host.hostname)) return `${host.protocol}//${host.hostname}:8002`
  return ''
}
