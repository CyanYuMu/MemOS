"""Strict shared fact shapes for release schema six; frozen contracts stay unchanged."""

from memos.skill_governance.domain import _ASSESSMENT_KEYS, _enum, _refs
from memos.skill_management.domain import digest, require, text


_STATUSES = {"passed", "failed", "error"}
_KEYS = _ASSESSMENT_KEYS | {
    "initialVerification",
    "publicVerification",
    "outcome",
    "batchId",
    "taskDigest",
    "suiteId",
    "checkMapping",
    "processVerification",
    "intermediateVerifications",
    "taskOutcome",
}


def _validate_events(events):
    require(isinstance(events, list) and len(events) <= 2000)
    indexed = {}
    for event in events:
        require(isinstance(event, dict) and set(event) == {"id", "kind", "value"})
        text(event["id"], maximum=300)
        _enum(
            event["kind"], {"tool_started", "tool_completed", "assistant_text", "rule_declaration"}
        )
        require(isinstance(event["value"], dict) and event["id"] not in indexed)
        indexed[event["id"]] = event
    return indexed


def _validate_public(public, events):
    require(
        isinstance(public, dict)
        and set(public)
        == {
            "initialExitCode",
            "finalExitCode",
            "recordsVerified",
            "deliveriesVerified",
            "deliveryRefs",
        }
    )
    for key in ("initialExitCode", "finalExitCode"):
        # Preserve actual process failures (including negative signal codes).
        # Qualification below still requires the fixed business exits 0/1.
        require(public[key] is None or type(public[key]) is int)
    for key in ("recordsVerified", "deliveriesVerified"):
        require(type(public[key]) is bool)
    deliveries = public["deliveryRefs"]
    require(isinstance(deliveries, list) and len(deliveries) <= 100)
    indices, used = [], set()
    event_order = list(events)
    for delivery in deliveries:
        require(
            isinstance(delivery, dict)
            and set(delivery) == {"checkIndex", "startedEvent", "completedEvent"}
        )
        index = delivery["checkIndex"]
        require(type(index) is int and 1 <= index <= 100)
        ids = _refs([delivery["startedEvent"], delivery["completedEvent"]], events)
        require(not used.intersection(ids), "invalid_delivery_reference")
        require(
            events[ids[0]]["kind"] == "tool_started"
            and events[ids[1]]["kind"] == "tool_completed"
            and event_order.index(ids[0]) < event_order.index(ids[1]),
            "invalid_delivery_reference",
        )
        indices.append(index)
        used.update(ids)
    require(indices == sorted(set(indices)), "invalid_delivery_reference")
    if public["deliveriesVerified"]:
        require(
            len(deliveries) >= 1 and indices == list(range(1, len(deliveries) + 1)),
            "invalid_delivery_reference",
        )
    else:
        require(not deliveries, "invalid_delivery_reference")


def _declares_rule(event, rule_id):
    display = event["value"].get("display")
    return (
        event["kind"] == "rule_declaration"
        and isinstance(display, dict)
        and display.get("rule_id") == rule_id
    )


def _check_rows(rows, suite, *, complete=True):
    require(isinstance(rows, list) and len(rows) <= len(suite.checks))
    names = []
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"name", "status"})
        text(row["name"], maximum=200)
        _enum(row["status"], _STATUSES)
        names.append(row["name"])
    expected = [row["id"] for row in suite.checks]
    require(names == [name for name in expected if name in names], "extension_checks_changed")
    if complete:
        require(names == expected, "extension_checks_missing")
    return {row["name"]: row["status"] for row in rows}


def _groups(rows, suite):
    states = {row["name"]: row["status"] for row in rows}
    result = {}
    for group in ("scoped", "dream"):
        values = [states.get(row["id"], "error") for row in suite.checks if row["group"] == group]
        result[group] = (
            "error" if "error" in values else "failed" if "failed" in values else "passed"
        )
    return result


def _display(event):
    value = event["value"].get("display")
    return value if isinstance(value, dict) else {}


def _is_edit(event):
    return event["kind"] == "tool_started" and _display(event).get("tool_name") in {"Edit", "Write"}


