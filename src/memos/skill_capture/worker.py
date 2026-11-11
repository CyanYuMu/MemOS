"""Archive observed turns and reconcile durable Memory delivery without replaying POSTs."""

from __future__ import annotations

import json
import time

from .core import NAMESPACE, canonical, digest


def archive(store, *, idle_seconds=300):
    count = 0
    now = time.time()
    with store.db() as db:
        groups = db.execute(
            "SELECT session, turn, MAX(received) AS updated FROM events GROUP BY session, turn"
        ).fetchall()
    for group in groups:
        with store.db() as db:
            session = db.execute(
                "SELECT * FROM sessions WHERE id=?", (group["session"],)
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM events WHERE session=? AND turn=? ORDER BY seq",
                (group["session"], group["turn"]),
            ).fetchall()
            previous = db.execute(
                "SELECT id FROM experiences WHERE session_id=? AND turn=? ORDER BY created DESC LIMIT 1",
                (group["session"], group["turn"]),
            ).fetchone()
        events = [
            {
                "id": r["id"],
                "kind": r["kind"],
                "received": r["received"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]
        meaningful = [e for e in events if e["kind"] not in {"SessionStart", "SessionEnd"}]
        if not meaningful:
            continue
        closed = [e["kind"] for e in events if e["kind"] in {"Stop", "StopFailure", "SessionEnd"}]
        if now - group["updated"] < (
            store.config.get("settleSeconds", 5) if closed else idle_seconds
        ):
            continue
        issues = []
        if not any(e["kind"] == "UserPromptSubmit" for e in events):
            issues.append("prompt_missing")
        if not closed:
            issues.append("turn_not_closed")
        if not any(e["kind"] == "Stop" for e in events):
            issues.append("final_response_missing")
        if any(e["kind"] == "StopFailure" for e in events):
            issues.append("agent_api_failure")
        started = {e["payload"].get("tool_use_id") for e in events if e["kind"] == "PreToolUse"}
        ended = {
            e["payload"].get("tool_use_id")
            for e in events
            if e["kind"] in {"PostToolUse", "PostToolUseFailure"}
        }
        if started - ended:
            issues.append("tool_result_missing")
        if ended - started:
            issues.append("tool_start_missing")
        if "truncated:" in canonical(events):
            issues.append("content_truncated")
        evidence = {
            "schema": "claude-daily-observation-1",
            "project": session["project"],
            "session": session["native_id"],
            "turn": group["turn"],
            "qualification": "observed_unverified",
            "task_success": None,
            "independent_validation": False,
            "closed_by": closed[-1] if closed else None,
            "issues": issues,
            "events": events,
        }
        identifier = digest(evidence)
        evidence["supersedes"] = (
            previous["id"] if previous and previous["id"] != identifier else None
        )
        lines = [
            f"Claude Code 日常任务观察：{session['project']}。记录 {identifier}。",
            "资格：observed_unverified；以下均为会话观察或 Agent 自报，未经独立验收，不构成可发布 Skill 或成功修复依据。",
            "完整性问题："
            + (", ".join(issues) or "未发现已支持事件的配对缺失；未采集中间推理或全部子 Agent。"),
        ]
        for e in meaningful:
            p = e["payload"]
            if e["kind"] == "UserPromptSubmit":
                lines.append("用户请求：" + str(p.get("prompt", ""))[:1500])
            elif e["kind"] == "Stop":
                lines.append(
                    "Agent 最终回复（自报）：" + str(p.get("last_assistant_message", ""))[:1500]
                )
            elif e["kind"] == "StopFailure":
                lines.append("Agent/API 失败：" + canonical(p)[:1000])
            elif e["kind"] in {"PostToolUse", "PostToolUseFailure"}:
                lines.append("工具观察：" + canonical(p)[:700])
        summary = "\n".join(lines)
        if len(summary) > 7000:
            summary = summary[:7000] + "\n[摘要截断；完整的已采集证据保存在本机账本]"
        with store.db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO experiences (id, session_id, project, turn, created, evidence, summary, status, reason, memory_ids, readback) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '[]', '[]')",
                (
                    identifier,
                    group["session"],
                    session["project"],
                    group["turn"],
                    now,
                    canonical(evidence),
                    summary,
                ),
            )
            count += cursor.rowcount
    return count


def _info(store, item):
    memory = store.config["memory"]
    return {
        "capture_namespace": NAMESPACE,
        "capture_operation_id": item["id"],
        "capture_evidence_sha256": digest(item["evidence"]),
        "capture_summary_sha256": digest(item["summary"]),
        "capture_project": item["project"],
        "capture_session": item["session_id"],
        "capture_owner": memory["userId"],
        "capture_cube": memory["cubeId"],
        "capture_qualification": "observed_unverified",
    }


def _found(response, cube):
    groups = response.get("data", {}).get("text_mem")
    if not isinstance(groups, list):
        raise ValueError("recovery_shape_invalid")
    records = []
    for group in groups:
        if group.get("cube_id") != cube or group.get("total_nodes") != len(
            group.get("memories", [])
        ):
            raise ValueError("recovery_scope_invalid")
        records.extend(group["memories"])
    return records


