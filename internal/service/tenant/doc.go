// Package tenant 是项目（租户）注册表的用例层：创建、改名、启停、归档与运维 upsert。
// 规则本身落在 domain.Tenant 上，这里负责编排与写后清缓存。
package tenant
