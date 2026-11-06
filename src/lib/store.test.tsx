// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api, type Account, type Meta, type Project } from './api'
import { LangProvider, MetaProvider, ProjectProvider, useMeta, useProject } from './store'

function PreviewProbe() {
  const { previewUser, setPreviewUser } = useMeta()
  const { currentProjectId, ready } = useProject()
  return (
    <>
      <output>{ready ? currentProjectId : 'loading'}</output>
      <button onClick={() => setPreviewUser(!previewUser)}>Toggle preview</button>
    </>
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  localStorage.clear()
})

it('预览排除当前管理员空间时展示成员个人空间，退出后恢复且不写项目选择', async () => {
  localStorage.setItem('console.project', 'admin-space')
  vi.spyOn(api, 'meta').mockResolvedValue({ authEnabled: true } as Meta)
  vi.spyOn(api.auth, 'me').mockResolvedValue({ accountId: 'user', platformAdmin: true } as Account)
  vi.spyOn(api.project, 'dict').mockResolvedValue([
    { id: 'admin-space', name: 'Admin', status: 'ENABLED', member: false },
    { id: 'disabled-personal', name: 'Disabled', kind: 'personal', member: true, disabled: true },
    { id: 'team', name: 'Team', member: true },
    { id: 'personal', name: 'Personal', kind: 'personal', member: true },
  ] as Project[])
  const update = vi.spyOn(api.project, 'update')
  render(
    <LangProvider>
      <MetaProvider>
        <ProjectProvider>
          <PreviewProbe />
        </ProjectProvider>
      </MetaProvider>
    </LangProvider>,
  )
  await screen.findByText('admin-space')
  await waitFor(() => expect(api.auth.me).toHaveBeenCalled())
  fireEvent.click(screen.getByRole('button', { name: 'Toggle preview' }))
  await screen.findByText('personal')
  fireEvent.click(screen.getByRole('button', { name: 'Toggle preview' }))
  await screen.findByText('admin-space')
  expect(update).not.toHaveBeenCalled()
  expect(localStorage.getItem('console.project')).toBe('admin-space')
})
