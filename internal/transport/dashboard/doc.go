// Package dashboard 是控制台前端契约的 HTTP 适配（/api/dashboard/*、/api/meta、/api/user/*）。
// 统一信封 {code,data,message}，错误码映射，列表键保持 records / logs / memListItemResponseList / list。
// 只做编解码与鉴权绑定，业务全部委托 service。
package dashboard
