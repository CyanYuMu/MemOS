-- 调用入口。tool 回答“调了什么”，source 回答“从哪进来的”：同一个 add_message 既可能
-- 来自 MCP 工具调用（异步入队、deferred=true），也可能来自记忆插件的 /memory/add 门面
-- （同步等上游改写、一条常在数秒）。只看工具名时两类流量混成一份，出不了“谁在压上游”
-- 的账。由各入口 transport 写入，service/worker 不推断。
-- 历史行（0023 之前）与异步 add 的终态行为空：前者没这个信息，后者与请求行同 call_id、
-- 来源以请求行为准，重复记反而要保证两行永远一致。
ALTER TABLE audit_records ADD COLUMN source TEXT NOT NULL DEFAULT '';
