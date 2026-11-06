/** 快速开始页四种接入方式的内容。命令里的**地址**是真实值，直接可跑；
 *  **密钥一律是占位符**——见下面 `KEY_PLACEHOLDER` 的说明。 */

import type { Lang, TKey } from './i18n'
import type { ToolInfo } from './api'
import { LINKS, SERVICE_BASE } from './config'
import zhCopy from '../locales/zh.json'
import enCopy from '../locales/en.json'

/** 示例里密钥一律写成占位符，**不把真实密钥渲染进页面**。
 *
 *  ⚠️ 这里原来是把「当前项目的第一把密钥」查出来、直接嵌进每一段示例代码和提示词里，
 *  页面看着很贴心（复制即用），但代价是：一把**能用的密钥**被渲染在一个没有权限校验的
 *  静态页面上，任何能看到这个页面的人（截图、投屏、录屏）都顺手拿到了它，
 *  而页面本身**看不出来这是个秘密**。2026-10-19 用户指出后改成占位符：
 *  密钥去「接口密钥」页创建，示例里只留 `YOUR_API_KEY`。 */
const KEY_PLACEHOLDER = 'YOUR_API_KEY'

// 下载入口和插件运行时网关必须属于同一个部署环境。
const shellQuote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'"

/** 服务暴露的工具清单由 /api/meta 提供，示例命令据此生成，不写死在前端 */
export interface ServiceFacts {
  tools: ToolInfo[]
}

const bi = (zh: string, en: string) => ({ zh, en })

export interface Step {
  n: string
  title: { zh: string; en: string }
  desc?: { zh: string; en: string }
  code: string
  /** 多行代码用 <pre>，单行用 <code> */
  multiline: boolean
  /** 这一步在控制台里有对应页面时，渲染成一个跳转按钮而不是代码块。
   *  只在「当前空间还没有密钥」时作为兜底展示。 */
  link?: { to: string; label: { zh: string; en: string } }
  /** 取密钥这一步交给页面自己渲染：有密钥就列出来直接复制，没有才退回 link。 */
  kind?: 'apikey'
}

/** 接入方式的 id。
 *
 *  ⚠️ 刻意写成**联合类型**而不是 `string`：下面 `HINT_COPY` 是 `Record<AccessId, …>`，
 *  于是「加了一个页签、却忘了写它在浮层里的说明」会**编译不过**。
 *  这一页原来的毛病正是两处各写一份然后对不上——浮层里写着 Plugin，
 *  而实际上我们根本没有 Plugin 这种接入形式（2026-10-19 用户指出的）。 */
export type AccessId = 'hermes' | 'prompt' | 'coming-soon' | 'python-http' | 'typescript-http' | 'curl'

export interface AccessTab {
  id: AccessId
  label: string
  /** 少数标签需要中英两版 */
  labelEn?: string
  icon: string
  head: { zh: string; en: string }
  docs: string
  steps: Step[]
  /** 「提示词」走另一套版式：一段可复制的提示词 */
  prompt?: { zh: string; en: string }
  /** 还没有内容的页签（如其他 Coding Agent）只给一段说明 */
  note?: { zh: string; en: string }
}

export function buildMcpConfig() {
  return `{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "${LINKS.mcp}",
      "headers": { "Authorization": "Bearer ${KEY_PLACEHOLDER}" }
    }
  }
}`
}

/** Hermes 一条命令接入。
 *
 *  子命令是 `hermes mcp add <name> --url <url>`；加完会交互式问鉴权，
 *  密钥落在 `~/.hermes/.env` 的 `MCP_<名字大写>_API_KEY`。
 *
 *  server 名决定工具前缀：`tools/mcp_tool.py` 拼的是
 *  `mcp_<server>_<tool>`，所以叫 `graphmemory` 得到
 *  `mcp_graphmemory_search_memory`，叫 `memory` 会得到
 *  `mcp_memory_search_memory`（名字里 memory 重复一次）。 */
const hermesCmd = `hermes mcp add graphmemory --url ${LINKS.mcp}`

