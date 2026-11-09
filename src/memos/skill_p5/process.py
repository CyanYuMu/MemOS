"""Reconstruct conservative repair cycles from paired, delivered native events.

This module proves order and budget facts. It never infers a semantic branch or
actual mutation from an Agent statement, and never grants branch coverage.
"""

from __future__ import annotations

from memos.skill_drafting.domain import hash_json
from memos.skill_p5.broker import decode_public_result
from memos.skill_p5.delivery import _visible_deliveries
from memos.skill_p5.tasks import get_suite


VERSION = "p5-process-1"
VISIBLE = "mcp__memos_run__run_visible_checks"
MUTATIONS = {"Edit", "Write"}
ALLOWED = MUTATIONS | {"Read", "Skill", VISIBLE, "mcp__memos_run__record_rule"}


def select_intermediate_snapshots(suite_id: str, records: list[dict]) -> list[dict]:
    """C2 retains all public checkpoints but has no C1 midpoint qualification.

    Even a newly observed Dream regression is not a C1 transition. It stays in
    the public timeline for review and never creates hidden midpoint coverage.
    """
    get_suite(suite_id)
    if not isinstance(records, list):
        raise ValueError("invalid_release_process_records")
    return []


def initial_checks_match(suite_id: str, checks: list[dict]) -> bool:
    """Require the exact registered initial defects, including healthy Dream."""
    task = get_suite(suite_id)
    expected = [
        {
            "name": row["id"],
            "status": "failed"
            if row["id"] in task.expected_failures["initial"]["hidden"]
            else "passed",
        }
        for row in task.checks
    ]
    return (
        isinstance(checks, list)
        and [
            {"name": row.get("name"), "status": row.get("status")}
            if isinstance(row, dict)
            else None
            for row in checks
        ]
        == expected
    )


