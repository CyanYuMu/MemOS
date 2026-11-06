import type { ReactNode } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { useT } from '../../lib/store'
import { MemoryDetailFields } from './MemoryDetailFields'
import type { TurnSnapshot } from './turnSnapshot'

/** 引用编号和卡片展开均读取同一个历史实体，不访问当前版本。 */
export function MemorySnapshotDetail({
  projectId,
  row,
  children,
}: {
  projectId: string
  row: TurnSnapshot['entries'][number]
  children: ReactNode
}) {
  const t = useT()
  return (
    <Dialog.Root>
      <Dialog.Trigger asChild>{children}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="memos-detail-overlay" />
        <Dialog.Content className="memos-detail-dialog">
          <header className="memos-detail-header">
            <Dialog.Title>{t('memory.detailTitle')}</Dialog.Title>
            <Dialog.Close className="memos-detail-close" aria-label={t('common.close')}>
              <X size={20} />
            </Dialog.Close>
          </header>
          <Dialog.Description className="sr-only">{t('chat.snapshot.description')}</Dialog.Description>
          <div className="memos-detail-body">
            {row.record && <MemoryDetailFields projectId={projectId} entry={row.entry} snapshot={row.record} />}
          </div>
          <footer className="memos-detail-footer">
            <span />
            <Dialog.Close className="memos-detail-close-button">{t('common.close')}</Dialog.Close>
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
