/** 模型列表。
 *
 *  ⚠️ 这一页**读的是上游 Pod 真正在读的那份配置**（后端 `GET /api/dashboard/models`
 *  → 上游 `/product/models`），不是前端维护的静态副本。
 *
 *  2026-10-19 之前它读 `public/tech-config/*.json`，那是一份**手写的假清单**：
 *  模型名是编的（`memory-writer` / `embedding-jina-v5` / `rerank-gte` / `deepseek-v3` /
 *  `claude-sonnet-4-6` / `qwen3-32b`），还混了一条标着「规划中」的 `dream-organizer`。
 *  上游一个都不叫这些名字——用户一眼就看出来了（「数据好像都是假的？」）。
 *
 *  ⚠️ 同时删掉了每行那个「查看详情」按钮：它只弹一句「模型详情页还没接，先看文档吧」，
 *  点了等于没点。留着比删掉更糟——业务会以为是自己没点对。行内现在只剩**真能用**的
 *  「复制模型名称」。 */
import { useState } from 'react'
import { api, type ModelRecord } from '../lib/api'
import { useAsync } from '../lib/useAsync'
import { useT, useToast } from '../lib/store'
import { copyText } from '../lib/clipboard'
import { PageHeader, Panel, StateRow, Table, TD, Th } from '../components/ui'
import { Tabs } from '../components/ui/tabs'
import { RowActions } from '../components/ui/row-actions'

const TABS = [
  { key: 'memory', label: 'models.tab.memory' },
  { key: 'chat', label: 'models.tab.chat' },
] as const

/** 「记忆读取」这类角色名做成标签，扫一眼就能分出链路位置。
 *
 *  ⚠️ 2026-10-19 起**不再显示 `spec`（提供方主机名 / 向量维度 / 重排后端）**，
 *  列头也从「用途与参数」改成「用途」——用户的要求是「参数不用写了」。
 *  接口里 `spec` 字段还留着（上游配置的真实值，见 `adapters/models.ts`），
 *  要恢复显示把上面那个 `SPEC_TAG` 和一行 `<span>` 加回来就行。 */
const ROLE_TAG = 'inline-block h-6 px-2.5 text-xs font-medium rounded-md leading-6 bg-accent-soft border border-accent/30 text-accent'

const fetchModels = async (): Promise<ModelRecord[]> => (await api.models.list()).records

export function Models() {
  const t = useT()
  const toast = useToast()
  const [tab, setTab] = useState<(typeof TABS)[number]['key']>(TABS[0].key)
  const { data, state, error } = useAsync(fetchModels, [])

  const copy = async (text: string) => {
    try {
      await copyText(text)
      toast(t('common.copied'))
    } catch {
      // ⚠️ 不写「请求失败」：这里根本没发请求。控制台跑在 `http://<内网 IP>:8280`，
      //    不是 secure context，`navigator.clipboard` 是 undefined，锅在浏览器。
      toast(t('common.copyFailed'), 'err')
    }
  }

  const rows = (data ?? []).filter((m) => m.group === tab)
  const cols = 3

  return (
    <>
      <PageHeader title={t('models.title')} desc={t('models.desc')} />

      <div className="flex flex-col flex-1 space-y-4 h-full">
        <Tabs label={t('models.title')} items={TABS.map((x) => ({ key: x.key, label: t(x.label) }))} value={tab} onChange={setTab} />

        <div className="flex-1">
          <div className="h-full flex flex-col justify-between">
            <Panel>
              <Table
                head={
                  <>
                    <Th>{t('models.col.modelName')}</Th>
                    <Th>{t('models.col.role')}</Th>
                    <Th>{t('models.col.action')}</Th>
                  </>
                }
              >
                {state !== 'ready' && <StateRow colSpan={cols} state={state} error={error} />}
                {state === 'ready' && rows.length === 0 && <StateRow colSpan={cols} state="ready" />}
                {state === 'ready' &&
                  rows.map((m) => (
                    <tr key={m.source} className="data-[selected=true]:bg-elevated/50">
                      <td data-pinned="false" className={TD}>
                        <span className="leading-5 text-second-gray">{m.model}</span>
                      </td>
                      <td data-pinned="false" className={TD}>
                        <span className={ROLE_TAG}>{m.role}</span>
                      </td>
                      <td data-pinned="false" className={TD}>
                        <RowActions actions={[{ label: t('models.copyModelName'), icon: 'i-lucide:copy', onClick: () => copy(m.model) }]} />
                      </td>
                    </tr>
                  ))}
              </Table>
            </Panel>
          </div>
        </div>
      </div>
    </>
  )
}
