/** 文档页内容。只写客户能在控制台或接口上直接感知到的东西：
 *  接入方式、地址、认证、接口、限制、控制台功能；内部实现与运维细节留在这份代码之外。 */
import { SERVICE_BASE, LINKS } from './config'
import { buildMcpConfig } from './quickstart-data'

const bi = (zh: string, en: string) => ({ zh, en })

export interface Bi {
  zh: string
  en: string
}

export type DocBlock =
  | { kind: 'p'; text: Bi }
  | { kind: 'code'; text: string }
  | { kind: 'list'; items: Bi[] }
  | { kind: 'table'; head: Bi[]; rows: Bi[][] }
  | { kind: 'note'; text: Bi }

export interface DocSection {
  id: string
  title: Bi
  blocks: DocBlock[]
  subsections?: { id: string; title: Bi; blocks: DocBlock[] }[]
}

export function buildDocs(): DocSection[] {
  return [
    {
      id: 'overview',
      title: bi('概览', 'Overview'),
      blocks: [
        {
          kind: 'p',
          text: bi(
            '记忆服务为 Agent 提供跨会话的长期记忆：对话开始前召回相关记忆，对话结束后把新信息写回。',
            'The memory service gives agents long-term memory across sessions: it recalls relevant memories before a turn and writes new information back afterwards.',
          ),
        },
        {
          kind: 'list',
          items: [
            bi('按项目隔离：记忆、密钥、用量都分项目存放。', 'Isolated by project: memories, keys and usage are kept per project.'),
            bi(
              '可查可控：控制台能检索每条记忆、查看调用日志与用量。',
              'Inspectable: search every memory, review call logs and usage in the console.',
            ),
          ],
        },
      ],
    },
    {
      id: 'access',
      title: bi('接入方式', 'Getting started'),
      blocks: [],
      subsections: [
        {
          id: 'hermes-mcp',
          title: bi('Hermes MCP', 'Hermes over MCP'),
          blocks: [
            { kind: 'code', text: `hermes mcp add graphmemory --url ${LINKS.mcp}` },
            {
              kind: 'p',
              text: bi(
                '按提示确认鉴权并粘贴密钥。工具名以 mcp_graphmemory_ 开头，例如 mcp_graphmemory_search_memory。',
                'Confirm auth when prompted and paste the key. Tool names start with mcp_graphmemory_, for example mcp_graphmemory_search_memory.',
              ),
            },
          ],
        },
        {
          id: 'mcp-clients',
          title: bi('其他 MCP 客户端', 'Other MCP clients'),
          blocks: [
            { kind: 'p', text: bi('把下面这段加入客户端的 MCP 配置：', 'Add this to the client MCP config:') },
            { kind: 'code', text: buildMcpConfig() },
          ],
        },
        {
          id: 'other-agents',
          title: bi('其他 Coding Agent', 'Other coding agents'),
          blocks: [
            {
              kind: 'note',
              text: bi('Claude Code、Codex 等适配中，暂未支持。', 'Claude Code, Codex and others are not supported yet.'),
            },
          ],
        },
      ],
    },
    {
      id: 'auth',
      title: bi('地址与认证', 'Endpoint and auth'),
      blocks: [
        {
          kind: 'table',
          head: [bi('项目', 'Item'), bi('值', 'Value')],
          rows: [
            [bi('MCP 地址', 'MCP endpoint'), bi(LINKS.mcp, LINKS.mcp)],
            [bi('REST 基址', 'REST base URL'), bi(SERVICE_BASE, SERVICE_BASE)],
            [bi('认证方式', 'Auth'), bi('Authorization: Bearer <API Key>', 'Authorization: Bearer <API Key>')],
          ],
        },
        {
          kind: 'p',
          text: bi(
            'API Key 在控制台「API Key」页按项目创建，创建后立即生效；删除或重新生成后旧密钥立即失效。',
            'Create API keys per project on the API Key page; they take effect immediately, and deleting or regenerating one invalidates the old key at once.',
          ),
        },
      ],
    },
    {
      id: 'rest',
      title: bi('REST 接口', 'REST API'),
      blocks: [
        {
          kind: 'table',
          head: [bi('方法', 'Method'), bi('路径', 'Path'), bi('用途', 'Purpose')],
          rows: [
            [
              bi('POST', 'POST'),
              bi('/memory/add', '/memory/add'),
              bi(
                '写入一轮对话，异步抽取为记忆，返回 call_id',
                'Write a conversation turn; extraction runs in the background and returns a call_id',
              ),
            ],
            [bi('POST', 'POST'), bi('/memory/search', '/memory/search'), bi('检索记忆', 'Search memories')],
            [bi('POST', 'POST'), bi('/memory/delete', '/memory/delete'), bi('按 id 删除记忆', 'Delete memories by id')],
            [
              bi('POST', 'POST'),
              bi('/memory/update', '/memory/update'),
              bi('修改记忆并保留旧版本', 'Update a memory and preserve its history'),
            ],
            [bi('POST', 'POST'), bi('/memory/feedback', '/memory/feedback'), bi('提交纠正反馈', 'Submit a correction')],
            [bi('POST', 'POST'), bi('/memory/profile', '/memory/profile'), bi('读取用户画像', 'Read the user profile')],
            [
              bi('GET', 'GET'),
              bi('/memory/whoami', '/memory/whoami'),
              bi('查看当前密钥对应的用户与项目', 'Show the user and project behind the key'),
            ],
          ],
        },
      ],
    },
    {
      id: 'limits',
      title: bi('参数与限制', 'Parameters and limits'),
      blocks: [
        {
          kind: 'list',
          items: [
            bi('检索条数 top_k 最大 25，默认 10。', 'top_k is capped at 25, default 10.'),
            bi('单次请求体最大 4 MiB。', 'Request bodies are limited to 4 MiB.'),
            bi(
              '写入是异步的：add 立即返回 call_id，内容在后台抽取入库，可在控制台「记忆」页查看结果。',
              'Writes are asynchronous: add returns a call_id immediately, extraction continues in the background, and results show up on the Memories page.',
            ),
          ],
        },
      ],
    },
    {
      id: 'console',
      title: bi('控制台功能', 'Console'),
      blocks: [
        {
          kind: 'p',
          text: bi('已上线：', 'Available now:'),
        },
        {
          kind: 'list',
          items: [
            bi(
              '项目：记忆按项目隔离，密钥与成员都挂在项目下。',
              'Projects: memories are isolated per project; keys and members belong to a project.',
            ),
            bi('API Key：按项目创建、复制、删除或重新生成。', 'API keys: create, copy, delete or regenerate per project.'),
            bi('记忆：检索、查看详情与关联图谱。', 'Memories: search, open details and the relation graph.'),
            bi('请求日志：每次调用的入参、结果与耗时。', 'Request log: inputs, results and latency for every call.'),
            bi('用量：按时间查看调用次数与 token 用量。', 'Usage: calls and token usage over time.'),
            bi('自定义抽取：调整记忆抽取的提示词并试跑。', 'Custom extraction: adjust the extraction prompt and try it out.'),
          ],
        },
      ],
      subsections: [
        {
          id: 'roadmap',
          title: bi('排期中（尚未上线）', 'Planned (not shipped yet)'),
          blocks: [
            {
              kind: 'list',
              items: [
                bi(
                  '知识库：上传项目文档，与记忆一起参与检索。',
                  'Knowledge base: upload project documents and search them alongside memories.',
                ),
                bi('画像模板：自定义用户画像的字段与展示方式。', 'Profile templates: customise profile fields and how they are shown.'),
                bi('其他 Coding Agent 接入：Claude Code、Codex 等。', 'Other coding agents: Claude Code, Codex and more.'),
              ],
            },
            {
              kind: 'note',
              text: bi('顺序与时间未定，以控制台实际上线为准。', 'Order and timing are not fixed; the console is the source of truth.'),
            },
          ],
        },
      ],
    },
  ]
}
