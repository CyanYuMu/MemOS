# Claude Code 日常会话采集：开发与验收

日期：2026-11-11。分支：`yumu/claude-daily-capture`。

本机安装与实际验证结果见 [日常采集验收记录](skill-prototype-daily-capture-results.md)。

## 目标与边界

用户在 MemOS、MemOS-be、MemOS-fe 中直接启动本机 Claude Code 后，自动收集新产生的请求、工具调用与结果、每轮最终回复，后台保存到现有 Memory 服务，并提供本地状态与证据查看入口。用户已选择仅接入这三个项目；保留全部现有 Claude 配置与 Hook，不追溯导入历史会话。

这是日常经验接入，不替代已冻结的受控验收协议。日常记录统一标记 `observed_unverified`，不能冒充 qualified_success、独立验证或 Skill 发布资格。不自动提炼或发布 Skill，也不向普通会话注入当前仅适用于固定任务的 Dream Skill。

## 实现顺序

1. 定义轻量 Hook、私有事件账本和配置安装器，先测试范围隔离、配置合并与脱敏。
2. 实现后台经验归档、Memory 提交与真实回读；测试断网、未知提交、重复事件和重启恢复。
3. 提供只读本机状态页面及 CLI；配置 macOS 用户级后台服务，服务退出后能重启，采集不依赖浏览器或本次聊天。
4. 安装项目级 Hook，运行真实 Claude 会话及范围外对照，确认 Memory 中可回读的新记录。写实际结果与使用/停用说明，中文提交。

## 接入契约

- Hook 使用独立、无第三方依赖的 Python 入口，读取 stdin JSON，快速写入本机 SQLite；Hook 内不联网、不调用模型、不执行传入命令，不改变工具权限或用户任务结果。
- 事件：SessionStart、UserPromptSubmit、PreToolUse、PostToolUse、PostToolUseFailure、Stop、StopFailure、SessionEnd。保留 session/tool/turn 关联；工具事件允许并发与乱序。Stop 仅表示一轮回复结束，SessionEnd 也不代表任务成功；API 失败另记，缺最终回复显式标记。
- 捕获白名单字段：用户请求、工具名/输入/输出/错误、最终回复及运行标识。排除内部推理、凭据字段和敏感文件正文；文本按模式及本机已知凭据脱敏，超长内容截断并标记。首版不读取历史 transcript，也不宣称完整记录所有中间自然语言消息或子 Agent。
- 仅配置目录及其子目录允许采集；目录按真实路径判断，范围外事件直接忽略。首次启用后的事件才进入新账本。多个项目与会话各自归属，不通过模糊路径前缀混合。
- 每轮归档绑定实际事件及其摘要，保留缺失开始/结果、未正常结束、截断等事实。后台在安静窗口后归档，迟到事件产生新修订，不覆盖已送入 Memory 的旧证据。
- 本地服务不可用时事件仍留在本机。已尝试但结果未知的 Memory POST 不自动重发，只做带精确归属的查询回读；离线且尚未提交的记录恢复后正常提交。
- Memory 使用独立日常采集命名空间和明确 user/cube 归属；不改动既有来源 Memory。记录包含 `observed_unverified`、经验摘要和本地证据摘要。引用存在并不意味着来源足以支持可复用规则。
- 支持 Memory fast 模式把完整来源保留在多个正文片段上的格式：精确核对归属、摘要与原始来源覆盖，回读标记 `source_identity_only`。这只证明来源身份一致，不证明派生正文与原文语义等价。原受控 MemoryBridge 的资格门槛不变。
- 安装器只追加自己管理的 Hook，备份原配置，重复安装幂等；卸载只移除自己的命令，不恢复整份旧配置覆盖用户后续编辑。配置、账本、日志和凭据均在仓库外。
- 状态页面只监听回环地址，不提供写接口、跨源读取或任意文件读取；页面对文本转义，显示采集、待同步、回读确认和错误状态。

## 验收标准

1. 原有配置与 Hook 保持；三项目安装、重复安装与卸载均可核对。
2. 真实本机 Claude 原生 Hook 自动收到请求、工具开始/结果和最终回复；不依赖原受控执行器的临时设置。
3. 范围外目录无记录；敏感字段/敏感文件、内部推理和模拟密钥不进入存储或页面。
4. 两个并发会话、重复投递、进程重启、无 Stop、中断与截断保留可解释状态，不凭最终回复升级成功。
5. 后台进程自动形成经验并写入 Memory，回读核对命名空间、用户、cube、操作标识、证据摘要和内容来源；记录真实 Memory ID。
6. 服务断网不阻塞 Claude；已知未发送与提交结果未知分开处理，未知不重复 POST。
7. 浏览器能看到真实会话、事件、同步结果与“不具备提炼/发布资格”的标记。后台服务在本次执行结束后继续可用。