def _validate_process(value, events, suite):
    process = value["processVerification"]
    require(
        isinstance(process, dict)
        and set(process)
        == {
            "version",
            "complete",
            "issues",
            "maxCycles",
            "cyclesUsed",
            "budgetExceeded",
            "unverifiedEdits",
            "checkpoints",
        }
    )
    require(process["version"] == "release-process-1")
    require(type(process["maxCycles"]) is int and process["maxCycles"] == 2)
    require(type(process["cyclesUsed"]) is int and 0 <= process["cyclesUsed"] <= 100)
    for key in ("complete", "budgetExceeded", "unverifiedEdits"):
        require(type(process[key]) is bool)
    _refs(process["issues"], process["issues"], nonempty=False)
    require(
        process["budgetExceeded"]
        is (
            process["cyclesUsed"] > 2 or (process["cyclesUsed"] >= 2 and process["unverifiedEdits"])
        ),
        "extension_budget_mismatch",
        409,
    )
    checkpoints = process["checkpoints"]
    require(isinstance(checkpoints, list) and len(checkpoints) <= 100)
    deliveries = {row["checkIndex"]: row for row in value["publicVerification"]["deliveryRefs"]}
    order = {key: index for index, key in enumerate(events)}
    edits, cycles, previous_end = [], 0, -1
    for index, checkpoint in enumerate(checkpoints, 1):
        require(
            isinstance(checkpoint, dict)
            and set(checkpoint)
            == {
                "checkIndex",
                "snapshot",
                "recordSha256",
                "startedEvent",
                "completedEvent",
                "checks",
                "groupStatus",
                "cyclesUsed",
                "editEventRefs",
            }
        )
        require(type(checkpoint["checkIndex"]) is int and checkpoint["checkIndex"] == index)
        digest(checkpoint["snapshot"])
        digest(checkpoint["recordSha256"])
        require(type(checkpoint["cyclesUsed"]) is int and 0 <= checkpoint["cyclesUsed"] <= 100)
        _check_rows(
            checkpoint["checks"],
            suite,
            complete=process["complete"]
            and not (suite.fault and index == suite.fault["checkIndex"]),
        )
        require(
            checkpoint["groupStatus"] == _groups(checkpoint["checks"], suite)
            or (
                not process["complete"]
                and bool(process["issues"])
                and (
                    checkpoint["groupStatus"] == {"scoped": "error", "dream": "error"}
                    or (not checkpoint["checks"] and checkpoint["groupStatus"] == {})
                )
            ),
            "extension_group_mismatch",
            409,
        )
        started, completed = checkpoint["startedEvent"], checkpoint["completedEvent"]
        for ref in (started, completed):
            if ref is not None:
                _refs([ref], events)
        if started is not None:
            require(
                events[started]["kind"] == "tool_started"
                and _display(events[started]).get("tool_name")
                == "mcp__memos_run__run_visible_checks",
                "invalid_delivery_reference",
            )
        if completed is not None:
            require(events[completed]["kind"] == "tool_completed", "invalid_delivery_reference")
        if started is not None and completed is not None:
            require(order[started] < order[completed], "invalid_delivery_reference")
            require(
                deliveries.get(index)
                == {"checkIndex": index, "startedEvent": started, "completedEvent": completed},
                "invalid_delivery_reference",
            )
        edit_refs = _refs(checkpoint["editEventRefs"], events, nonempty=False)
        require(not set(edits).intersection(edit_refs), "extension_duplicate_edit")
        require(all(_is_edit(events[ref]) for ref in edit_refs), "extension_edit_reference")
        edits.extend(edit_refs)
        if process["complete"]:
            require(started is not None and completed is not None, "extension_delivery_missing")
            require(previous_end < order[started], "extension_checkpoint_order")
            require(
                all(previous_end < order[ref] < order[started] for ref in edit_refs),
                "extension_edit_order",
            )
            require(edit_refs == sorted(edit_refs, key=order.get), "extension_edit_order")
            cycles += bool(edit_refs)
            require(checkpoint["cyclesUsed"] == cycles, "extension_cycle_mismatch", 409)
            require(
                all(row["status"] != "error" for row in checkpoint["checks"]),
                "extension_check_error",
            )
            previous_end = order[completed]
    if process["complete"]:
        require(
            not process["issues"] and not process["unverifiedEdits"] and len(checkpoints) >= 1,
            "extension_process_incomplete",
        )
        require(
            value["publicVerification"]["recordsVerified"]
            and value["publicVerification"]["deliveriesVerified"],
            "extension_process_incomplete",
        )
        require(
            checkpoints[0]["cyclesUsed"] == 0 and not checkpoints[0]["editEventRefs"],
            "extension_baseline_missing",
        )
        require(
            checkpoints[0]["snapshot"] == value["initialVerification"]["candidateSnapshot"]
            and checkpoints[-1]["snapshot"] == value["candidateSnapshot"],
            "extension_snapshot_mismatch",
            409,
        )
        require(
            edits == [ref for ref, event in events.items() if _is_edit(event)],
            "extension_edits_unaccounted",
            409,
        )
        require(process["cyclesUsed"] == cycles, "extension_cycle_mismatch", 409)
        require(len(deliveries) == len(checkpoints), "extension_delivery_missing")
        for key, checkpoint in (
            ("initialExitCode", checkpoints[0]),
            ("finalExitCode", checkpoints[-1]),
        ):
            expected_exit = (
                None
                if suite.fault and checkpoint["checkIndex"] == suite.fault["checkIndex"]
                else 0
                if all(row["status"] == "passed" for row in checkpoint["checks"])
                else 1
            )
            require(
                value["publicVerification"][key] == expected_exit, "extension_public_status", 409
            )


def _validate_intermediates(value, suite):
    # C2 has no C1 transition qualification; never repurpose an old midpoint.
    require(value["intermediateVerifications"] == [], "release_intermediates_not_supported", 409)
