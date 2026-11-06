import { expect, it } from 'vitest'
import { publicServiceBase } from './publicConfig'
it('同一产物读取不同部署地址，未配置时不回退生产', () => {
  const host = { hostname: 'console.example', protocol: 'https:' }
  expect(
    publicServiceBase({ serviceBase: 'https://dev-gateway.example/', consoleBase: 'https://dev.example' }, 'https://prod.example', host),
  ).toBe('https://dev-gateway.example')
  expect(publicServiceBase({ serviceBase: 'https://prod-gateway.example', consoleBase: 'https://prod.example' }, '', host)).toBe(
    'https://prod-gateway.example',
  )
  expect(publicServiceBase({ serviceBase: '', consoleBase: '' }, 'https://prod.example', host)).toBe('')
  expect(publicServiceBase(undefined, '', host)).toBe('')
  expect(publicServiceBase({ serviceBase: 'https://user:pass@example.com', consoleBase: '' }, '', host)).toBe('')
  expect(publicServiceBase(undefined, '', { hostname: 'localhost', protocol: 'http:' })).toBe('http://localhost:8002')
})
