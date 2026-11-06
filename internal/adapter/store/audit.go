package store

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/hugegraph/MemOS/apps/memory-console-be/internal/domain"
)

// InsertAudit 写入一条审计记录。
func (s *Store) InsertAudit(ctx context.Context, r domain.AuditRecord) error {
	return s.InsertAudits(ctx, []domain.AuditRecord{r})
}

// auditInsertColumns 是插入列清单，顺序必须与 auditArgs 的返回值一一对应。
const auditInsertColumns = `id, call_id, ts, tool, source, tenant_id, user_id, session_id,
	case_like, deferred, ms, ok, err, err_message, http_status, result_bytes, input_json,
	output, query, request_id, remote_ip, request_tokens, response_tokens, llm_calls,
	client_version, host_version, agent_identity, agent_context, turn_number`

// InsertAudits 在同一个事务里写入一批审计记录。审计是逐条产生的，批量提交能把
// N 次 fsync 降成一次，避免 flush 长时间占住唯一的写连接。
func (s *Store) InsertAudits(ctx context.Context, records []domain.AuditRecord) error {
	if len(records) == 0 {
		return nil
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("store: begin audit batch: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	placeholders := strings.TrimSuffix(strings.Repeat("?,", auditInsertColumnCount()), ",")
	//nolint:gosec // 拼接的只有固定列名与占位符，没有任何外部输入
	stmt, err := tx.PrepareContext(ctx, `
		INSERT INTO audit_records (`+auditInsertColumns+`)
		VALUES (`+placeholders+`)`)
	if err != nil {
		return fmt.Errorf("store: prepare audit insert: %w", err)
	}
	defer func() { _ = stmt.Close() }()

	for i := range records {
		args, err := auditArgs(records[i])
		if err != nil {
			return err
		}
		if _, err := stmt.ExecContext(ctx, args...); err != nil {
			return fmt.Errorf("store: insert audit: %w", err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("store: commit audit batch: %w", err)
	}
	return nil
}

// auditInsertColumnCount 返回插入列数。
func auditInsertColumnCount() int {
	return strings.Count(auditInsertColumns, ",") + 1
}

func auditArgs(r domain.AuditRecord) ([]any, error) {
	if r.ID == "" {
		r.ID = domain.NewID()
	}
	if r.TS.IsZero() {
		r.TS = time.Now().UTC()
	}
	input := "{}"
	if len(r.Input) > 0 {
		// 不做 HTML 转义：json.Marshal 会把 & < > 写成 6 字节的 Unicode 转义序列，
		// 极端输入下体积膨胀约 6 倍，会让 service 侧的 256 KiB 估算失效。
		// 存储的仍是合法 JSON，读取方不需要 HTML 安全。
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(r.Input); err != nil {
			return nil, fmt.Errorf("store: marshal audit input: %w", err)
		}
		input = strings.TrimSuffix(buf.String(), "\n")
	}
	var caseLike any
	if r.CaseLike != nil {
		caseLike = *r.CaseLike
	}
	return []any{
		r.ID, r.CallID, encodeTime(r.TS), r.Tool, r.Source, r.TenantID, r.UserID, r.SessionID,
		caseLike, r.Deferred, r.Ms, r.OK, r.Err, r.ErrMessage, r.HTTPStatus, r.ResultBytes, input, r.Output, r.Query,
		r.RequestID, r.RemoteIP, r.RequestTokens, r.ResponseTokens, r.LLMCalls,
		r.ClientVersion, r.HostVersion, r.AgentIdentity, r.AgentContext, r.TurnNumber,
	}, nil
}

// auditExistsBatch 是 AuditIDsExist 单条查询的参数上限。SQLite 的变量上限
// 远大于 500，但分批能让一个超大导入文件也不会构造出超长 SQL。
const auditExistsBatch = 500

// AuditIDsExist 返回给定 ID 中已经存在的集合，供一次性导入做幂等去重。
func (s *Store) AuditIDsExist(ctx context.Context, ids []string) (map[string]struct{}, error) {
	out := make(map[string]struct{}, len(ids))
	for start := 0; start < len(ids); start += auditExistsBatch {
		chunk := ids[start:min(start+auditExistsBatch, len(ids))]
		found, err := s.auditIDsChunk(ctx, chunk)
		if err != nil {
			return nil, err
		}
		for _, id := range found {
			out[id] = struct{}{}
		}
	}
	return out, nil
}

func (s *Store) auditIDsChunk(ctx context.Context, ids []string) ([]string, error) {
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	//nolint:gosec // 拼接的只是 ? 占位符，所有 ID 都走参数
	query := `SELECT id FROM audit_records WHERE id IN (` + strings.TrimSuffix(strings.Repeat("?,", len(ids)), ",") + `)`
	rows, err := s.read.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("store: check audit ids: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var found []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("store: scan audit id: %w", err)
		}
		found = append(found, id)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("store: check audit ids: %w", err)
	}
	return found, nil
}

const auditColumns = `id, call_id, ts, tool, source, tenant_id, user_id, session_id, case_like, deferred,
		ms, ok, err, err_message, http_status, result_bytes, input_json, output, query, request_id, remote_ip,
		request_tokens, response_tokens, llm_calls, client_version, host_version, agent_identity,
		agent_context, turn_number`

// auditListColumns 只取元数据，input/output/query 用空串占位：审计正文虽已在
// prepare 阶段按 256 KiB 截断，单条仍可达数百 KB，列表一次读 500 条会把整个
// 响应体拉进内存，而列表只用元数据。详情走 GetAudit。
const auditListColumns = `id, call_id, ts, tool, source, tenant_id, user_id, session_id, case_like, deferred,
		ms, ok, err, err_message, http_status, result_bytes, '' AS input_json, '' AS output, '' AS query, request_id, remote_ip,
		request_tokens, response_tokens, llm_calls, client_version, host_version, agent_identity,
		agent_context, turn_number`

// auditStatusExpr 是控制台逻辑调用的派生状态：非 deferred 行按自身 ok；deferred
// 行优先取同一 call_id 的最新一条后台终态行。没有终态行时，只有同一 call_id 的
// job 仍在 jobs 表里才算 RUNNING——job 按保留期（默认 7 天）清理后，请求行回退到
// 自身 ok；升级前落库的行 call_id 与 job.ID 对不上（两次 NewID），同样直接按行内
// ok，不会在审计保留期内一直显示 RUNNING。只用了 CASE / COALESCE / EXISTS /
// 标量子查询，SQLite 与 PG 语义一致；call_id 为空的历史行不参与合并。状态过滤、
// 计数与分页都复用这个表达式，全部在库里完成。
const auditStatusExpr = `CASE
		WHEN NOT audit_records.deferred THEN CASE WHEN audit_records.ok THEN 'SUCCESS' ELSE 'FAILED' END
		ELSE COALESCE((
			SELECT CASE WHEN t.ok THEN 'SUCCESS' ELSE 'FAILED' END
			  FROM audit_records t
			 WHERE t.tool = '` + domain.ToolAddMessageBackground + `'
			   AND audit_records.call_id <> ''
			   AND t.call_id = audit_records.call_id
			 ORDER BY t.ts DESC, t.id DESC
			 LIMIT 1
		), CASE
			WHEN audit_records.call_id <> ''
			 AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = audit_records.call_id)
			THEN 'RUNNING'
			ELSE CASE WHEN audit_records.ok THEN 'SUCCESS' ELSE 'FAILED' END
		END)
	END`

const auditStatusColumn = `, ` + auditStatusExpr + ` AS status`

// ListAudit 按过滤条件分页返回审计记录（ts DESC, id DESC）与总数。
//
// 索引：租户内查询走 idx_audit_tenant_ts_id，带工具过滤时走 idx_audit_tenant_tool_ts；
// 平台管理员跨项目只看某个 tool 时没有租户条件，走 idx_audit_tool_ts_id；
// 不限定项目的全局翻页走 idx_audit_ts_id。三条 (…, ts, id) 索引都是覆盖索引，
// 分页的排序不再需要临时 B-tree。
func (s *Store) ListAudit(ctx context.Context, f domain.AuditFilter) ([]domain.AuditRecord, int, error) {
	where, args := auditWhere(f)

	limit := f.Limit
	if limit <= 0 {
		limit = 100
	}
	if limit > 500 {
		limit = 500
	}
	offset := max(f.Offset, 0)

	// count 与分页查询互不依赖，并行发出；两者各占一条读连接。
	type countResult struct {
		n   int
		err error
	}
	countCh := make(chan countResult, 1)
	go func() {
		var n int
		err := s.read.QueryRowContext(ctx, `SELECT COUNT(1) FROM audit_records`+where, args...).Scan(&n)
		countCh <- countResult{n: n, err: err}
	}()

	//nolint:gosec // where 只由固定条件片段拼接，所有取值都走占位符
	query := `SELECT ` + auditListColumns + auditStatusColumn + ` FROM audit_records` + where + ` ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?`
	rows, err := s.read.QueryContext(ctx, query, append(slices.Clone(args), limit, offset)...)
	if err != nil {
		return nil, 0, fmt.Errorf("store: list audit: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []domain.AuditRecord
	for rows.Next() {
		rec, err := scanAudit(rows)
		if err != nil {
			return nil, 0, fmt.Errorf("store: scan audit: %w", err)
		}
		out = append(out, rec)
	}
	count := <-countCh
	if count.err != nil {
		return nil, 0, fmt.Errorf("store: count audit: %w", count.err)
	}
	return out, count.n, rows.Err()
}

// purgeAuditBatchSize 控制单条 DELETE 删除的行数。一次删几十万行会形成长事务，
// WAL 也会在提交前持续膨胀；分批后每批独立提交，读请求能在批次之间推进。
const purgeAuditBatchSize = 5000

// PurgeAuditBefore 分批删除早于 before 的审计记录，返回删除总条数。
// ctx 取消后立即停止，返回已删除的数量与 ctx 错误。
func (s *Store) PurgeAuditBefore(ctx context.Context, before time.Time) (int64, error) {
	return s.purgeAuditBefore(ctx, before, purgeAuditBatchSize)
}

func (s *Store) purgeAuditBefore(ctx context.Context, before time.Time, batchSize int) (int64, error) {
	if batchSize <= 0 {
		batchSize = purgeAuditBatchSize
	}
	cutoff := encodeTime(before)
	var total int64
	for {
		if err := ctx.Err(); err != nil {
			return total, fmt.Errorf("store: purge audit: %w", err)
		}
		res, err := s.db.ExecContext(ctx, `
			DELETE FROM audit_records WHERE id IN (
				SELECT id FROM audit_records WHERE ts < ? ORDER BY ts LIMIT ?
			)`, cutoff, batchSize)
		if err != nil {
			return total, fmt.Errorf("store: purge audit: %w", err)
		}
		n, err := res.RowsAffected()
		if err != nil {
			return total, fmt.Errorf("store: purge audit rows: %w", err)
		}
		total += n
		if n == 0 {
			return total, nil
		}
		// PASSIVE 不阻塞读写；失败只说明这一轮没回收完，不影响已提交的删除。
		_, _ = s.db.ExecContext(ctx, `PRAGMA wal_checkpoint(PASSIVE)`)
		if n < int64(batchSize) {
			return total, nil
		}
	}
}

// Optimize 在批量清理后刷新 SQLite 的查询规划统计。与删除分成两步，
// 清理循环可以在整轮删除结束后只调用一次，而不是每批都做一次分析。
func (s *Store) Optimize(ctx context.Context) error {
	if _, err := s.db.ExecContext(ctx, `PRAGMA optimize`); err != nil {
		return fmt.Errorf("store: optimize: %w", err)
	}
	return nil
}

// GetAudit 按 ID 取一条审计记录。异步 add 的请求行带派生状态；token 用量始终
// 取同一 call_id 的终态行，进入终态失败时错误与响应正文也改用终态行，
// 入参与查询词仍是请求行的原值。
func (s *Store) GetAudit(ctx context.Context, id string) (domain.AuditRecord, error) {
	row := s.read.QueryRowContext(ctx, `SELECT `+auditColumns+auditStatusColumn+` FROM audit_records WHERE id = ?`, id)
	rec, err := scanAudit(row)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.AuditRecord{}, domain.NotFound("get_audit", "审计记录不存在")
	}
	if err != nil {
		return domain.AuditRecord{}, fmt.Errorf("store: get audit: %w", err)
	}
	if rec.Deferred {
		var (
			terminalErr  string
			terminalMsg  string
			terminalHTTP int
			output       string
			reqTokens    int
			respTokens   int
			llmCalls     int
		)
		err := s.read.QueryRowContext(ctx, `SELECT err, err_message, http_status, output,
				request_tokens, response_tokens, llm_calls FROM audit_records t
			WHERE t.tool = ? AND t.call_id = ? ORDER BY t.ts DESC, t.id DESC LIMIT 1`,
			domain.ToolAddMessageBackground, rec.CallID).Scan(&terminalErr, &terminalMsg, &terminalHTTP, &output,
			&reqTokens, &respTokens, &llmCalls)
		switch {
		case err == nil:
			rec.RequestTokens = reqTokens
			rec.ResponseTokens = respTokens
			rec.LLMCalls = llmCalls
			if rec.Status != domain.AuditStatusFailed {
				break
			}
			rec.Err = terminalErr
			rec.ErrMessage = terminalMsg
			if terminalHTTP > 0 {
				rec.HTTPStatus = terminalHTTP
			}
			if output != "" {
				rec.Output = output
			}
		case !errors.Is(err, sql.ErrNoRows):
			return domain.AuditRecord{}, fmt.Errorf("store: get audit terminal row: %w", err)
		}
	}
	return rec, nil
}

// AuditHourCounts 按 UTC 整点与工具聚合调用次数与 token 用量。ts 是 RFC3339 文本，
// 前 13 个字符就是整点，取子串在 SQLite 与 PG 上都可用，不依赖方言的日期函数。
func (s *Store) AuditHourCounts(ctx context.Context, f domain.AuditFilter) ([]domain.AuditHourCount, error) {
	where, args := auditWhere(f)
	//nolint:gosec // where 只由固定条件片段拼接，所有取值都走占位符
	rows, err := s.read.QueryContext(ctx, `SELECT substr(ts, 1, 13) AS bucket, tool, COUNT(1),
			COALESCE(SUM(request_tokens), 0), COALESCE(SUM(response_tokens), 0), COALESCE(SUM(llm_calls), 0)
		FROM audit_records`+where+` GROUP BY bucket, tool`, args...)
	if err != nil {
		return nil, fmt.Errorf("store: audit hour counts: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []domain.AuditHourCount
	for rows.Next() {
		var (
			bucket string
			item   domain.AuditHourCount
		)
		if err := rows.Scan(&bucket, &item.Tool, &item.Calls,
			&item.RequestTokens, &item.ResponseTokens, &item.LLMCalls); err != nil {
			return nil, fmt.Errorf("store: scan audit hour counts: %w", err)
		}
		hour, err := time.Parse("2006-01-02T15", bucket)
		if err != nil {
			// 历史脏时间不应让整张用量图 500；无法归桶的行跳过。
			continue
		}
		item.Hour = hour.UTC()
		out = append(out, item)
	}
	return out, rows.Err()
}

func auditWhere(f domain.AuditFilter) (string, []any) {
	var (
		clauses []string
		args    []any
	)
	if f.TenantID != "" {
		clauses = append(clauses, "tenant_id = ?")
		args = append(args, f.TenantID)
	}
	if f.CallID != "" {
		clauses = append(clauses, "call_id = ?")
		args = append(args, f.CallID)
	}
	if f.Tool != "" {
		clauses = append(clauses, "tool = ?")
		args = append(args, f.Tool)
	}
	if len(f.Tools) > 0 {
		clauses = append(clauses, "tool IN ("+strings.TrimSuffix(strings.Repeat("?,", len(f.Tools)), ",")+")")
		for _, tool := range f.Tools {
			args = append(args, tool)
		}
	}
	if len(f.ExcludeTools) > 0 {
		clauses = append(clauses, "tool NOT IN ("+strings.TrimSuffix(strings.Repeat("?,", len(f.ExcludeTools)), ",")+")")
		for _, tool := range f.ExcludeTools {
			args = append(args, tool)
		}
	}
	// 历史行与异步终态行的 source 为空，按来源筛选时自然被排除——不清楚来源的行
	// 不该被算进某条入口的账里。
	if len(f.Sources) > 0 {
		clauses = append(clauses, "source IN ("+strings.TrimSuffix(strings.Repeat("?,", len(f.Sources)), ",")+")")
		for _, src := range f.Sources {
			args = append(args, src)
		}
	}
	if f.OK != nil {
		clauses = append(clauses, "ok = ?")
		args = append(args, *f.OK)
	}
	if f.Status != "" {
		clauses = append(clauses, "("+auditStatusExpr+") = ?")
		args = append(args, string(f.Status))
	}
	if f.Text != "" {
		clause, textArgs := auditTextClause(f.Text)
		clauses = append(clauses, clause)
		args = append(args, textArgs...)
	}
	if !f.Since.IsZero() {
		clauses = append(clauses, "ts >= ?")
		args = append(args, encodeTime(f.Since))
	}
	if !f.Until.IsZero() {
		clauses = append(clauses, "ts < ?")
		args = append(args, encodeTime(f.Until))
	}
	if len(clauses) == 0 {
		return "", nil
	}
	return " WHERE " + strings.Join(clauses, " AND "), args
}

var auditTextColumns = []string{"input_json", "output", "query", "err", "err_message", "session_id", "tenant_id"}

// auditTextClause 在若干文本列里做不区分大小写的子串匹配；通配符按字面处理。
// 异步 add 的错误与正文只存在终态行里，详情会合并它们，搜索也必须能命中，
// 否则同一个调用在列表里搜不到、点进详情却能看到。
func auditTextClause(text string) (string, []any) {
	escaper := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
	pattern := "%" + escaper.Replace(strings.ToLower(text)) + "%"
	parts := make([]string, 0, len(auditTextColumns)+1)
	args := make([]any, 0, len(auditTextColumns)+3)
	for _, column := range auditTextColumns {
		parts = append(parts, "lower("+column+") LIKE ? ESCAPE '\\'")
		args = append(args, pattern)
	}
	parts = append(parts, `EXISTS (SELECT 1 FROM audit_records t
			WHERE t.tool = ? AND audit_records.call_id <> ''
			  AND t.call_id = audit_records.call_id
			  AND (lower(t.err) LIKE ? ESCAPE '\' OR lower(t.output) LIKE ? ESCAPE '\'))`)
	args = append(args, domain.ToolAddMessageBackground, pattern, pattern)
	return "(" + strings.Join(parts, " OR ") + ")", args
}

func scanAudit(row scanner) (domain.AuditRecord, error) {
	var (
		rec       domain.AuditRecord
		ts        string
		caseLike  *bool
		inputJSON string
	)
	if err := row.Scan(&rec.ID, &rec.CallID, &ts, &rec.Tool, &rec.Source, &rec.TenantID, &rec.UserID,
		&rec.SessionID, &caseLike, &rec.Deferred, &rec.Ms, &rec.OK, &rec.Err,
		&rec.ErrMessage, &rec.HTTPStatus, &rec.ResultBytes, &inputJSON, &rec.Output, &rec.Query,
		&rec.RequestID, &rec.RemoteIP, &rec.RequestTokens, &rec.ResponseTokens, &rec.LLMCalls,
		&rec.ClientVersion, &rec.HostVersion, &rec.AgentIdentity, &rec.AgentContext, &rec.TurnNumber,
		&rec.Status); err != nil {
		return domain.AuditRecord{}, err
	}
	parsed, err := decodeTime(ts)
	if err != nil {
		return domain.AuditRecord{}, err
	}
	rec.TS = parsed
	rec.CaseLike = caseLike
	if inputJSON != "" {
		if err := json.Unmarshal([]byte(inputJSON), &rec.Input); err != nil {
			rec.Input = nil
		}
	}
	return rec, nil
}