def _validate(record, info, summary, user, cube):
    if not isinstance(record, dict) or not record.get("id") or not record.get("memory"):
        raise ValueError("readback_missing")
    meta = record.get("metadata", {})
    if any(meta.get(k) != v for k, v in {**info, "user_id": user}.items()):
        raise ValueError("readback_provenance_mismatch")
    if any(
        k in meta and meta[k] != v
        for k, v in (("cube_id", cube), ("owner_id", user), ("owner_type", "user"))
    ):
        raise ValueError("readback_scope_mismatch")
    sources = meta.get("sources", [])
    fragments = [s.get("content", "") for s in sources if isinstance(s, dict)]
    if not fragments or any(
        not f or f not in summary or (f != summary and f not in record["memory"]) for f in fragments
    ):
        raise ValueError("readback_source_mismatch")
    return fragments


def sync_one(store, identifier, client):
    with store.db() as db:
        raw = db.execute("SELECT * FROM experiences WHERE id=?", (identifier,)).fetchone()
    if raw is None or raw["status"] in {"synced", "invalid"}:
        return
    item = store.decode(raw)
    memory = store.config["memory"]
    cube, user = memory["cubeId"], memory["userId"]
    info = _info(store, item)
    status, reason = "pending_reconcile", "memory_outcome_unknown"
    ids = item["memory_ids"]
    readback = []
    claimed = False
    try:
        # A read preflight leaves definitely-unsent items pending while offline.
        records = _found(client.find(cube, user, {**info, "user_id": user}), cube)
        if item["status"] == "pending" and not records:
            with store.db() as db:
                changed = db.execute(
                    "UPDATE experiences SET status='sending' WHERE id=? AND status='pending'",
                    (identifier,),
                ).rowcount
            if not changed:
                return
            claimed = True
            payload = {
                "user_id": user,
                "writable_cube_ids": [cube],
                "session_id": item["session_id"],
                "task_id": identifier,
                "async_mode": "sync",
                "mode": "fast",
                "allow_memory_view": ["detail_factual"],
                "messages": [{"role": "user", "content": item["summary"]}],
                "info": info,
            }
            response = client.add(payload)
            data = response.get("data")
            if not isinstance(data, list) or not data:
                raise OSError("add_not_confirmed")
            if any(x.get("cube_id") != cube or not x.get("memory_id") for x in data):
                raise ValueError("add_scope_mismatch")
            ids = [x["memory_id"] for x in data]
            with store.db() as db:
                db.execute(
                    "UPDATE experiences SET memory_ids=? WHERE id=?", (canonical(ids), identifier)
                )
        elif records:
            ids = [r["id"] for r in records]
        if not ids:
            raise OSError("pending_reconcile")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_memory_ids")
        fragments = []
        for memory_id in ids:
            record = client.get(memory_id, cube).get("data")
            if not record or record.get("id") != memory_id:
                raise ValueError("readback_id_mismatch")
            fragments.extend(_validate(record, info, item["summary"], user, cube))
            # Only observed public fields are retained; never retain full HTTP headers.
            # Fast Memory may keep FULL original sources on PARTIAL body chunks.
            # Confirm source identity, without claiming that derived body text is an exact copy.
            readback.append(
                {
                    "id": memory_id,
                    "memory": record["memory"],
                    "metadata": {
                        **info,
                        "user_id": user,
                        "sources": [
                            {"content": s["content"]} for s in record["metadata"]["sources"]
                        ],
                    },
                    "verification": "source_identity_only",
                }
            )
        covered = [False] * len(item["summary"])
        for fragment in fragments:
            pos = item["summary"].find(fragment)
            while pos >= 0:
                covered[pos : pos + len(fragment)] = [True] * len(fragment)
                pos = item["summary"].find(fragment, pos + 1)
        if not all(covered):
            raise ValueError("readback_content_incomplete")
        status, reason = "synced", ""
    except ValueError as error:
        status, reason = "invalid", str(error)
    except Exception as error:
        # Never print remote exception text; it may contain private configuration.
        if item["status"] == "pending" and not claimed:
            status, reason = "pending", "memory_unavailable_before_send"
        if type(error).__name__ == "MemoryClientError":
            reason += ":" + str(error)
    with store.db() as db:
        db.execute(
            "INSERT INTO delivery_observations VALUES (NULL, ?, ?, ?, ?)",
            (identifier, time.time(), status, reason),
        )
        db.execute(
            "UPDATE experiences SET status=?, reason=?, memory_ids=?, readback=?, attempts=attempts+1, next_attempt=? WHERE id=?",
            (status, reason, canonical(ids), canonical(readback), time.time() + 30, identifier),
        )


def tick(store, client):
    archive(store)
    with store.db() as db:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT id FROM experiences WHERE status NOT IN ('synced', 'invalid') AND next_attempt<=? ORDER BY created LIMIT 10",
                (time.time(),),
            )
        ]
    for identifier in ids:
        sync_one(store, identifier, client)
    store.health("worker", {"updated": time.time(), "status": "running"})