const buildPrompt = (tools: ToolInfo[], en = false) => {
  // tools 是后端 /api/meta 下发的工具名数组；之前按 {name,title} 对象取字段，
  // 运行时全是 undefined，提示词里就出现了「undefined（undefined）」。
  const list = tools.length ? tools.join(en ? ', ' : '、') : en ? 'several tools' : '若干工具'
  const n = tools.length || (en ? 'several' : '')
  if (en) {
    return `Please connect the memory service to this Agent's MCP configuration.

Service URL: ${LINKS.mcp}
Authentication: request header Authorization: Bearer ${KEY_PLACEHOLDER}

Please:
1. Add an MCP server named memory (HTTP transport) with the URL and header above.
2. Restart the Agent's MCP gateway and confirm the connection.
3. After connecting, you should see ${n} tools (${list}).
4. Call a search tool once to verify the link; an empty list is expected (no memories yet).

If my client uses a different config format, adapt it to that format and tell me which file you changed.`
  }
  return `请帮我把记忆服务接入当前 Agent 的 MCP 配置。

服务地址：${LINKS.mcp}
鉴权方式：请求头 Authorization: Bearer ${KEY_PLACEHOLDER}

请完成：
1. 在 Agent 的 MCP 配置里新增一个名为 memory 的 http 类型服务，填上面的地址与鉴权头。
2. 重启 Agent 的 MCP 网关，确认连接成功。
3. 连上后应该能看到 ${n} 个工具（${list}）。
4. 调一次检索工具验证链路，返回空列表也是正常的（还没有记忆）。

如果我的客户端配置格式和上面不一样，请按它的格式改写，并告诉我改了哪个文件。`
}

/** 快速开始页提供的接入方式：Hermes（MCP / 插件两种）与「让 Agent 自己配」。
 *
 *  通用 MCP 页签已下线：面向具体客户端的配置写进文档页，快速开始只留能一条命令
 *  跑通的路径。 */
export function buildTabs(facts: ServiceFacts): AccessTab[] {
  return [
    {
      id: 'hermes',
      label: 'Hermes（MCP）',
      labelEn: 'Hermes (MCP)',
      icon: 'i-lucide:terminal',
      head: bi('通过 MCP 接入 Hermes', 'Wire up Hermes over MCP'),
      docs: LINKS.docs,
      steps: [
        {
          n: '1',
          title: bi('获取 API Key', 'Get an API key'),
          code: KEY_PLACEHOLDER,
          multiline: false,
          kind: 'apikey',
          link: { to: '/cn/apikeys', label: bi('去创建密钥', 'Create a key') },
        },
        {
          n: '2',
          title: bi('添加 MCP 服务', 'Add the MCP server'),
          desc: bi('运行后按提示确认鉴权并粘贴密钥。', 'Confirm auth when prompted, then paste the key.'),
          code: hermesCmd,
          multiline: true,
        },
      ],
    },
    {
      id: 'prompt',
      label: '让 Agent 自己配',
      labelEn: 'Let the agent do it',
      icon: 'i-lucide:sparkles',
      head: bi('', ''),
      docs: LINKS.docs,
      steps: [],
      prompt: bi(buildPrompt(facts.tools), buildPrompt(facts.tools, true)),
    },
    {
      id: 'coming-soon',
      label: '其他 Coding Agent',
      labelEn: 'Other coding agents',
      icon: 'i-lucide:rocket',
      head: bi('其他 Coding Agent 适配', 'Other coding agents'),
      docs: LINKS.docs,
      steps: [],
      note: bi('适配中，敬请期待。', 'Coming soon.'),
    },
  ]
}

export const tabLabel = (tab: AccessTab, lang: Lang) => (lang === 'en' && tab.labelEn ? tab.labelEn : tab.label)

/** 「选择哪种接入方式？」浮层里每一行的「适合场景 / 特点」。
 *
 *  ⚠️ 方式名**不写在这里**：它取自页签自己的 label（见 `AccessMethodHint` 的调用点），
 *  同一个名字只有一处维护。
 *
 *  ⚠️ 键是 `Record<AccessId, …>`：加了页签却忘了写说明，**编译期就会报错**。
 *  原来浮层里写死了三行 `Plugin / CLI + Skill / MCP`，其中两个我们都没提供
 *  （2026-10-19 用户指出「跟我们提供的接入方式还不太匹配」）。 */
