-- 已提交批次的分块不再有任何用途。历史版本只在 pending 批次上做 TTL 清理，
-- 导致 summary 模式拒收的原始 prompt / 工具 IO 以 gzip JSONL 长期留在控制面库里。
-- 启动时清理一次；新提交由 FinishBatch 在同一事务内删除。
DELETE FROM local_sync_chunks
WHERE batch_id IN (SELECT id FROM local_sync_batches WHERE status = 'committed');
