import { expect, it, vi } from 'vitest'
vi.mock('./config', () => ({
  SERVICE_BASE: 'https://dev-gateway.example',
  LINKS: { mcp: 'https://dev-gateway.example/mcp', docs: '/cn/docs' },
}))
import { buildTabs } from './quickstart-data'
it('MCP 接入使用当前环境地址', () => {
  const step = buildTabs({ tools: [] })
    .find((tab) => tab.id === 'hermes')
    ?.steps.find((item) => item.n === '2')
  expect(step?.code).toContain('https://dev-gateway.example/mcp')
})