export const HINT_COPY: Record<AccessId, { scene: TKey; feature: TKey }> = {
  'python-http': { scene: 'quickstart.hint.code.scene', feature: 'quickstart.hint.code.feature' },
  'typescript-http': { scene: 'quickstart.hint.code.scene', feature: 'quickstart.hint.code.feature' },
  curl: { scene: 'quickstart.hint.code.scene', feature: 'quickstart.hint.code.feature' },
  hermes: { scene: 'quickstart.hint.hermes.scene', feature: 'quickstart.hint.hermes.feature' },
  prompt: { scene: 'quickstart.hint.prompt.scene', feature: 'quickstart.hint.prompt.feature' },
  'coming-soon': { scene: 'quickstart.hint.coming-soon.scene', feature: 'quickstart.hint.coming-soon.feature' },
}

/** 示例绑定当前环境的真实REST契约，不依赖未发布的SDK。 */
export function buildCodeTabs(): AccessTab[] {
  const copy = (key: keyof typeof zhCopy) => bi(String(zhCopy[key]), String(enCopy[key]))
  const messages = {
    conversation_first_message: 'personal-assistant-demo',
    messages: [{ role: 'user', content: 'I prefer concise answers with the conclusion first.' }],
  }
  const search = { query: 'What are my answer preferences?', conversation_first_message: 'personal-assistant-demo', top_k: 5 }
  const key: Step = {
    n: '1',
    title: copy('quickstart.code.key'),
    code: KEY_PLACEHOLDER,
    multiline: false,
    kind: 'apikey',
    link: { to: '/cn/apikeys', label: copy('quickstart.code.getKey') },
  }
  const icons = {
    'python-http': '/images/access/python.svg',
    'typescript-http': '/images/access/typescript.svg',
    curl: '/images/access/bash.svg',
  }
  const request = (name: string, kind: keyof typeof icons, configure: string, add: string, query: string): AccessTab => ({
    id: kind,
    label: name,
    icon: icons[kind],
    head: copy('quickstart.code.heading'),
    docs: LINKS.docs,
    steps: [
      key,
      { n: '2', title: copy('quickstart.code.configure'), code: configure, multiline: true },
      { n: '3', title: copy('quickstart.code.add'), desc: copy('quickstart.code.addHint'), code: add, multiline: true },
      { n: '4', title: copy('quickstart.code.search'), desc: copy('quickstart.code.searchHint'), code: query, multiline: true },
    ],
  })
  const pyConfig = `import json
import os
from urllib.request import Request, urlopen

BASE_URL = os.getenv("GRAPH_MEMORY_BASE_URL", ${JSON.stringify(SERVICE_BASE)})
API_KEY = os.getenv("GRAPH_MEMORY_API_KEY", "YOUR_API_KEY")  # YOUR_API_KEY

def memory_request(path, data):
    request = Request(
        BASE_URL + path,
        data=json.dumps(data).encode(),
        headers={"Authorization": "Bearer " + API_KEY,
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)`
  const tsConfig = `const BASE_URL = ${JSON.stringify(SERVICE_BASE)};
const API_KEY = process.env.GRAPH_MEMORY_API_KEY ?? "YOUR_API_KEY"; // YOUR_API_KEY

async function memoryRequest(path: string, data: unknown) {
  const response = await fetch(BASE_URL + path, {
    method: "POST",
    headers: {
      Authorization: "Bearer " + API_KEY,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(data),
    signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}`
  const curl = (path: string, data: unknown) => `curl --fail-with-body "$GRAPH_MEMORY_BASE_URL${path}" \\
  -H "Authorization: Bearer $GRAPH_MEMORY_API_KEY" \\
  -H 'Content-Type: application/json' \\
  --data ${shellQuote(JSON.stringify(data, null, 2))}`
  return [
    request(
      'Python (HTTP)',
      'python-http',
      pyConfig,
      `result = memory_request("/memory/add", ${JSON.stringify(messages, null, 2)})
print(result)`,
      `result = memory_request("/memory/search", ${JSON.stringify(search, null, 2)})
print(result)`,
    ),
    request(
      'TypeScript (HTTP)',
      'typescript-http',
      tsConfig,
      `console.log(await memoryRequest("/memory/add", ${JSON.stringify(messages, null, 2)}));`,
      `console.log(await memoryRequest("/memory/search", ${JSON.stringify(search, null, 2)}));`,
    ),
    request(
      'cURL',
      'curl',
      `export GRAPH_MEMORY_API_KEY='YOUR_API_KEY'
export GRAPH_MEMORY_BASE_URL=${shellQuote(SERVICE_BASE)}`,
      curl('/memory/add', messages),
      curl('/memory/search', search),
    ),
  ]
}
