-- 内容摘要：去掉 updatedAt 的规范 JSON 哈希。
--
-- content_hash 覆盖 updatedAt，插件因 embedding 重试只刷新时间戳时，哈希会变。
-- 若物理清理后只按哈希判断，同内容行会被当成“内容变化”而复活。content_digest
-- 相同即视为同一内容：墓碑命中时不再复活，已有行命中时不重写正文、只刷新同步
-- 时间（客户端游标依赖它推进）。
ALTER TABLE local_memories ADD COLUMN content_digest TEXT NOT NULL DEFAULT '';
ALTER TABLE local_memory_tombstones ADD COLUMN content_digest TEXT NOT NULL DEFAULT '';
