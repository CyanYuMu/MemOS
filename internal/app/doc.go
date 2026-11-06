// Package app 是组合根（composition root）：整个项目里唯一允许同时 import adapter、service、transport 的地方。
// 负责按角色（console / mcp / all）装配依赖、启动各自独立的 http.Server、处理信号与优雅停机。业务逻辑不得写在这里。
package app
