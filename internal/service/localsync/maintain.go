package localsync

import (
	"context"
	"log/slog"
	"time"
)

// MaintainLocalSync 回收超期数据，由 worker 的小时级清理循环调用。
//
// 三件事：
//  1. 未提交批次按 BatchTTL 清理（原有语义，避免分块长期占库）。
//  2. 软删除超过 DeletedRetention 的行做物理删除。软删除已释放配额，但行仍占
//     磁盘；“删除→再上传”若不清理能无限撑大共享库。
//  3. 配置了 MirrorRetention 时，删除 synced_at 早于保留期的镜像行。只删控制面
//     副本，不触及本地插件与云端记忆；默认 0 表示永久保留。
//  4. 删除超过 TombstoneRetention 的墓碑（默认 180 天）。墓碑挡住同内容重传，
//     但无限增长会拖慢每次上传判定。
func (s *Service) MaintainLocalSync(ctx context.Context) error {
	now := s.now()

	if purged, err := s.store.PurgeBatchesBefore(ctx, now.Add(-s.opts.BatchTTL)); err != nil {
		return err
	} else if purged > 0 {
		s.log.InfoContext(ctx, "purged expired local sync batches", slog.Int64("count", purged))
	}

	if s.opts.DeletedRetention > 0 {
		cutoff := now.Add(-s.opts.DeletedRetention)
		purged, err := s.store.PurgeDeletedBefore(ctx, cutoff)
		if err != nil {
			return err
		}
		if purged > 0 {
			s.log.InfoContext(ctx, "purged soft-deleted local sync mirrors",
				slog.Int64("count", purged),
				slog.String("before", cutoff.UTC().Format(time.RFC3339)))
		}
	}

	if s.opts.TombstoneRetention > 0 {
		cutoff := now.Add(-s.opts.TombstoneRetention)
		purged, err := s.store.PurgeTombstonesBefore(ctx, cutoff)
		if err != nil {
			return err
		}
		if purged > 0 {
			s.log.InfoContext(ctx, "purged expired local sync tombstones",
				slog.Int64("count", purged),
				slog.String("before", cutoff.UTC().Format(time.RFC3339)))
		}
	}

	if s.opts.MirrorRetention <= 0 {
		return nil
	}
	before := now.Add(-s.opts.MirrorRetention)
	purged, err := s.store.PurgeMirroredBefore(ctx, before)
	if err != nil {
		return err
	}
	if purged == 0 {
		return nil
	}
	s.log.InfoContext(ctx, "purged expired local sync mirrors",
		slog.Int64("count", purged), slog.String("before", before.UTC().Format(time.RFC3339)))
	s.record(ctx, auditRecord("local_sync.memory.retention", "", "", map[string]any{
		"purged": purged,
		"before": before.UTC().Format(time.RFC3339),
		"days":   s.opts.MirrorRetention.Hours() / 24,
	}, nil))
	return nil
}
