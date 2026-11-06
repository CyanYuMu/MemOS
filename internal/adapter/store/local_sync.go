package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

const localDeviceColumns = `id, account_id, tenant_id, device_id, label, agent_kind,
	profile_id, token_hash, status, created_at, last_seen_at, revoked_at`

const localMemoryListColumns = `m.id, m.tenant_id, m.device_id, COALESCE(d.label, ''), m.layer,
	m.source_id, m.content_hash, m.title, m.summary, m.source_updated_at, m.deleted_at,
	m.promoted_memory_id, m.promote_job_id, m.promote_status, m.promote_error, m.synced_at`

const localMemoryFrom = ` FROM local_memories m
	LEFT JOIN local_devices d ON d.tenant_id = m.tenant_id AND d.device_id = m.device_id`

// CreateDeviceCode 写入一张短时授权码。user_code 与 code_hash 都唯一。
func (s *Store) CreateDeviceCode(ctx context.Context, c domain.LocalDeviceCode) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO local_device_codes
			(code_hash, user_code, device_label, agent_kind, status, account_id, tenant_id, expires_at, created_at, decided_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		c.CodeHash[:], c.UserCode, c.DeviceLabel, c.AgentKind, string(c.Status),
		c.AccountID, c.TenantID, encodeTime(c.ExpiresAt), encodeTime(c.CreatedAt), optionalTimeText(c.DecidedAt))
	if err != nil {
		if isUniqueViolation(err) {
			return conflict("create_device_code", "授权码生成冲突，请重试", err)
		}
		return fmt.Errorf("store: create device code: %w", err)
	}
	return nil
}

// DeviceCodeByHash 按 deviceCode 的哈希读授权码。
func (s *Store) DeviceCodeByHash(ctx context.Context, hash [32]byte) (domain.LocalDeviceCode, error) {
	var (
		c                 domain.LocalDeviceCode
		storedHash        []byte
		status            string
		expires, created  string
		decided           sql.NullString
		deviceLabel, kind string
	)
	err := s.read.QueryRowContext(ctx, `
		SELECT code_hash, user_code, device_label, agent_kind, status, account_id, tenant_id,
		       expires_at, created_at, decided_at
		FROM local_device_codes WHERE code_hash = ?`, hash[:]).
		Scan(&storedHash, &c.UserCode, &deviceLabel, &kind, &status, &c.AccountID, &c.TenantID,
			&expires, &created, &decided)
	if err != nil {
		if isNotFound(err) {
			return domain.LocalDeviceCode{}, notFound("device_code", "授权码")
		}
		return domain.LocalDeviceCode{}, fmt.Errorf("store: get device code: %w", err)
	}
	copy(c.CodeHash[:], storedHash)
	c.DeviceLabel, c.AgentKind = deviceLabel, kind
	c.Status = domain.LocalCodeStatus(status)
	if c.ExpiresAt, err = decodeTime(expires); err != nil {
		return domain.LocalDeviceCode{}, fmt.Errorf("store: device code expires_at: %w", err)
	}
	if c.CreatedAt, err = decodeTime(created); err != nil {
		return domain.LocalDeviceCode{}, fmt.Errorf("store: device code created_at: %w", err)
	}
	if err = setOptionalTime(&c.DecidedAt, decided.String); err != nil {
		return domain.LocalDeviceCode{}, fmt.Errorf("store: device code decided_at: %w", err)
	}
	return c, nil
}

// DeviceCodeByUserCode 按用户可读码读授权码；授权页只有这个值。
func (s *Store) DeviceCodeByUserCode(ctx context.Context, userCode string) (domain.LocalDeviceCode, error) {
	var (
		c                domain.LocalDeviceCode
		storedHash       []byte
		status           string
		expires, created string
		decided          sql.NullString
	)
	err := s.read.QueryRowContext(ctx, `
		SELECT code_hash, user_code, device_label, agent_kind, status, account_id, tenant_id,
		       expires_at, created_at, decided_at
		FROM local_device_codes WHERE user_code = ?`, userCode).
		Scan(&storedHash, &c.UserCode, &c.DeviceLabel, &c.AgentKind, &status, &c.AccountID, &c.TenantID,
			&expires, &created, &decided)
	if err != nil {
		if isNotFound(err) {
			return domain.LocalDeviceCode{}, notFound("device_code", "授权码")
		}
		return domain.LocalDeviceCode{}, fmt.Errorf("store: get device code by user code: %w", err)
	}
	copy(c.CodeHash[:], storedHash)
	c.Status = domain.LocalCodeStatus(status)
	if c.ExpiresAt, err = decodeTime(expires); err != nil {
		return domain.LocalDeviceCode{}, fmt.Errorf("store: device code expires_at: %w", err)
	}
	if c.CreatedAt, err = decodeTime(created); err != nil {
		return domain.LocalDeviceCode{}, fmt.Errorf("store: device code created_at: %w", err)
	}
	if err = setOptionalTime(&c.DecidedAt, decided.String); err != nil {
		return domain.LocalDeviceCode{}, fmt.Errorf("store: device code decided_at: %w", err)
	}
	return c, nil
}

// ApproveDeviceCode 把 pending 授权码置为 approved，并记录授权账号与项目。
// label 为空时保留插件上报的设备名。
func (s *Store) ApproveDeviceCode(ctx context.Context, hash [32]byte, accountID, tenantID, label string, at time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE local_device_codes
		SET status = ?, account_id = ?, tenant_id = ?,
		    device_label = CASE WHEN ? <> '' THEN ? ELSE device_label END,
		    decided_at = ?
		WHERE code_hash = ? AND status = ? AND expires_at > ?`,
		string(domain.LocalCodeApproved), accountID, tenantID, label, label, encodeTime(at),
		hash[:], string(domain.LocalCodePending), encodeTime(at))
	if err != nil {
		return fmt.Errorf("store: approve device code: %w", err)
	}
	return requireAffected(res, "approve_device_code", "授权码")
}

// DenyDeviceCode 把 pending 授权码置为 denied。
func (s *Store) DenyDeviceCode(ctx context.Context, hash [32]byte, at time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE local_device_codes SET status = ?, decided_at = ?
		WHERE code_hash = ? AND status = ? AND expires_at > ?`,
		string(domain.LocalCodeDenied), encodeTime(at), hash[:],
		string(domain.LocalCodePending), encodeTime(at))
	if err != nil {
		return fmt.Errorf("store: deny device code: %w", err)
	}
	return requireAffected(res, "deny_device_code", "授权码")
}