def build_process_evidence(
    suite_id: str, records: list[dict], events: list[dict], deliveries: list[dict] | None
) -> dict:
    """Return facts even for incomplete evidence; never replace gaps with guesses.

    All Edit/Write attempts count conservatively, including failed or no-op
    attempts. A complete delivered public check closes that edit pass. This is a
    process budget, distinct from whether the final patch actually repairs code.
    """
    task = get_suite(suite_id)
    if (
        not isinstance(records, list)
        or not isinstance(events, list)
        or any(not isinstance(row, dict) for row in records + events)
        or (
            deliveries is not None
            and (
                not isinstance(deliveries, list)
                or any(not isinstance(row, dict) for row in deliveries)
            )
        )
    ):
        raise ValueError("invalid_extension_process_inputs")
    issues = []

    def issue(code):
        if code not in issues:
            issues.append(code)

    parsed, checkpoints = {}, []
    for index, record in enumerate(records, 1):
        try:
            if record["check_index"] != index:
                raise ValueError("index")
            result = decode_public_result(suite_id, record["result"])
            registered_fault = (
                task.fault is not None
                and index == task.fault["checkIndex"]
                and record.get("fault") == task.fault
                and record["result"]["execution_error"] == task.fault["code"]
                and result["status"] == "error"
                and result["checks"] == []
            )
            if not registered_fault and (
                record["result"]["execution_error"] or result["status"] == "error"
            ):
                raise ValueError("execution")
            parsed[index] = result
        except (KeyError, TypeError, ValueError):
            issue("invalid_public_check")
        result = parsed.get(index, {})
        checkpoints.append(
            {
                "checkIndex": index,
                "snapshot": record.get("candidate_snapshot"),
                # This is the canonical record identity. File bytes have a
                # separate collector hash and cannot be supplied to this pure function.
                "recordSha256": hash_json(record),
                "startedEvent": None,
                "completedEvent": None,
                "checks": [
                    {"name": row["name"], "status": row["status"]}
                    for row in result.get("checks", [])
                ],
                "groupStatus": result.get("groupStatus", {"scoped": "error", "dream": "error"}),
                "cyclesUsed": 0,
                "editEventRefs": [],
            }
        )
    exact = _visible_deliveries(records, events, ())
    if exact is None or deliveries != exact:
        issue("public_delivery_incomplete")
        exact = []
    by_end = {row["completed_event"]: row for row in exact}
    by_start = {row["started_event"]: row for row in exact}
    pending, completed = {}, set()
    context, session, previous_sequence = None, None, 0
    seen_events = set()
    edit_refs = []
    cycles, last_index, last_snapshot = 0, 0, None
    for event in events:
        try:
            event_id = event["event_id"]
            sequence = event["sequence"]
            if (
                event_id in seen_events
                or type(sequence) is not int
                or sequence <= previous_sequence
            ):
                issue("event_order_invalid")
            seen_events.add(event_id)
            previous_sequence = sequence
            if event["producer"] != "native_stream":
                continue
            if event["kind"] not in {"tool_started", "tool_completed"}:
                continue
            current_context = (
                event["context"]["run_id"],
                event["context"]["attempt_id"],
            )
            current_session = event["native_session_id"]
            call_id = event["native_tool_call_id"]
            if not all(
                isinstance(item, str) and item
                for item in (*current_context, current_session, call_id)
            ):
                raise ValueError("identity")
            if context is None:
                context, session = current_context, current_session
            if context != current_context or session != current_session:
                issue("native_identity_changed")
            identity = (current_session, call_id)
            if event["kind"] == "tool_started":
                tool = event["payload"]["tool_name"]
                if tool not in ALLOWED:
                    issue("unrecognized_tool")
                if identity in pending or identity in completed:
                    issue("tool_pair_invalid")
                active_tools = [row["tool"] for row in pending.values()]
                if (tool in MUTATIONS and VISIBLE in active_tools) or (
                    tool == VISIBLE and (set(active_tools) & MUTATIONS or VISIBLE in active_tools)
                ):
                    issue("check_edit_overlap")
                if tool in MUTATIONS:
                    if last_index == 0:
                        issue("baseline_not_observed_before_edit")
                    edit_refs.append(event_id)
                if tool == VISIBLE and event_id in by_start:
                    row = by_start[event_id]
                    checkpoint = checkpoints[row["check_index"] - 1]
                    checkpoint["startedEvent"] = event_id
                pending[identity] = {"tool": tool, "event": event_id}
                continue
            started = pending.pop(identity, None)
            if started is None or identity in completed:
                issue("tool_pair_invalid")
                continue
            completed.add(identity)
            if started["tool"] != VISIBLE:
                continue
            delivery = by_end.get(event_id)
            if delivery is None:
                issue("public_delivery_incomplete")
                continue
            index = delivery["check_index"]
            if index != last_index + 1:
                issue("check_order_invalid")
            checkpoint = checkpoints[index - 1]
            checkpoint["completedEvent"] = event_id
            checkpoint["editEventRefs"] = list(edit_refs)
            if index not in parsed:
                continue
            if (
                last_snapshot is not None
                and checkpoint["snapshot"] != last_snapshot
                and not edit_refs
            ):
                issue("snapshot_changed_without_edit")
            if edit_refs:
                cycles += 1
                edit_refs.clear()
            checkpoint["cyclesUsed"] = cycles
            last_index, last_snapshot = index, checkpoint["snapshot"]
        except (KeyError, TypeError, ValueError, IndexError):
            issue("malformed_process_event")
    if pending:
        issue("tool_pair_incomplete")
    if len(records) < 1 or last_index != len(records):
        issue("checkpoint_incomplete")
    if edit_refs:
        issue("unverified_edits")
    return {
        "version": VERSION,
        "complete": not issues,
        "issues": issues,
        "maxCycles": 2,
        "cyclesUsed": cycles,
        "budgetExceeded": cycles > 2 or (cycles == 2 and bool(edit_refs)),
        "unverifiedEdits": bool(edit_refs),
        "checkpoints": checkpoints,
    }
