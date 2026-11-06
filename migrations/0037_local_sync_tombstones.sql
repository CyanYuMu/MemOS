-- 本地镜像的轻量墓碑：软删除行被物理清理后，用最小字段挡住同内容重传。
--
-- 背景：软删除立即释放配额，但行仍占磁盘；`MEMOSD_LOCAL_SYNC_DELETED_RETENTION`
-- 到期后物理删除。没有墓碑时，插件的 `/state` 游标会对齐到存活行 MAX，删除最新
-- 行会让客户端把游标下调并重传，已删除的记忆就被悄悄复活；解绑后从 0 全量重扫
-- 也一样。墓碑只保留 (tenant, layer, source_id) 与内容哈希：同哈希重传视为已删除，
-- 内容变化则允许作为新行重新出现。
CREATE TABLE IF NOT EXISTS local_memory_tombstones (
    tenant_id    TEXT NOT NULL,
    layer        TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    device_id    TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    deleted_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, layer, source_id)
);
CREATE INDEX IF NOT EXISTS idx_local_tombstones_deleted
    ON local_memory_tombstones(deleted_at);
