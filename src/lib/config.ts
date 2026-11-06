/** Go部署通过同源runtime-config.js提供对外地址；Vite/独立静态部署可显式配置构建值。
 * VITE_API_BASE只控制同源后台请求，不改变记忆网关环境。
 */

import { publicServiceBase, type PublicConfig } from './publicConfig'
declare global {
  interface Window {
    __GRAPH_MEMORY_CONFIG__?: PublicConfig
  }
}
const env = import.meta.env
const runtime = typeof window === 'undefined' ? undefined : window.__GRAPH_MEMORY_CONFIG__
const host = typeof location === 'undefined' ? { hostname: 'localhost', protocol: 'http:' } : location
export const SERVICE_BASE = publicServiceBase(runtime, env.VITE_SERVICE_BASE, host)

export const API_BASE: string = (env.VITE_API_BASE || '').replace(/\/+$/, '')

export const LINKS = {
  /** MCP 接入地址 */
  mcp: `${SERVICE_BASE}/mcp`,
  /** 控制台内置文档页 */
  docs: '/cn/docs',
  about: '/cn/about',
} as const
