import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/** 后端代理：默认本地 mock（pnpm mock），也可以直接指线上后端。
 *
 *  看真实数据只改这一处，两条路都行：
 *    .env.local:  MOCK_ORIGIN=http://<线上后端地址>
 *    命令行:      MOCK_ORIGIN=http://<线上后端地址> pnpm dev
 *
 *  ⚠️ 线上后端是登录页鉴权，所以 `/__login`、`/__logout`
 *  也必须走代理。只代理 /api 的话，浏览器打不开登录页、拿不到会话 cookie，
 *  所有接口都只会回 401；退出登录也会落到本地路由而不是后端。 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = process.env.MOCK_ORIGIN || env.MOCK_ORIGIN || 'http://localhost:5280'
  const proxy = {
    '/api': { target, changeOrigin: true },
    '/admin': { target, changeOrigin: true },
    '/__login': { target, changeOrigin: true },
    '/__logout': { target, changeOrigin: true },
  }

  return {
    plugins: [react(), tailwindcss()],
    server: { port: 5274, proxy },
    preview: { port: 5275, proxy },
  }
})
