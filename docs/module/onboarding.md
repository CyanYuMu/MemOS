---
module: onboarding
packages:
  - internal/service/onboarding
status: implemented
---

# 平台Web引导

控制面保存账号级引导状态与账号/项目级检查点，不写入用户记忆，不启动Pi或生成凭据。

## 职责与边界

负责个人操作进度，不代表记忆成功审计。账号认证和项目权限复用auth/account，聊天、记忆、Key操作继续由既有模块执行。

## 依赖

service端口由adapter/store实现，依赖domain模型；transport/auth调用service，app组合根注入。所有读写同时限定账号及项目。

## 契约

`GET /api/auth/onboarding?projectId=...` 返回 `{code:0,data:{status,step,revision,version}}`；`PUT` 同路径接收 `{projectId,action,revision}`。action为start/advance/dismiss/replay。status为pending/active/dismissed/completed，step为0..6，version固定1。

只接受真实账号身份，包括无SessionID的统一认证账号；账号ID来自认证主体，客户端不能指定。项目必须显式指定，具有project:view。无权限403，未登录401，非法动作400，过期revision409。GET不创建状态或触发业务动作。PUT在单个事务中CAS更新账号revision和本项目检查点，防止多标签/跨项目迟到写覆盖；start保留步骤，replay明确重置本项目步骤，advance只增加一步，完成第六步时status=completed。

迁移0033仅追加两张控制面表；迁移时为全部已有账号写dismissed，老账号不自动弹窗。后建账号缺省pending。重播不删旧对话/记忆。切项目读取自己的检查点，全局dismissed/completed不自动开场。

聊天子步骤通过action=checkpoint追加保存`checkpoint:{stage,first,second?}`，只允许active状态的step3。stage为first/logs/recall/finish，first/second仅保存真实会话ID，不保存表单或消息。服务端逐一查询会话并验证当前账号/项目归属；recall/finish要求两个不同会话。GET通过chat字段返回检查点，跨设备可恢复到原会话，不自动重发消息。

端口在service/onboarding声明，store实现SQLite读写；transport/auth负责JSON/认证与错误映射；app装配。前端按真实聊天state、写入/召回结果控制业务步骤，服务端保存的是个人操作进度，不将其作为业务成功审计。模型结果继续以既有事件为准。

## 错误与边界情况

读失败不返回虚构默认进度；CAS冲突409要求刷新，不能覆盖新进度。外部身份不要求SessionID，管理Token不提供用户指引状态。项目切换不复用检查点，重播仅清本项目指引关联，不删除业务记录。

## 测试与验证

SQLite关闭重开持久化、CAS冲突、跨账号/项目隔离、老账号迁移抑制；HTTP未登录、外部身份、越权项目、JSON错误；无凭据或模型副作用。前端覆盖断点恢复、失败保留、Chat单一遮罩和无Key恢复。

## 参考来源

`account/external.go`和`account/personal.go`的账号及空间创建，`service/chat/chat.go`的聊天前置条件，`transport/auth/access.go`的项目权限规则。

## 实现记录

See the public version-control history.
