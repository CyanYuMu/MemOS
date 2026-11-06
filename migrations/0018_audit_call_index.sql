-- 幂等补建 call_id 终态合并用的索引。
--
-- 0014 首次建立它；但迁移号只按整数记录，若某个库跑过同一分支 rebase 前的编号
-- （0014 曾被时间格式迁移占用、0015 曾为索引），新 0014 会被当成已应用跳过，
-- 索引就永远不会创建。0018 是新编号，所有库都会执行一次；IF NOT EXISTS 让
-- 正常库与已建过索引的库都是空操作。
CREATE INDEX IF NOT EXISTS idx_audit_call_tool ON audit_records(call_id, tool);
