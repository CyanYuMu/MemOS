---
module: skill-lifecycle
status: accepted
packages:
  - internal/transport/dashboard
  - internal/adapter/upstream
---

# Skill 生命周期控制面接入

日期：2026-11-09；分支 `codex/skill-prototype-lifecycle-be`。基于 Python 的 `docs/skill-prototype-delivery.md`，本文先于实现提交；精确 JSON 以生命周期子合同及新版事实合同为准。

## 职责与边界

范围：为来源/工具扫描、固定影响审阅包、挑战处置、恢复与撤回增加严格同源路由；支持新版事实的健康成功和停止判断。Python 负责观察、证据和图事务，Go 不接受浏览器提交任意路径、命令、通过结果或 actor。

## 依赖

使用现有 session、项目能力检查、SkillGateway 与固定 Python 管理服务，不新增依赖。

## 契约

所有管理写继续需要项目 manage 权限、可信会话 actor、固定版本、非负安全序号、操作 ID 和理由。审阅动作还绑定包 ID/hash。未知字段、伪造主体、错误枚举和跨项目输入拒绝；上游调用不自动重试。审阅代办写入明确理由，不将助手执行描述为用户逐项亲审。

工作顺序：先与 Python 冻结请求字段及路由，写失败测试，再实现路由/字段检查/上游白名单，更新 OpenAPI 并生成前端副本。已有裁决 schema 1–5 和暂停/撤回行为保持兼容；不削弱既有权限或证据门槛。

## 错误与边界情况

异常状态仅转发既有安全错误码和序号，不回显探针端点、文件路径或凭据。陈旧包必须重读，不自动批准。

## 测试与验证

验收：合法新请求可达正确 Python action；viewer/editor 不得管理；未知字段/类型/序号/摘要被拒绝；actor 不由请求覆盖；冲突仅安全转发且无重试；Go `make check` 和 OpenAPI 生成通过。实际服务与浏览器联调由主代理执行，运行证据与私有配置不提交仓库。

## 参考来源

Python 仓库 `docs/skill-prototype-delivery.md`、`docs/skill-prototype-lifecycle.md`；本仓库 [dashboard-routes](dashboard-routes.md) 和 OpenAPI。

## 实现记录

2026-11-09：先冻结开发边界；实际结果在实现结束后追加。

同日 D2：新增 `/lifecycle/scan|prepare|review` 与 `/challenges/resolve`，对应 `impact_scan|impact_prepare|impact_review|challenge_resolve`；OpenAPI 的请求、响应及前端派生 JSON 同步。摘要数组拒绝重复及非摘要，decision 校验按具体 action 隔离；旧审阅接口不能借新枚举执行恢复。健康确认与未解决确认仅扩展传输枚举，实际事实资格仍由数据面判断。

验证：新路由先因 404、裁决枚举先因拒绝而 RED；实现后 dashboard/upstream 包全部通过。`make check` 完成格式、lint（0 issues）、vet、全库 race 乱序测试、tidy、文档、容器配置和构建。新增上游测试确认四命令冲突时均仅调用一次，记录安全错误不泄露上游细节。真实服务演示由主代理继续。
