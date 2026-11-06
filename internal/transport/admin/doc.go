// Package admin 是运维自动化入口（/admin/*）：租户、Token、账号与项目成员的管理。
// 凭据是管理 Token（平台管理员等价）或控制台会话 cookie，授权由 transport/auth 的 Rule 统一裁决。
package admin