// ConsumeDeviceCode 在一个事务里把 approved 授权码置为 consumed 并写入设备行。
// 并发轮询只有一个能成功，其余得到 ErrConflict。
func (s *Store) ConsumeDeviceCode(ctx context.Context, hash [32]byte, dev domain.LocalDevice, at time.Time) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: consume device code: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx, `
		UPDATE local_device_codes SET status = ?, decided_at = ?
		WHERE code_hash = ? AND status = ? AND expires_at > ?`,
		string(domain.LocalCodeConsumed), encodeTime(at), hash[:],
		string(domain.LocalCodeApproved), encodeTime(at))
	if err != nil {
		return fmt.Errorf("store: consume device code: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: consume device code rows: %w", err)
	}
	if n == 0 {
		return conflict("consume_device_code", "授权码已被使用或已过期", nil)
	}

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO local_devices
			(id, account_id, tenant_id, device_id, label, agent_kind, profile_id,
			 token_hash, status, created_at, last_seen_at, revoked_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)`,
		dev.ID, dev.AccountID, dev.TenantID, dev.DeviceID, dev.Label, dev.AgentKind,
		dev.ProfileID, dev.TokenHash[:], string(domain.LocalDeviceActive), encodeTime(dev.CreatedAt),
	); err != nil {
		if isUniqueViolation(err) {
			return conflict("consume_device_code", "设备令牌冲突，请重新授权", err)
		}
		return fmt.Errorf("store: create local device: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit consume device code: %w", err)
	}
	return nil
}

// DeviceByTokenHash 按令牌哈希读设备。已吊销/过期由 service 判定。
func (s *Store) DeviceByTokenHash(ctx context.Context, hash [32]byte) (domain.LocalDevice, error) {
	d, err := scanLocalDevice(s.read.QueryRowContext(ctx,
		`SELECT `+localDeviceColumns+` FROM local_devices WHERE token_hash = ?`, hash[:]))
	if err != nil {
		if isNotFound(err) {
			return domain.LocalDevice{}, notFound("local_device", "设备")
		}
		return domain.LocalDevice{}, fmt.Errorf("store: get local device: %w", err)
	}
	return d, nil
}

// TouchDevice 刷新设备的 last_seen_at；写失败不影响当前请求。
func (s *Store) TouchDevice(ctx context.Context, id string, at time.Time) error {
	if _, err := s.db.ExecContext(ctx,
		`UPDATE local_devices SET last_seen_at = ? WHERE id = ?`, encodeTime(at), id); err != nil {
		return fmt.Errorf("store: touch local device: %w", err)
	}
	return nil
}

// RevokeDevice 按项目与设备 ID 吊销；已吊销时保持原吊销时间（幂等）。
func (s *Store) RevokeDevice(ctx context.Context, tenantID, deviceID string, at time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE local_devices
		SET status = ?, revoked_at = CASE WHEN revoked_at IS NULL OR revoked_at = '' THEN ? ELSE revoked_at END
		WHERE tenant_id = ? AND (id = ? OR device_id = ?)`,
		string(domain.LocalDeviceRevoked), encodeTime(at), tenantID, deviceID, deviceID)
	if err != nil {
		return fmt.Errorf("store: revoke local device: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: revoke local device rows: %w", err)
	}
	if n == 0 {
		return notFound("revoke_local_device", "设备")
	}
	return nil
}

