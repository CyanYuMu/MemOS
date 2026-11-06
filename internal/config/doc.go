// Package config 把环境变量一次性解析成不可变的 Config。
// 进程内其他位置不得再调用 os.Getenv；缺少必填项或取值非法时启动即失败，而不是带着默认值静默运行。
package config
