import { expect, it } from 'vitest'
import { skillLifecycle, skillLifecyclePackage } from './skill-lifecycle.contract'

import { lifecycleFixture, lifecyclePackageFixture } from './skill-lifecycle.test-fixture'

it('显式解析来源观察、未解除根因和固定生命周期包', () => {
  expect(skillLifecycle.parse(lifecycleFixture())).toMatchObject({ enabled: true, gate: { status: 'BLOCK' } })
  expect(skillLifecyclePackage.parse(lifecyclePackageFixture())).toMatchObject({ kind: 'lifecycle', sequence: 7 })
})
it.each(['watermark', 'subjects', 'roots', 'observations', 'decisions', 'dependencies', 'gate'])(
  '缺少生命周期字段 %s 不能降级为旧详情',
  (key) => {
    const value: Record<string, unknown> = lifecycleFixture()
    delete value[key]
    expect(skillLifecycle.safeParse(value).success).toBe(false)
  },
)
it('未知状态、私有额外字段和重复根因摘要不被静默接受', () => {
  const value = lifecycleFixture()
  value.subjects[0].status = 'passed'
  expect(skillLifecycle.safeParse(value).success).toBe(false)
  expect(skillLifecycle.safeParse({ ...lifecycleFixture(), privatePath: '/tmp/private' }).success).toBe(false)
  const packet = lifecyclePackageFixture()
  packet.activeRootIds.push(packet.activeRootIds[0])
  expect(skillLifecyclePackage.safeParse(packet).success).toBe(false)
})
it('固定包摘要数组允许 200 项，拒绝 201 项', () => {
  const value = lifecyclePackageFixture()
  value.evidenceIds = Array.from({ length: 200 }, (_, index) => index.toString(16).padStart(64, '0'))
  expect(skillLifecyclePackage.safeParse(value).success).toBe(true)
  value.evidenceIds.push('f'.repeat(64))
  expect(skillLifecyclePackage.safeParse(value).success).toBe(false)
})