// ListDevices 列出项目下的全部设备（含已吊销），按创建时间倒序。
func (s *Store) ListDevices(ctx context.Context, tenantID string) ([]domain.LocalDevice, error) {
	rows, err := s.read.QueryContext(ctx,
		`SELECT `+localDeviceColumns+` FROM local_devices
		 WHERE tenant_id = ? ORDER BY created_at DESC, id DESC`, tenantID)
	if err != nil {
		return nil, fmt.Errorf("store: list local devices: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []domain.LocalDevice
	for rows.Next() {
		d, err := scanLocalDevice(rows)
		if err != nil {
			return nil, fmt.Errorf("store: scan local device: %w", err)
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

func scanLocalDevice(row scanner) (domain.LocalDevice, error) {
	var (
		d                 domain.LocalDevice
		hash              []byte
		status            string
		created           string
		lastSeen, revoked sql.NullString
	)
	if err := row.Scan(&d.ID, &d.AccountID, &d.TenantID, &d.DeviceID, &d.Label, &d.AgentKind,
		&d.ProfileID, &hash, &status, &created, &lastSeen, &revoked); err != nil {
		return domain.LocalDevice{}, err
	}
	copy(d.TokenHash[:], hash)
	d.Status = domain.LocalDeviceStatus(status)
	var err error
	if d.CreatedAt, err = decodeTime(created); err != nil {
		return domain.LocalDevice{}, fmt.Errorf("local device created_at: %w", err)
	}
	if err = setOptionalTime(&d.LastSeenAt, lastSeen.String); err != nil {
		return domain.LocalDevice{}, fmt.Errorf("local device last_seen_at: %w", err)
	}
	if err = setOptionalTime(&d.RevokedAt, revoked.String); err != nil {
		return domain.LocalDevice{}, fmt.Errorf("local device revoked_at: %w", err)
	}
	return d, nil
}

// GetMembership 读取账号在项目里的成员关系；非成员返回 ErrNotFound。
func (s *Store) GetMembership(ctx context.Context, accountID, tenantID string) (domain.Membership, error) {
	var (
		m       domain.Membership
		role    string
		created string
	)
	err := s.read.QueryRowContext(ctx, `
		SELECT account_id, tenant_id, role, created_at FROM project_members
		WHERE account_id = ? AND tenant_id = ?`, accountID, tenantID).
		Scan(&m.AccountID, &m.TenantID, &role, &created)
	if err != nil {
		if isNotFound(err) {
			return domain.Membership{}, notFound("get_membership", "成员关系")
		}
		return domain.Membership{}, fmt.Errorf("store: get membership: %w", err)
	}
	m.Role = domain.Role(role)
	if m.CreatedAt, err = decodeTime(created); err != nil {
		return domain.Membership{}, fmt.Errorf("store: membership created_at: %w", err)
	}
	return m, nil
}

// CreateBatch 写入一个待上传批次。同一 (项目, 设备, batchId) 重复返回 ErrConflict。
func (s *Store) CreateBatch(ctx context.Context, b domain.LocalSyncBatch) error {
	layers := make([]string, 0, len(b.Layers))
	for _, l := range b.Layers {
		layers = append(layers, string(l))
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO local_sync_batches
			(id, tenant_id, device_id, status, layers, manifest_json, manifest_hash, trace_mode,
			 received_bytes, accepted, rejected, error, result_json, cursor_at, created_at, committed_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, '', '', '', ?, NULL)`,
		b.ID, b.TenantID, b.DeviceID, string(domain.LocalBatchPending),
		strings.Join(layers, ","), manifestJSON(b.Manifest), b.ManifestHash,
		string(b.TraceMode), encodeTime(b.CreatedAt))
	if err != nil {
		if isUniqueViolation(err) {
			return conflict("create_batch", "该 batchId 已存在", err)
		}
		return fmt.Errorf("store: create local batch: %w", err)
	}
	return nil
}

// CreateBatchWithinQuota 在同一个写事务里完成“重复检查 + 配额预占 + 插入”。
// 分两步做（先查后插）会让并发建批次各自读到相同的 pending，行配额被
// “并发数 × 单批声明行数”短暂突破；单写连接串行化后这里不再有窗口。
func (s *Store) CreateBatchWithinQuota(
	ctx context.Context, b domain.LocalSyncBatch, quotaBytes, quotaRows int64,
) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: create local batch: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	exists, err := batchExists(ctx, tx, b.TenantID, b.DeviceID, b.ID)
	if err != nil {
		return err
	}
	if exists {
		return conflict("create_batch", "该 batchId 已存在", nil)
	}
	if err := s.batchHeadroom(ctx, tx, b, quotaBytes, quotaRows); err != nil {
		return err
	}
	if err := insertBatch(ctx, tx, b); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: create local batch commit: %w", err)
	}
	return nil
}

func batchExists(ctx context.Context, tx *sql.Tx, tenantID, deviceID, batchID string) (bool, error) {
	var n int
	if err := tx.QueryRowContext(ctx, `
		SELECT COUNT(*) FROM local_sync_batches
		WHERE tenant_id = ? AND device_id = ? AND id = ?`,
		tenantID, deviceID, batchID).Scan(&n); err != nil {
		return false, fmt.Errorf("store: create local batch lookup: %w", err)
	}
	return n > 0, nil
}

// batchHeadroom 在写事务内确认“已落库 + 其他未提交 + 本批声明”仍在配额内。
func (s *Store) batchHeadroom(ctx context.Context, tx *sql.Tx, b domain.LocalSyncBatch, quotaBytes, quotaRows int64) error {
	if quotaBytes <= 0 && quotaRows <= 0 {
		return nil
	}
	usedBytes, usedRows, err := s.tenantUsageWithin(ctx, tx, b.TenantID)
	if err != nil {
		return err
	}
	pendingBytes, pendingRows, err := pendingUsageTx(ctx, tx, b.TenantID)
	if err != nil {
		return err
	}
	var declaredRows int64
	for _, count := range b.Manifest {
		declaredRows += int64(count)
	}
	if (quotaRows > 0 && usedRows+pendingRows+declaredRows > quotaRows) ||
		(quotaBytes > 0 && usedBytes+pendingBytes > quotaBytes) {
		return domain.Wrap(domain.ErrQuotaExceeded, "create_batch",
			fmt.Sprintf("项目配额已满（%d 字节 / %d 行，含未提交批次）", quotaBytes, quotaRows), nil)
	}
	return nil
}

// pendingUsageTx 是 PendingUsage 的事务内版本：其他待提交批次的分块字节与
// manifest 声明行数。调用方持有写事务，读到的是一致快照。
func pendingUsageTx(ctx context.Context, tx *sql.Tx, tenantID string) (int64, int64, error) {
	var pendingBytes int64
	if err := tx.QueryRowContext(ctx, `
		SELECT COALESCE(SUM(LENGTH(c.body)), 0)
		FROM local_sync_chunks c JOIN local_sync_batches b ON b.id = c.batch_id
		WHERE b.tenant_id = ? AND b.status = ?`,
		tenantID, string(domain.LocalBatchPending)).Scan(&pendingBytes); err != nil {
		return 0, 0, fmt.Errorf("store: create local batch pending bytes: %w", err)
	}
	rows, err := tx.QueryContext(ctx,
		`SELECT manifest_json FROM local_sync_batches WHERE tenant_id = ? AND status = ?`,
		tenantID, string(domain.LocalBatchPending))
	if err != nil {
		return 0, 0, fmt.Errorf("store: create local batch pending manifests: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var pendingRows int64
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return 0, 0, fmt.Errorf("store: scan pending manifest: %w", err)
		}
		var manifest map[string]int
		if err := json.Unmarshal([]byte(raw), &manifest); err != nil {
			continue
		}
		for _, count := range manifest {
			pendingRows += int64(count)
		}
	}
	if err := rows.Err(); err != nil {
		return 0, 0, fmt.Errorf("store: iterate pending manifests: %w", err)
	}
	return pendingBytes, pendingRows, nil
}

func insertBatch(ctx context.Context, tx *sql.Tx, b domain.LocalSyncBatch) error {
	layers := make([]string, 0, len(b.Layers))
	for _, l := range b.Layers {
		layers = append(layers, string(l))
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO local_sync_batches
			(id, tenant_id, device_id, status, layers, manifest_json, manifest_hash, trace_mode,
			 received_bytes, accepted, rejected, error, result_json, cursor_at, created_at, committed_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, '', '', '', ?, NULL)`,
		b.ID, b.TenantID, b.DeviceID, string(domain.LocalBatchPending),
		strings.Join(layers, ","), manifestJSON(b.Manifest), b.ManifestHash,
		string(b.TraceMode), encodeTime(b.CreatedAt)); err != nil {
		if isUniqueViolation(err) {
			return conflict("create_batch", "该 batchId 已存在", err)
		}
		return fmt.Errorf("store: create local batch: %w", err)
	}
	return nil
}

// GetBatch 按项目、设备与 batchId 读批次。
func (s *Store) GetBatch(ctx context.Context, tenantID, deviceID, batchID string) (domain.LocalSyncBatch, error) {
	var (
		b              domain.LocalSyncBatch
		status, layers string
		manifestText   string
		traceMode      string
		created        string
		committed      sql.NullString
		cursor         string
	)
	err := s.read.QueryRowContext(ctx, `
		SELECT id, tenant_id, device_id, status, layers, manifest_json, manifest_hash, trace_mode,
		       received_bytes, accepted, rejected, error, result_json, cursor_at, created_at, committed_at
		FROM local_sync_batches WHERE tenant_id = ? AND device_id = ? AND id = ?`,
		tenantID, deviceID, batchID).
		Scan(&b.ID, &b.TenantID, &b.DeviceID, &status, &layers, &manifestText, &b.ManifestHash,
			&traceMode, &b.ReceivedBytes, &b.Accepted, &b.Rejected, &b.Error, &b.ResultJSON,
			&cursor, &created, &committed)
	if err != nil {
		if isNotFound(err) {
			return domain.LocalSyncBatch{}, notFound("get_batch", "批次")
		}
		return domain.LocalSyncBatch{}, fmt.Errorf("store: get local batch: %w", err)
	}
	b.Status = domain.LocalBatchStatus(status)
	b.TraceMode = domain.LocalTraceMode(traceMode)
	if manifestText != "" {
		if err := json.Unmarshal([]byte(manifestText), &b.Manifest); err != nil {
			return domain.LocalSyncBatch{}, fmt.Errorf("store: batch manifest: %w", err)
		}
	}
	for raw := range strings.SplitSeq(layers, ",") {
		if l, ok := domain.ParseLocalLayer(raw); ok {
			b.Layers = append(b.Layers, l)
		}
	}
	if b.CreatedAt, err = decodeTime(created); err != nil {
		return domain.LocalSyncBatch{}, fmt.Errorf("store: batch created_at: %w", err)
	}
	if err = setOptionalTime(&b.CommittedAt, committed.String); err != nil {
		return domain.LocalSyncBatch{}, fmt.Errorf("store: batch committed_at: %w", err)
	}
	if b.Cursor, err = decodeTime(cursor); err != nil {
		return domain.LocalSyncBatch{}, fmt.Errorf("store: batch cursor_at: %w", err)
	}
	return b, nil
}

// PutChunk 覆盖写入一个分块并更新批次累计字节。超过单批上限返回 ErrQuotaExceeded。
func (s *Store) PutChunk(ctx context.Context, tenantID, deviceID, batchID string, seq int, body []byte, maxBatchBytes, quotaBytes int64, at time.Time) (int64, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("store: put chunk: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var (
		status   string
		received int64
	)
	if err := tx.QueryRowContext(ctx, `
		SELECT status, received_bytes FROM local_sync_batches
		WHERE tenant_id = ? AND device_id = ? AND id = ?`,
		tenantID, deviceID, batchID).Scan(&status, &received); err != nil {
		if isNotFound(err) {
			return 0, notFound("put_chunk", "批次")
		}
		return 0, fmt.Errorf("store: put chunk load batch: %w", err)
	}
	if domain.LocalBatchStatus(status) != domain.LocalBatchPending {
		return 0, conflict("put_chunk", "批次已提交，不能继续上传", nil)
	}

	var previous int64
	err = tx.QueryRowContext(ctx,
		`SELECT received_bytes FROM local_sync_chunks WHERE batch_id = ? AND seq = ?`,
		batchID, seq).Scan(&previous)
	if err != nil && !isNotFound(err) {
		return 0, fmt.Errorf("store: put chunk previous: %w", err)
	}
	total := received - previous + int64(len(body))
	if maxBatchBytes > 0 && total > maxBatchBytes {
		return 0, domain.Wrap(domain.ErrQuotaExceeded, "put_chunk",
			fmt.Sprintf("单批上限 %d 字节，已收到 %d 字节", maxBatchBytes, total), nil)
	}
	if err := s.ensureChunkHeadroom(ctx, tx, tenantID, batchID, seq, int64(len(body)), quotaBytes); err != nil {
		return 0, err
	}

	if _, err := tx.ExecContext(ctx, `
		INSERT INTO local_sync_chunks (batch_id, seq, body, received_bytes, updated_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(batch_id, seq) DO UPDATE SET
			body = excluded.body, received_bytes = excluded.received_bytes, updated_at = excluded.updated_at`,
		batchID, seq, body, len(body), encodeTime(at)); err != nil {
		return 0, fmt.Errorf("store: put chunk upsert: %w", err)
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE local_sync_batches SET received_bytes = ? WHERE id = ?`, total, batchID); err != nil {
		return 0, fmt.Errorf("store: put chunk update batch: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("store: put chunk commit: %w", err)
	}
	return total, nil
}

// ensureChunkHeadroom 在写入事务里确认“已落库 + 其他未提交批次 + 本批其他分块 +
// 本次分块”不超过项目字节配额。未提交分块同样占磁盘，不统计就能靠反复建批次写满库。
func (s *Store) ensureChunkHeadroom(
	ctx context.Context, tx *sql.Tx, tenantID, batchID string, seq int, incoming, quotaBytes int64,
) error {
	if quotaBytes <= 0 {
		return nil
	}
	used, _, err := s.tenantUsageWithin(ctx, tx, tenantID)
	if err != nil {
		return err
	}
	var otherPending, sameBatch int64
	if err := tx.QueryRowContext(ctx, `
		SELECT COALESCE(SUM(LENGTH(c.body)), 0)
		FROM local_sync_chunks c JOIN local_sync_batches b ON b.id = c.batch_id
		WHERE b.tenant_id = ? AND b.status = ? AND c.batch_id <> ?`,
		tenantID, string(domain.LocalBatchPending), batchID).Scan(&otherPending); err != nil {
		return fmt.Errorf("store: put chunk quota pending: %w", err)
	}
	if err := tx.QueryRowContext(ctx,
		`SELECT COALESCE(SUM(LENGTH(body)), 0) FROM local_sync_chunks WHERE batch_id = ? AND seq <> ?`,
		batchID, seq).Scan(&sameBatch); err != nil {
		return fmt.Errorf("store: put chunk quota batch: %w", err)
	}
	if used+otherPending+sameBatch+incoming > quotaBytes {
		return domain.Wrap(domain.ErrQuotaExceeded, "put_chunk",
			fmt.Sprintf("项目配额 %d 字节已满（含未提交分块）", quotaBytes), nil)
	}
	return nil
}

// ListChunkSeqs 返回批次的分块序号，升序。
func (s *Store) ListChunkSeqs(ctx context.Context, tenantID, deviceID, batchID string) ([]int, error) {
	rows, err := s.read.QueryContext(ctx, `
		SELECT c.seq FROM local_sync_chunks c
		JOIN local_sync_batches b ON b.id = c.batch_id
		WHERE b.tenant_id = ? AND b.device_id = ? AND b.id = ?
		ORDER BY c.seq ASC`, tenantID, deviceID, batchID)
	if err != nil {
		return nil, fmt.Errorf("store: list chunk seqs: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var out []int
	for rows.Next() {
		var seq int
		if err := rows.Scan(&seq); err != nil {
			return nil, fmt.Errorf("store: scan chunk seq: %w", err)
		}
		out = append(out, seq)
	}
	return out, rows.Err()
}

// GetChunk 读一个分块的压缩正文。
func (s *Store) GetChunk(ctx context.Context, tenantID, deviceID, batchID string, seq int) ([]byte, error) {
	var body []byte
	err := s.read.QueryRowContext(ctx, `
		SELECT c.body FROM local_sync_chunks c
		JOIN local_sync_batches b ON b.id = c.batch_id
		WHERE b.tenant_id = ? AND b.device_id = ? AND b.id = ? AND c.seq = ?`,
		tenantID, deviceID, batchID, seq).Scan(&body)
	if err != nil {
		if isNotFound(err) {
			return nil, notFound("get_chunk", "分块")
		}
		return nil, fmt.Errorf("store: get chunk: %w", err)
	}
	return body, nil
}

// FinishBatch 把 pending 批次置为 committed、保存首次结果，并删除已提交批次的分块。
//
// 分块里是客户端上传的原始 JSONL（summary 模式下被拒的行也还在里面）。批次提交后
// 它们不再有用途，留在控制面库里会让“拒绝原始正文”只对 local_memories 生效，
// 备份与运维仍能看到原文。清理与状态变更放在同一个事务里：要么都生效，要么都回滚。
func (s *Store) FinishBatch(ctx context.Context, tenantID, deviceID, batchID string, result domain.LocalCommitResult, at time.Time) error {
	body, err := marshalJSON(result)
	if err != nil {
		return fmt.Errorf("store: encode commit result: %w", err)
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: finish local batch: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	res, err := tx.ExecContext(ctx, `
		UPDATE local_sync_batches
		SET status = ?, accepted = ?, rejected = ?, result_json = ?, cursor_at = ?, committed_at = ?
		WHERE tenant_id = ? AND device_id = ? AND id = ? AND status = ?`,
		string(domain.LocalBatchCommitted), result.Accepted, result.Rejected, string(body),
		encodeTime(result.Cursor), encodeTime(at), tenantID, deviceID, batchID,
		string(domain.LocalBatchPending))
	if err != nil {
		return fmt.Errorf("store: finish local batch: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: finish local batch rows: %w", err)
	}
	if n == 0 {
		return conflict("finish_batch", "批次已提交", nil)
	}
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM local_sync_chunks WHERE batch_id = ?`, batchID); err != nil {
		return fmt.Errorf("store: drop committed chunks: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit finished batch: %w", err)
	}
	return nil
}

// PurgeBatchesBefore 删除早于 before 的未提交批次，分块随外键级联删除。
func (s *Store) PurgeBatchesBefore(ctx context.Context, before time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM local_sync_batches WHERE status = ? AND created_at < ?`,
		string(domain.LocalBatchPending), encodeTime(before))
	if err != nil {
		return 0, fmt.Errorf("store: purge local batches: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: purge local batches rows: %w", err)
	}
	return n, nil
}

// SummarizeMemories 汇总一个项目的本地镜像：分层计数、转存状态与同步时间范围。
// 只看控制面副本；软删除行计入 Deleted，不计入 Layers/Total。
func (s *Store) SummarizeMemories(ctx context.Context, tenantID string) (domain.LocalMemorySummary, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT layer,
		       COUNT(*),
		       SUM(CASE WHEN deleted_at IS NULL OR deleted_at = '' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN (deleted_at IS NULL OR deleted_at = '')
		                     AND promote_status = 'succeeded' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN (deleted_at IS NULL OR deleted_at = '')
		                     AND promote_status IN ('queued', 'running') THEN 1 ELSE 0 END),
		       SUM(CASE WHEN (deleted_at IS NULL OR deleted_at = '')
		                     AND promote_status = 'failed' THEN 1 ELSE 0 END),
		       MIN(synced_at), MAX(synced_at)
		FROM local_memories
		WHERE tenant_id = ?
		GROUP BY layer`, tenantID)
	if err != nil {
		return domain.LocalMemorySummary{}, fmt.Errorf("store: summarize local memories: %w", err)
	}
	defer func() { _ = rows.Close() }()

	summary := domain.LocalMemorySummary{TenantID: tenantID, Layers: make(map[string]int64)}
	var oldest, newest string
	for rows.Next() {
		var (
			layer                     string
			total, live               int64
			promoted, pending, failed int64
			minSynced, maxSynced      string
		)
		if err := rows.Scan(&layer, &total, &live, &promoted, &pending, &failed, &minSynced, &maxSynced); err != nil {
			return domain.LocalMemorySummary{}, fmt.Errorf("store: scan local summary: %w", err)
		}
		summary.Layers[layer] = live
		summary.Total += live
		summary.Deleted += total - live
		summary.Promoted += promoted
		summary.PromotePending += pending
		summary.PromoteFailed += failed
		if minSynced != "" && (oldest == "" || minSynced < oldest) {
			oldest = minSynced
		}
		if maxSynced != "" && maxSynced > newest {
			newest = maxSynced
		}
	}
	if err := rows.Err(); err != nil {
		return domain.LocalMemorySummary{}, fmt.Errorf("store: iterate local summary: %w", err)
	}
	if oldest != "" {
		at, err := decodeTime(oldest)
		if err != nil {
			return domain.LocalMemorySummary{}, fmt.Errorf("store: decode oldest synced_at: %w", err)
		}
		summary.OldestSyncedAt = &at
	}
	if newest != "" {
		at, err := decodeTime(newest)
		if err != nil {
			return domain.LocalMemorySummary{}, fmt.Errorf("store: decode newest synced_at: %w", err)
		}
		summary.NewestSyncedAt = &at
	}
	quota, err := s.Quota(ctx, tenantID)
	if err != nil {
		return domain.LocalMemorySummary{}, err
	}
	summary.Quota = quota
	return summary, nil
}

// 清理所用的四条语句。墓碑谓词与删除谓词可以不同：镜像保留期只给“已被用户
// 删除”的行留墓碑，而删除本身要覆盖所有超期行。
const (
	tombstoneDeletedSQL = `
		INSERT INTO local_memory_tombstones
			(tenant_id, layer, source_id, device_id, content_hash, content_digest, deleted_at)
		SELECT tenant_id, layer, source_id, device_id, content_hash, content_digest, ?
		FROM local_memories
		WHERE deleted_at IS NOT NULL AND deleted_at <> '' AND deleted_at < ?
		  AND (promote_status IS NULL OR promote_status NOT IN (?, ?))
		ON CONFLICT(tenant_id, layer, source_id) DO UPDATE SET
			device_id = excluded.device_id,
			content_hash = excluded.content_hash,
			content_digest = excluded.content_digest,
			deleted_at = excluded.deleted_at
		WHERE excluded.deleted_at >= local_memory_tombstones.deleted_at`
	deleteDeletedSQL = `
		DELETE FROM local_memories
		WHERE deleted_at IS NOT NULL AND deleted_at <> '' AND deleted_at < ?
		  AND (promote_status IS NULL OR promote_status NOT IN (?, ?))`
	tombstoneExpiredMirrorsSQL = `
		INSERT INTO local_memory_tombstones
			(tenant_id, layer, source_id, device_id, content_hash, content_digest, deleted_at)
		SELECT tenant_id, layer, source_id, device_id, content_hash, content_digest, ?
		FROM local_memories
		WHERE synced_at < ?
		  AND deleted_at IS NOT NULL AND deleted_at <> ''
		  AND (promote_status IS NULL OR promote_status NOT IN (?, ?))
		ON CONFLICT(tenant_id, layer, source_id) DO UPDATE SET
			device_id = excluded.device_id,
			content_hash = excluded.content_hash,
			content_digest = excluded.content_digest,
			deleted_at = excluded.deleted_at
		WHERE excluded.deleted_at >= local_memory_tombstones.deleted_at`
	deleteExpiredMirrorsSQL = `
		DELETE FROM local_memories
		WHERE synced_at < ?
		  AND (promote_status IS NULL OR promote_status NOT IN (?, ?))`
)

// PurgeMirroredBefore 删除同步时间早于 before 的镜像行（含已软删除的行）。
// 只动控制面副本；本地插件与云端记忆不受影响。按全局时间清理，不区分项目。
func (s *Store) PurgeMirroredBefore(ctx context.Context, before time.Time) (int64, error) {
	return s.purgeMirrors(ctx, before, tombstoneExpiredMirrorsSQL, deleteExpiredMirrorsSQL)
}

// PurgeDeletedBefore 物理删除软删除时间早于 before 的镜像行。软删除立即释放
// 配额，但行仍占磁盘；不做这一步，“删除→再上传”可以无限撑大共享库。只删控制面
// 副本；正在转存的行已软删除时也等作业结束（queued/running 的 deleted_at 通常
// 在作业完成后才写，这里仍按 deleted_at 判断，避免删掉作业回写目标）。
func (s *Store) PurgeDeletedBefore(ctx context.Context, before time.Time) (int64, error) {
	return s.purgeMirrors(ctx, before, tombstoneDeletedSQL, deleteDeletedSQL)
}

// purgeMirrors 在同一事务里先给命中行写墓碑再删除。墓碑必须与删除原子完成：
// 先删后写墓碑的窗口里，同内容重传会作为新行复活已删除的记忆。
//
// 仍在排队/执行中的转存行不能先删：作业完成时会更新 0 行，云端却已经写入，
// 留下没有本地来源的记忆。等作业结束后下一轮再按保留期回收。
func (s *Store) purgeMirrors(ctx context.Context, before time.Time, tombstoneSQL, deleteSQL string) (int64, error) {
	cutoff := encodeTime(before)
	// 墓碑时间戳取“物理清理发生的时刻”，不是用户原始删除时间：清理与墓碑清理
	// 是两条独立配置，若按原始删除时间算，删除保留期较长时新建的墓碑会立刻
	// 超过墓碑保留期，在下一轮维护被清掉，已删除内容就能被重传复活。
	purgedAt := encodeTime(time.Now().UTC())
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, fmt.Errorf("store: purge local mirrors: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx, tombstoneSQL,
		purgedAt, cutoff, domain.LocalPromoteQueued, domain.LocalPromoteRunning); err != nil {
		return 0, fmt.Errorf("store: record local tombstones: %w", err)
	}
	res, err := tx.ExecContext(ctx, deleteSQL,
		cutoff, domain.LocalPromoteQueued, domain.LocalPromoteRunning)
	if err != nil {
		return 0, fmt.Errorf("store: purge local mirrors: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: purge local mirrors rows: %w", err)
	}
	if err := tx.Commit(); err != nil {
		return 0, fmt.Errorf("store: purge local mirrors commit: %w", err)
	}
	s.invalidateUsage("")
	return n, nil
}

// PurgeTombstonesBefore 清理超过保留期的墓碑。墓碑按内容无限增长会拖慢每次
// 上传的判定；保留期需覆盖“解绑后全量重扫”的窗口，默认 180 天。
func (s *Store) PurgeTombstonesBefore(ctx context.Context, before time.Time) (int64, error) {
	res, err := s.db.ExecContext(ctx,
		`DELETE FROM local_memory_tombstones WHERE deleted_at < ?`, encodeTime(before))
	if err != nil {
		return 0, fmt.Errorf("store: purge local tombstones: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("store: purge local tombstones rows: %w", err)
	}
	return n, nil
}

// ApplyRows 在一个事务里应用一批已校验的行，并在写入过程中检查项目配额。
// 内容未变返回 unchanged；新行或内容变化返回 applied。超配额返回 ErrQuotaExceeded，
// 整批回滚。
func (s *Store) ApplyRows(ctx context.Context, tenantID, deviceID string, rows []domain.LocalRow, limits domain.LocalQuota) (int64, int64, error) {
	if len(rows) == 0 {
		return 0, 0, nil
	}
	if limits.LimitBytes <= 0 {
		limits.LimitBytes = domain.LocalQuotaBytesLimit
	}
	if limits.LimitRows <= 0 {
		limits.LimitRows = domain.LocalRowCountLimit
	}

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, 0, fmt.Errorf("store: apply local rows: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var usedBytes, usedRows int64
	if err := tx.QueryRowContext(ctx, `
		SELECT COALESCE(SUM(LENGTH(payload)), 0), COUNT(*)
		FROM local_memories
		WHERE tenant_id = ? AND (deleted_at IS NULL OR deleted_at = '')`, tenantID).
		Scan(&usedBytes, &usedRows); err != nil {
		return 0, 0, fmt.Errorf("store: local quota: %w", err)
	}

	var applied, unchanged int64
	for i := range rows {
		outcome, err := s.applyRow(ctx, tx, tenantID, deviceID, &rows[i], &usedBytes, &usedRows, limits)
		if err != nil {
			return 0, 0, err
		}
		if outcome == rowUnchanged {
			unchanged++
			continue
		}
		applied++
	}
	if err := tx.Commit(); err != nil {
		return 0, 0, fmt.Errorf("store: commit local rows: %w", err)
	}
	s.invalidateUsage(tenantID)
	return applied, unchanged, nil
}

type localRowOutcome int

const (
	rowApplied localRowOutcome = iota
	rowUnchanged
)

// applyRow 在调用方的事务里插入或更新一行，并把配额用量累加到 usedBytes/usedRows。
func (s *Store) applyRow(
	ctx context.Context, tx *sql.Tx, tenantID, deviceID string, row *domain.LocalRow,
	usedBytes, usedRows *int64, limits domain.LocalQuota,
) (localRowOutcome, error) {
	blocked, tombstoned, err := localTombstone(ctx, tx, tenantID, row)
	if err != nil {
		return 0, err
	}
	if blocked {
		return rowUnchanged, nil
	}

	var existing existingLocalRow
	err = tx.QueryRowContext(ctx, `
		SELECT id, content_hash, content_digest, LENGTH(payload), deleted_at
		FROM local_memories
		WHERE tenant_id = ? AND device_id = ? AND layer = ? AND source_id = ?`,
		tenantID, deviceID, string(row.Layer), row.SourceID).
		Scan(&existing.id, &existing.hash, &existing.digest, &existing.length, &existing.deletedAt)
	switch {
	case err == nil:
		return mergeLocalRow(ctx, tx, tenantID, row, &existing, usedBytes, usedRows, limits, tombstoned)
	case isNotFound(err):
		return rowApplied, insertLocalRow(ctx, tx, tenantID, deviceID, row,
			usedBytes, usedRows, limits, tombstoned)
	default:
		return 0, fmt.Errorf("store: lookup local memory: %w", err)
	}
}

type existingLocalRow struct {
	id        string
	hash      string
	digest    string
	length    int64
	deletedAt sql.NullString
}

// mergeLocalRow 处理同一 (device, layer, source_id) 已存在的行：内容未变只可能在
// 时间戳变化时推进游标列；内容变化则按差额计入配额后整行更新。
func mergeLocalRow(
	ctx context.Context, tx *sql.Tx, tenantID string, row *domain.LocalRow,
	existing *existingLocalRow, usedBytes, usedRows *int64, limits domain.LocalQuota, tombstoned bool,
) (localRowOutcome, error) {
	if sameLocalContent(existing.hash, existing.digest, row) {
		// 内容未变：不重写正文。软删除的副本同样不会因重复上传复活；只有时间戳
		// 变化时下面才会刷新 source_updated_at/synced_at（后者会推迟保留期清理）。
		// 只有时间戳变了（embedding 重试会刷新 updated_at）时仍要推进 source_updated_at：
		// 服务端 /state 游标取自该列，不更新会让客户端每轮重发同一行。
		if row.ContentHash != existing.hash {
			if err := refreshLocalRowTimestamp(ctx, tx, existing.id, row.SourceUpdatedAt); err != nil {
				return 0, err
			}
		}
		return rowUnchanged, nil
	}

	deltaBytes, deltaRows := int64(len(row.PayloadGzip))-existing.length, int64(0)
	// 软删除的行不计入配额；同一 source_id 再次上传会把它复活。此时旧长度本来就
	// 不在 usedBytes 里，只能加新长度，否则会出现负用量并绕过字节配额。
	if existing.deletedAt.Valid && existing.deletedAt.String != "" {
		deltaBytes, deltaRows = int64(len(row.PayloadGzip)), 1
	}
	if err := reserveLocalQuota(usedBytes, usedRows, deltaBytes, deltaRows, limits,
		fmt.Sprintf("项目存储配额 %d 字节已满", limits.LimitBytes)); err != nil {
		return 0, err
	}
	if _, err := tx.ExecContext(ctx, `
		UPDATE local_memories
		SET content_hash = ?, content_digest = ?, title = ?, summary = ?, payload = ?,
		    source_updated_at = ?, deleted_at = NULL, promoted_memory_id = '',
		    promote_job_id = '', promote_status = '', promote_error = '', synced_at = ?
		WHERE id = ?`,
		row.ContentHash, row.ContentDigest, row.Title, row.Summary, row.PayloadGzip,
		encodeTime(row.SourceUpdatedAt), encodeTime(time.Now()), existing.id); err != nil {
		return 0, fmt.Errorf("store: update local memory: %w", err)
	}
	if err := dropLocalTombstone(ctx, tx, tenantID, row, tombstoned); err != nil {
		return 0, err
	}
	return rowApplied, nil
}

// insertLocalRow 写入新行；tombstoned 表示存在旧内容的墓碑，写入成功后删除。
func insertLocalRow(
	ctx context.Context, tx *sql.Tx, tenantID, deviceID string, row *domain.LocalRow,
	usedBytes, usedRows *int64, limits domain.LocalQuota, tombstoned bool,
) error {
	if err := reserveLocalQuota(usedBytes, usedRows, int64(len(row.PayloadGzip)), 1, limits,
		fmt.Sprintf("项目配额已满（%d 字节 / %d 行）", limits.LimitBytes, limits.LimitRows)); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO local_memories
			(id, tenant_id, device_id, layer, source_id, content_hash, content_digest,
			 title, summary, payload, source_updated_at, deleted_at, promoted_memory_id,
			 promote_job_id, promote_status, promote_error, synced_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', '', '', '', ?)`,
		domain.NewID(), tenantID, deviceID, string(row.Layer), row.SourceID, row.ContentHash,
		row.ContentDigest, row.Title, row.Summary, row.PayloadGzip,
		encodeTime(row.SourceUpdatedAt), encodeTime(time.Now())); err != nil {
		return fmt.Errorf("store: insert local memory: %w", err)
	}
	return dropLocalTombstone(ctx, tx, tenantID, row, tombstoned)
}

// reserveLocalQuota 累加用量并在越限时返回配额错误（调用方事务会整体回滚）。
func reserveLocalQuota(
	usedBytes, usedRows *int64, deltaBytes, deltaRows int64, limits domain.LocalQuota, message string,
) error {
	*usedBytes += deltaBytes
	*usedRows += deltaRows
	if *usedBytes > limits.LimitBytes || *usedRows > limits.LimitRows {
		return domain.Wrap(domain.ErrQuotaExceeded, "apply_local_rows", message, nil)
	}
	return nil
}

// localTombstone 查墓碑。blocked 表示同内容已被用户删除（不得复活）；
// present 表示存在内容不同的旧墓碑（真正写入新内容后删除）。墓碑按
// (tenant, layer, source_id) 而不是设备：删除镜像后再解绑重绑（设备 ID 变了）
// 或从 0 全量重扫时，同一内容也不该以“新设备”的身份重新出现。
func localTombstone(ctx context.Context, tx *sql.Tx, tenantID string, row *domain.LocalRow) (blocked, present bool, err error) {
	var hash, digest string
	switch err := tx.QueryRowContext(ctx, `
		SELECT content_hash, content_digest FROM local_memory_tombstones
		WHERE tenant_id = ? AND layer = ? AND source_id = ?`,
		tenantID, string(row.Layer), row.SourceID).Scan(&hash, &digest); {
	case err == nil && sameLocalContent(hash, digest, row):
		return true, false, nil
	case err == nil:
		// 内容变了：允许作为新行重新出现。墓碑要等真正写入后再删——若既有行与
		// 本次内容一致（未变化路径），提前删掉会让旧内容在之后再重传时复活。
		return false, true, nil
	case isNotFound(err):
		return false, false, nil
	default:
		return false, false, fmt.Errorf("store: load local tombstone: %w", err)
	}
}

// refreshLocalRowTimestamp 只在内容未变、时间戳变化时推进游标列；不重写正文。
func refreshLocalRowTimestamp(ctx context.Context, tx *sql.Tx, id string, updatedAt time.Time) error {
	if _, err := tx.ExecContext(ctx, `
		UPDATE local_memories SET source_updated_at = ?, synced_at = ? WHERE id = ?`,
		encodeTime(updatedAt), encodeTime(time.Now()), id); err != nil {
		return fmt.Errorf("store: refresh local memory: %w", err)
	}
	return nil
}

// sameLocalContent 判断上传行与库里记录是否同一内容。两个 digest 都有时优先
// 比较 digest：它不包含 updatedAt，embedding 重试刷新时间戳不会让内容“变新”。
func sameLocalContent(hash, digest string, row *domain.LocalRow) bool {
	if digest != "" && row.ContentDigest != "" {
		return digest == row.ContentDigest
	}
	return hash == row.ContentHash
}

// dropLocalTombstone 只在真正写入行时删除墓碑；未变化路径保留墓碑。
func dropLocalTombstone(ctx context.Context, tx *sql.Tx, tenantID string, row *domain.LocalRow, tombstoned bool) error {
	if !tombstoned {
		return nil
	}
	if _, err := tx.ExecContext(ctx, `
		DELETE FROM local_memory_tombstones
		WHERE tenant_id = ? AND layer = ? AND source_id = ?`,
		tenantID, string(row.Layer), row.SourceID); err != nil {
		return fmt.Errorf("store: drop local tombstone: %w", err)
	}
	return nil
}

// LocalState 返回设备的上次提交、游标与项目配额。
func (s *Store) LocalState(ctx context.Context, tenantID, deviceID string) (domain.LocalState, error) {
	state := domain.LocalState{TenantID: tenantID, DeviceID: deviceID}
	var lastBatch sql.NullString
	err := s.read.QueryRowContext(ctx, `
		SELECT id FROM local_sync_batches
		WHERE tenant_id = ? AND device_id = ? AND status = ?
		ORDER BY committed_at DESC, id DESC LIMIT 1`,
		tenantID, deviceID, string(domain.LocalBatchCommitted)).Scan(&lastBatch)
	if err != nil && !isNotFound(err) {
		return domain.LocalState{}, fmt.Errorf("store: local state batch: %w", err)
	}
	state.LastBatchID = lastBatch.String

	var cursor sql.NullString
	if err := s.read.QueryRowContext(ctx, `
		SELECT MAX(source_updated_at) FROM local_memories
		WHERE tenant_id = ? AND device_id = ? AND (deleted_at IS NULL OR deleted_at = '')`,
		tenantID, deviceID).Scan(&cursor); err != nil {
		return domain.LocalState{}, fmt.Errorf("store: local state cursor: %w", err)
	}
	if cursor.Valid && cursor.String != "" {
		t, err := decodeTime(cursor.String)
		if err != nil {
			return domain.LocalState{}, fmt.Errorf("store: local state cursor time: %w", err)
		}
		state.LastAcceptedAt = &t
	}
	quota, err := s.Quota(ctx, tenantID)
	if err != nil {
		return domain.LocalState{}, err
	}
	state.Quota = quota
	return state, nil
}

// Quota 返回项目的已用镜像字节与行数（不含软删除行）。
func (s *Store) Quota(ctx context.Context, tenantID string) (domain.LocalQuota, error) {
	bytes, rows, err := s.tenantUsage(ctx, tenantID)
	if err != nil {
		return domain.LocalQuota{}, err
	}
	return domain.LocalQuota{UsedBytes: bytes, UsedRows: rows}, nil
}

type usageEntry struct {
	bytes int64
	rows  int64
	at    time.Time
}

// usageTTL 是已用量的缓存时间。分块写入的准入检查不必精确到毫秒：真正的最终
// 裁决在 ApplyRows 事务里，且所有改动存活行的路径都会立即失效缓存。
const usageTTL = 10 * time.Second

// tenantUsage 返回项目已落库的存活字节与行数，短时缓存避免每个分块都全表扫描。
func (s *Store) tenantUsage(ctx context.Context, tenantID string) (int64, int64, error) {
	if bytes, rows, ok := s.cachedUsage(tenantID); ok {
		return bytes, rows, nil
	}
	gen := s.usageGeneration()
	bytes, rows, err := queryUsage(ctx, s.read, tenantID)
	if err != nil {
		return 0, 0, err
	}
	s.storeUsage(tenantID, bytes, rows, gen)
	return bytes, rows, nil
}

// tenantUsageWithin 是写事务内版本：缓存未命中时用当前事务的读快照，不再从
// 连接池借第二条连接。SQLite 单写连接（以及 `:memory:` 的单连接）下，写事务里
// 再去池里取连接会自锁。
func (s *Store) tenantUsageWithin(ctx context.Context, tx *sql.Tx, tenantID string) (int64, int64, error) {
	if bytes, rows, ok := s.cachedUsage(tenantID); ok {
		return bytes, rows, nil
	}
	gen := s.usageGeneration()
	bytes, rows, err := queryUsage(ctx, tx, tenantID)
	if err != nil {
		return 0, 0, err
	}
	s.storeUsage(tenantID, bytes, rows, gen)
	return bytes, rows, nil
}

// rowQuerier 同时由 *sql.DB 与 *sql.Tx 满足。
type rowQuerier interface {
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

func queryUsage(ctx context.Context, q rowQuerier, tenantID string) (int64, int64, error) {
	var bytes, rows int64
	if err := q.QueryRowContext(ctx, `
		SELECT COALESCE(SUM(LENGTH(payload)), 0), COUNT(*)
		FROM local_memories
		WHERE tenant_id = ? AND (deleted_at IS NULL OR deleted_at = '')`, tenantID).
		Scan(&bytes, &rows); err != nil {
		return 0, 0, fmt.Errorf("store: local quota: %w", err)
	}
	return bytes, rows, nil
}

func (s *Store) cachedUsage(tenantID string) (int64, int64, bool) {
	s.usageMu.Lock()
	defer s.usageMu.Unlock()
	entry, ok := s.usageCache[tenantID]
	if !ok || time.Since(entry.at) >= usageTTL {
		return 0, 0, false
	}
	return entry.bytes, entry.rows, true
}

func (s *Store) usageGeneration() uint64 {
	s.usageMu.Lock()
	defer s.usageMu.Unlock()
	return s.usageGen
}

func (s *Store) storeUsage(tenantID string, bytes, rows int64, gen uint64) {
	s.usageMu.Lock()
	defer s.usageMu.Unlock()
	if s.usageGen != gen {
		return
	}
	if s.usageCache == nil {
		s.usageCache = make(map[string]usageEntry)
	}
	s.usageCache[tenantID] = usageEntry{bytes: bytes, rows: rows, at: time.Now()}
}

// invalidateUsage 在存活行变化后调用；tenantID 为空表示清空全部（清理任务）。
func (s *Store) invalidateUsage(tenantID string) {
	s.usageMu.Lock()
	defer s.usageMu.Unlock()
	s.usageGen++
	if tenantID == "" || s.usageCache == nil {
		s.usageCache = make(map[string]usageEntry)
		return
	}
	delete(s.usageCache, tenantID)
}

// PendingUsage 返回项目内待提交批次占用的分块字节与 manifest 声明行数。
// 这些数据还没进 local_memories，但已经真实占用磁盘，配额要一起算。
func (s *Store) PendingUsage(ctx context.Context, tenantID string) (int64, int64, error) {
	var pendingBytes int64
	if err := s.read.QueryRowContext(ctx, `
		SELECT COALESCE(SUM(LENGTH(c.body)), 0)
		FROM local_sync_chunks c JOIN local_sync_batches b ON b.id = c.batch_id
		WHERE b.tenant_id = ? AND b.status = ?`,
		tenantID, string(domain.LocalBatchPending)).Scan(&pendingBytes); err != nil {
		return 0, 0, fmt.Errorf("store: pending usage bytes: %w", err)
	}
	rows, err := s.read.QueryContext(ctx,
		`SELECT manifest_json FROM local_sync_batches WHERE tenant_id = ? AND status = ?`,
		tenantID, string(domain.LocalBatchPending))
	if err != nil {
		return 0, 0, fmt.Errorf("store: pending usage manifests: %w", err)
	}
	defer func() { _ = rows.Close() }()
	var pendingRows int64
	for rows.Next() {
		var raw string
		if err := rows.Scan(&raw); err != nil {
			return 0, 0, fmt.Errorf("store: scan manifest: %w", err)
		}
		var manifest map[string]int
		if err := json.Unmarshal([]byte(raw), &manifest); err != nil {
			continue // 内部写出的 JSON 解析失败只影响行数保守估计，不阻塞上传。
		}
		for _, count := range manifest {
			pendingRows += int64(count)
		}
	}
	if err := rows.Err(); err != nil {
		return 0, 0, fmt.Errorf("store: pending usage rows: %w", err)
	}
	return pendingBytes, pendingRows, nil
}

// ListMemories 返回一页镜像摘要与过滤后的总数，不读取 payload。
func (s *Store) ListMemories(ctx context.Context, f domain.LocalMemoryFilter) ([]domain.LocalMemory, int, error) {
	where := `m.tenant_id = ? AND (m.deleted_at IS NULL OR m.deleted_at = '')`
	args := []any{f.TenantID}
	if f.Layer != "" {
		where += ` AND m.layer = ?`
		args = append(args, f.Layer)
	}
	if f.DeviceID != "" {
		where += ` AND m.device_id = ?`
		args = append(args, f.DeviceID)
	}
	if !f.StartTime.IsZero() {
		where += ` AND m.source_updated_at >= ?`
		args = append(args, encodeTime(f.StartTime))
	}
	if !f.EndTime.IsZero() {
		where += ` AND m.source_updated_at <= ?`
		args = append(args, encodeTime(f.EndTime))
	}
	if q := strings.TrimSpace(f.Query); q != "" {
		escaper := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
		pattern := "%" + escaper.Replace(q) + "%"
		where += ` AND (m.title LIKE ? ESCAPE '\' OR m.summary LIKE ? ESCAPE '\' OR m.source_id LIKE ? ESCAPE '\')`
		args = append(args, pattern, pattern, pattern)
	}
	page, size := f.Page, f.PageSize
	if page < 1 {
		page = 1
	}
	if size < 1 {
		size = 20
	}
	size = min(size, 200)
	offset := (page - 1) * size

	tx, err := s.read.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, 0, fmt.Errorf("store: begin local memories page: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	var total int
	if err := tx.QueryRowContext(ctx,
		`SELECT COUNT(*)`+localMemoryFrom+` WHERE `+where, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("store: count local memories: %w", err)
	}

	pageArgs := append(append([]any(nil), args...), size, offset)
	rows, err := tx.QueryContext(ctx,
		`SELECT `+localMemoryListColumns+localMemoryFrom+` WHERE `+where+
			` ORDER BY m.source_updated_at DESC, m.id DESC LIMIT ? OFFSET ?`, pageArgs...)
	if err != nil {
		return nil, 0, fmt.Errorf("store: list local memories: %w", err)
	}
	defer func() { _ = rows.Close() }()

	out := make([]domain.LocalMemory, 0, size)
	for rows.Next() {
		m, err := scanLocalMemory(rows, false)
		if err != nil {
			return nil, 0, fmt.Errorf("store: scan local memory: %w", err)
		}
		out = append(out, m)
	}
	rowErr := rows.Err()
	if err := rows.Close(); err != nil {
		return nil, 0, fmt.Errorf("store: close local memories: %w", err)
	}
	if rowErr != nil {
		return nil, 0, fmt.Errorf("store: list local memories rows: %w", rowErr)
	}
	if err := tx.Commit(); err != nil {
		return nil, 0, fmt.Errorf("store: commit local memories page: %w", err)
	}
	return out, total, nil
}

// GetMemory 读单条镜像（含 gzip payload）。已删除返回 ErrNotFound。
func (s *Store) GetMemory(ctx context.Context, tenantID, id string) (domain.LocalMemory, error) {
	m, err := scanLocalMemory(s.read.QueryRowContext(ctx,
		`SELECT `+localMemoryListColumns+`, m.payload`+localMemoryFrom+`
		 WHERE m.tenant_id = ? AND m.id = ? AND (m.deleted_at IS NULL OR m.deleted_at = '')`, tenantID, id), true)
	if err != nil {
		if isNotFound(err) {
			return domain.LocalMemory{}, notFound("get_local_memory", "本地记忆")
		}
		return domain.LocalMemory{}, fmt.Errorf("store: get local memory: %w", err)
	}
	return m, nil
}

func scanLocalMemory(row scanner, withPayload bool) (domain.LocalMemory, error) {
	var (
		m                      domain.LocalMemory
		layer, updated, synced string
		deleted                sql.NullString
		payload                []byte
	)
	dest := []any{
		&m.ID, &m.TenantID, &m.DeviceID, &m.DeviceLabel, &layer, &m.SourceID, &m.ContentHash,
		&m.Title, &m.Summary, &updated, &deleted, &m.PromotedMemoryID, &m.PromoteJobID,
		&m.PromoteStatus, &m.PromoteError, &synced,
	}
	if withPayload {
		dest = append(dest, &payload)
	}
	if err := row.Scan(dest...); err != nil {
		return domain.LocalMemory{}, err
	}
	m.Layer = domain.LocalLayer(layer)
	m.Payload = payload
	var err error
	if m.SourceUpdatedAt, err = decodeTime(updated); err != nil {
		return domain.LocalMemory{}, fmt.Errorf("local memory source_updated_at: %w", err)
	}
	if err = setOptionalTime(&m.DeletedAt, deleted.String); err != nil {
		return domain.LocalMemory{}, fmt.Errorf("local memory deleted_at: %w", err)
	}
	if m.SyncedAt, err = decodeTime(synced); err != nil {
		return domain.LocalMemory{}, fmt.Errorf("local memory synced_at: %w", err)
	}
	return m, nil
}

// DeleteMemory 软删除镜像副本；已删除时保持幂等。不传播到本地。
func (s *Store) DeleteMemory(ctx context.Context, tenantID, id string, at time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE local_memories SET deleted_at = ?
		WHERE tenant_id = ? AND id = ? AND (deleted_at IS NULL OR deleted_at = '')`,
		encodeTime(at), tenantID, id)
	if err != nil {
		return fmt.Errorf("store: delete local memory: %w", err)
	}
	s.invalidateUsage(tenantID)
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: delete local memory rows: %w", err)
	}
	if n > 0 {
		return nil
	}
	var exists int
	if err := s.read.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM local_memories WHERE tenant_id = ? AND id = ?`, tenantID, id).
		Scan(&exists); err != nil {
		return fmt.Errorf("store: check local memory: %w", err)
	}
	if exists == 0 {
		return notFound("delete_local_memory", "本地记忆")
	}
	return nil
}

// MarkPromoteQueued 把镜像行标记为已入队。已处于 queued/running/succeeded 时不覆盖。
func (s *Store) MarkPromoteQueued(ctx context.Context, tenantID, id, jobID string, _ time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE local_memories
		SET promote_status = ?, promote_job_id = ?, promote_error = '', promoted_memory_id = ''
		WHERE tenant_id = ? AND id = ? AND (deleted_at IS NULL OR deleted_at = '')
		  AND promote_status NOT IN (?, ?, ?)`,
		domain.LocalPromoteQueued, jobID, tenantID, id,
		domain.LocalPromoteQueued, domain.LocalPromoteRunning, domain.LocalPromoteSucceeded)
	if err != nil {
		return fmt.Errorf("store: mark promote queued: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: mark promote queued rows: %w", err)
	}
	if n == 0 {
		return conflict("mark_promote_queued", "该记忆已在转存中或已转存", nil)
	}
	return nil
}

// MarkPromoteResult 由转存作业终态写回；只认当前 promote_job_id，旧作业不能覆盖新状态。
// 成功时**不**把作业 ID 写进 promoted_memory_id：上游 add 不返回记忆 ID，写作业 ID
// 会让控制台把一次作业当成“云端记忆 ID”。该列只在拿到真实上游记忆 ID 时写入，
// 作业本身仍可通过 promote_job_id 追溯。
func (s *Store) MarkPromoteResult(ctx context.Context, tenantID, id, status, jobID, message string, _ time.Time) error {
	res, err := s.db.ExecContext(ctx, `
		UPDATE local_memories
		SET promote_status = ?, promote_error = ?
		WHERE tenant_id = ? AND id = ? AND promote_job_id = ?`,
		status, message, tenantID, id, jobID)
	if err != nil {
		return fmt.Errorf("store: mark promote result: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: mark promote result rows: %w", err)
	}
	if n == 0 {
		// 作业已被更新的转存请求取代，或行已被删除：不是错误。
		return nil
	}
	return nil
}

// marshalJSON 序列化 commit 结果。结果只在本进程与重放响应里使用，
// 标准库默认的 HTML 转义不改变解码后的值。
func marshalJSON(v any) ([]byte, error) { return json.Marshal(v) }

// manifestJSON 以稳定形式保存每层声明行数；键排序由 encoding/json 保证。
func manifestJSON(manifest map[string]int) string {
	if len(manifest) == 0 {
		return "{}"
	}
	body, err := json.Marshal(manifest)
	if err != nil {
		return "{}"
	}
	return string(body)
}
