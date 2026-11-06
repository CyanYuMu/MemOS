// Package logging 构造 slog.Logger（JSON 输出）。
// 规则：INFO 及以上不得出现记忆正文、Token、Authorization 头；确需排障正文时只写 DEBUG，且默认关闭。
package logging