## 后续边界

自动经验聚类和候选触发、真实项目独立验收、一般任务 Skill 的检索加载需要后续契约；不能把本轮采集接入称为它们已经完成。模式脱敏不能保证识别所有语义上的私密信息；只采集用户选定的项目，可随时停用并保留已有数据。

参考：[Claude Code Hook 官方契约](https://code.claude.com/docs/en/hooks)。部署时以本机 CLI 版本实测为准，不要求替换原受控实验使用的固定 CLI。

## 模块与安装

`src/memos/skill_capture/` 是新增的独立接入模块：`core.py` 处理范围、脱敏、事件与 SQLite 账本，`worker.py` 做经验修订及 Memory 回读，`install.py` 合并和移除 Hook，`deployment.py` 部署不可变程序副本与 LaunchAgent，`dashboard.py` 提供只读状态页，`cli.py`/`entrypoint.py` 为轻量命令入口。Hook 不导入整个 MemOS 模型运行时。

本机私有状态在 `~/.local/share/memos-claude-capture/`；项目的 `.claude/settings.local.json` 已加入各仓库本地 Git 排除，备份同样排除。运行程序复制到状态目录的内容寻址 `runtime` 子目录，不依赖当前 Git 分支；Python 解释器仍来自项目 `.venv`，不要删除该环境。全局 `~/.claude/settings.json` 不修改。

通用配置示例（将路径换成自己的绝对路径，文件权限必须为 0600，状态目录 0700）：

```json
{
  "version": 1,
  "enabled": true,
  "stateDir": "/absolute/private/capture",
  "projects": [{"name": "my-project", "path": "/absolute/projects/my-project"}],
  "memory": {"url": "http://127.0.0.1:64068", "userId": "daily-owner", "cubeId": "daily-cube"},
  "redactValues": [],
  "port": 5282,
  "settleSeconds": 8
}
```

受认证 Memory 服务可在 `memory.token` 提供现有令牌，放在私有配置中。`redactValues` 是额外需要去除的已知秘密值。该配置不能把任意项目自动授权为既有 Skill 的适用范围。

在仓库根目录使用：

```bash
.venv/bin/python -I src/memos/skill_capture/entrypoint.py install --config /absolute/private/config.json
.venv/bin/python -I src/memos/skill_capture/entrypoint.py service-install --config /absolute/private/config.json
```

`install` 安装或升级三个项目的 Hook；已有其他 Hook 原样保留。`service-install` 为 macOS 当前用户配置登录启动和退出重启，并检查就绪。其他平台可用同一入口的 `serve` 命令接入进程管理器，未做跨平台实测。

新开 Claude 会话，按平时方式执行任务即可；无需从 Skill 前端发起。若已有会话没有重新加载 Hook，退出并重新启动。模型认证由原 Claude 配置负责，采集器不提供或覆盖登录凭据。每轮安静 8 秒后归档；未正常结束的轮次在闲置 5 分钟后保存为不完整观察。服务断网时保留事件和待同步状态。

查看 `http://127.0.0.1:5282/`，也可用 `status --config ...` 查看结构化状态。`worker.log`、`worker-error.log` 在私有状态目录。出现 `invalid`，先核对问题，再用以下命令请求只读回读；原错误保留在 `delivery_observations`，绝不把已尝试提交的记录放回待 POST 状态：

```bash
.venv/bin/python -I src/memos/skill_capture/entrypoint.py reconcile --experience EXPERIENCE_ID --config /absolute/private/config.json
```

停用自动采集（保留事件、Memory 与用户其他配置）：

```bash
.venv/bin/python -I src/memos/skill_capture/entrypoint.py uninstall --config /absolute/private/config.json
```

它移除自己管理的 Hook 并关闭采集配置；旧会话中仍缓存的同一 Hook 也会读取关闭标记。后台可继续处理已经采集的队列。完全停止后台时执行 `launchctl bootout gui/$(id -u)/org.memos.claude-capture`；如不再需要登录启动，可将 `~/Library/LaunchAgents/org.memos.claude-capture.plist` 移到备份位置。重新启用时执行 `install` 和 `service-install`。
