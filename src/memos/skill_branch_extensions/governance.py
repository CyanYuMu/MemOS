"""Schema-three review: delivered intermediate states constrain human coverage."""

from __future__ import annotations

from memos.skill_branch_extensions.tasks import get_suite, suite_digest
from memos.skill_branch_extensions.verifier import PROTOCOL
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION
from memos.skill_drafting.domain import hash_json
from memos.skill_governance.domain import (
    _ASSESSMENT_KEYS,
    _enum,
    _refs,
    validate_judgment_shape,
)
from memos.skill_management.domain import branches, digest, public_copy, require, text


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
}
_STATUSES = {"passed", "failed", "error"}


def _collector_digest():
    from memos.skill_branch_extensions.collector import collector_digest

    return collector_digest()


def assessment_outcome(value: dict) -> str:
    """Project only automatic facts; this is not a human assessment of Skill use."""
    initial, final, public = (
        value["initialVerification"],
        value["verification"],
        value["publicVerification"],
    )
    complete = (
        value["processVerification"]["complete"] is True
        and value["processVerification"]["unverifiedEdits"] is False
        and value["captureComplete"] is True
        and value["loadVerified"] is True
        and initial["matches"] is True
        and initial["status"] == "failed"
        and final["matches"] is True
        and final["sameFinalPatch"] is True
        and final["status"] in {"passed", "failed"}
        and public["recordsVerified"] is True
        and public["deliveriesVerified"] is True
        and type(public["initialExitCode"]) is int
        and public["initialExitCode"] == 1
        and type(public["finalExitCode"]) is int
        and public["finalExitCode"] in {0, 1}
    )
    if not complete:
        return "incomplete"
    if (
        final["status"] == "passed"
        and public["finalExitCode"] == 0
        and value["behaviorChanged"] is True
    ):
        return "repair_candidate"
    return "failed"


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


def validate_assessment(candidate: dict, validation: dict, assessment: dict) -> dict:
    """Validate host-derived schema three while preserving older producer contracts."""
    require(isinstance(assessment, dict) and set(assessment) == _KEYS)
    value = public_copy(assessment)
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 3)
    require(value["collectorVersion"] == "extension-review-1")
    for key in (
        "validationId",
        "contentHash",
        "exportHash",
        "runId",
        "attemptId",
        "caseId",
        "caseRevision",
        "lineageId",
        "split",
        "purpose",
    ):
        require(value[key] == validation[key], "assessment_binding_mismatch", 409)
    require(value["split"] == "holdout" and validation["kind"] == "independent_task")
    _enum(value["purpose"], {"actual", "management_test"})
    require(
        candidate["revision"] == CANDIDATE_REVISION
        and value["contentHash"] == candidate["content_hash"],
        "assessment_binding_mismatch",
        409,
    )
    try:
        text(value["suiteId"], maximum=100)
        task = get_suite(value["suiteId"])
    except ValueError:
        require(False, "branch_task_mismatch", 409)
    require(
        value["caseId"] == task.task_id
        and value["caseRevision"] == task.revision
        and value["lineageId"] == task.lineage_id
        and value["taskDigest"] == suite_digest(task),
        "branch_task_mismatch",
        409,
    )
    for key in (
        "reportSha256",
        "eventsSha256",
        "candidateSnapshot",
        "coveragePlanHash",
        "collectorDigest",
        "batchId",
        "taskDigest",
    ):
        digest(value[key])
    if value["purpose"] == "actual":
        require(value["collectorDigest"] == _collector_digest(), "collector_changed", 409)
    for key in ("captureComplete", "loadVerified", "behaviorChanged"):
        require(type(value[key]) is bool)
    _enum(value["originalStatus"], _STATUSES)
    final = value["verification"]
    require(
        isinstance(final, dict)
        and set(final)
        == {"status", "receiptSha256", "protocol", "matches", "sameFinalPatch", "posthoc"}
    )
    _enum(final["status"], _STATUSES)
    digest(final["receiptSha256"])
    require(final["protocol"] == PROTOCOL and final["posthoc"] is False)
    require(value["originalStatus"] == final["status"])
    for key in ("matches", "sameFinalPatch"):
        require(type(final[key]) is bool)
    initial = value["initialVerification"]
    require(
        isinstance(initial, dict)
        and set(initial) == {"status", "receiptSha256", "protocol", "matches", "candidateSnapshot"}
    )
    _enum(initial["status"], _STATUSES)
    digest(initial["receiptSha256"])
    digest(initial["candidateSnapshot"])
    require(initial["protocol"] == PROTOCOL and type(initial["matches"]) is bool)
    require(
        not value["behaviorChanged"] or initial["candidateSnapshot"] != value["candidateSnapshot"]
    )
    checks = value["checks"]
    require(
        isinstance(checks, list) and len(checks) <= len(tuple(row["id"] for row in task.checks))
    )
    names = []
    for check in checks:
        require(isinstance(check, dict) and set(check) == {"name", "status"})
        require(
            isinstance(check["name"], str)
            and check["name"] in tuple(row["id"] for row in task.checks)
            and check["name"] not in names
        )
        _enum(check["status"], _STATUSES)
        names.append(check["name"])
    if final["status"] != "error":
        require(names == [row["id"] for row in task.checks])
        require(all(check["status"] in {"passed", "failed"} for check in checks))
    if final["status"] == "passed":
        require(all(check["status"] == "passed" for check in checks))
    require(value["checkMapping"] == list(task.checks), "extension_check_mapping_changed", 409)
    events = _validate_events(value["events"])
    _validate_public(value["publicVerification"], events)
    _validate_process(value, events, task)
    _validate_intermediates(value, task)
    eligible = _refs(value["branchCandidates"], task.target_branches, nonempty=False)
    require(set(eligible) <= branches(candidate))
    require(
        value["coveragePlanHash"]
        == hash_json(
            {
                "candidateRevision": candidate["revision"],
                "caseId": task.task_id,
                "caseDigest": suite_digest(task),
                "eligibleBranches": eligible,
            }
        ),
        "coverage_plan_mismatch",
        409,
    )
    _enum(value["outcome"], {"repair_candidate", "failed", "incomplete"})
    require(value["outcome"] == assessment_outcome(value), "assessment_outcome_mismatch", 409)
    value["assessmentId"] = hash_json(value)
    return value


def _declares_rule(event, rule_id):
    display = event["value"].get("display")
    return (
        event["kind"] == "rule_declaration"
        and isinstance(display, dict)
        and display.get("rule_id") == rule_id
    )


def adjudicate_assessment(
    candidate: dict, validation: dict, assessment: dict, decision: dict
) -> dict:
    """Grant only explicitly cited reviewed coverage; never waive automatic facts."""
    require(isinstance(assessment, dict))
    checked = validate_assessment(
        candidate, validation, {k: v for k, v in assessment.items() if k != "assessmentId"}
    )
    require(checked == assessment, "assessment_binding_mismatch", 409)
    decision = validate_judgment_shape(decision)
    require(
        decision["taskDecision"] in {"repair_confirmed", "failed", "needs_evidence"},
        "unsupported_branch_outcome",
        422,
    )
    require(decision["historyDecision"] == "retain", "history_explanation_unverified", 422)
    events = {event["id"]: event for event in assessment["events"]}
    _refs(decision["eventRefs"], events, nonempty=decision["taskDecision"] != "needs_evidence")
    confirmed = decision["taskDecision"] == "repair_confirmed"
    if confirmed:
        require(
            assessment["outcome"] == "repair_candidate"
            and decision["boundary"] is None
            and decision["violations"] == "none",
            "evidence_insufficient",
            422,
        )
    require(
        decision["usageDecision"] != "correct"
        or (
            assessment["processVerification"]["complete"]
            and not assessment["processVerification"]["budgetExceeded"]
        ),
        "usage_budget_or_process_unverified",
        422,
    )
    independent = all(
        source.get("case_id") != assessment["caseId"]
        and source.get("lineage_id") != assessment["lineageId"]
        for source in candidate["source_refs"]
    )
    coverage = []
    for claim in decision["branches"]:
        _refs([claim["branchId"]], assessment["branchCandidates"])
        require(claim["branchId"] not in coverage)
        require(
            confirmed
            and decision["usageDecision"] == "correct"
            and decision["applicability"] == "in_scope"
            and decision["violations"] == "none",
            "branch_not_applicable",
            422,
        )
        require(independent, "source_task_overlap", 422)
        _validate_claim(assessment, claim, events)
        coverage.append(claim["branchId"])
    return {
        "assessmentId": assessment["assessmentId"],
        "validationId": validation["validationId"],
        "decision": decision,
        "taskResolved": confirmed,
        "repairConfirmed": confirmed,
        "resolvedHistory": False,
        "qualifiedIndependent": confirmed
        and independent
        and decision["usageDecision"] == "correct"
        and bool(coverage),
        "branchCoverage": sorted(coverage),
        "boundaryCoverage": [],
        "counterexample": decision["taskDecision"] == "failed"
        and decision["applicability"] == "in_scope",
        "seriousViolation": decision["violations"] == "serious",
        "newIndependentSamples": 0,
    }


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


def _transition_checks(rows):
    """Only a remaining UPDATE fault is a supported scoped-to-Dream condition."""
    values = {row["name"]: row["status"] for row in rows}
    return (
        all(
            values.get("scoped." + name) == "passed"
            for name in (
                "caller_contract",
                "fake_scope_contract",
                "scope_generalization",
                "caller_with_fake",
                "update_regressions",
                "embedding_failure",
            )
        )
        and any(
            values.get("dream." + name) == "failed"
            for name in ("caller_contract", "fake_contract", "caller_with_fake")
        )
        and all(
            values.get("dream." + name) == "passed"
            for name in ("other_actions", "no_unintended_write")
        )
    )


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
    require(process["version"] == "extension-process-1")
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
        _check_rows(checkpoint["checks"], suite, complete=process["complete"])
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
            not process["issues"] and not process["unverifiedEdits"] and len(checkpoints) >= 2,
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
                0 if all(row["status"] == "passed" for row in checkpoint["checks"]) else 1
            )
            require(
                value["publicVerification"][key] == expected_exit, "extension_public_status", 409
            )


def _validate_intermediates(value, suite):
    rows = value["intermediateVerifications"]
    require(isinstance(rows, list) and len(rows) <= 100)
    expected = {}
    for checkpoint in value["processVerification"]["checkpoints"]:
        if _transition_checks(checkpoint["checks"]):
            expected.setdefault(checkpoint["snapshot"], []).append(checkpoint["checkIndex"])
    require(
        [row.get("snapshot") for row in rows if isinstance(row, dict)] == list(expected),
        "extension_intermediate_inventory",
        409,
    )
    for row in rows:
        require(
            isinstance(row, dict)
            and set(row)
            == {
                "snapshot",
                "checkIndices",
                "receiptSha256",
                "protocol",
                "matches",
                "status",
                "checks",
            }
        )
        digest(row["snapshot"])
        digest(row["receiptSha256"])
        require(row["protocol"] == PROTOCOL and type(row["matches"]) is bool)
        require(
            row["checkIndices"] == expected[row["snapshot"]]
            and all(type(i) is int for i in row["checkIndices"]),
            "extension_intermediate_binding",
            409,
        )
        _enum(row["status"], _STATUSES)
        states = _check_rows(row["checks"], suite, complete=row["status"] != "error")
        if row["status"] != "error":
            require("error" not in states.values())
            require(
                (row["status"] == "passed")
                is all(status == "passed" for status in states.values()),
                "extension_intermediate_status",
                409,
            )


def _check_refs(checkpoint, mapping, group=None):
    return {
        f"check:{checkpoint['checkIndex']}:{row['id']}"
        for row in mapping
        if group is None or row["group"] == group
    }


def _validate_claim(assessment, claim, events):
    conditions = _refs(claim["conditionRefs"], events)
    actions = _refs(claim["actionRefs"], events)
    checkpoints = assessment["processVerification"]["checkpoints"]
    mapping = assessment["checkMapping"]
    allowed = set().union(*(_check_refs(cp, mapping) for cp in checkpoints))
    refs = set(_refs(claim["checkRefs"], allowed))
    order = {key: index for index, key in enumerate(events)}
    declarations = [
        ref
        for ref in conditions + actions
        if _declares_rule(events[ref], claim["branchId"].rsplit("#", 1)[0])
    ]
    require(declarations, "rule_declaration_missing", 422)
    first, final = checkpoints[0], checkpoints[-1]

    def observed(cp):
        return cp["completedEvent"] in conditions

    def timely(cp, edit):
        return (
            edit in actions
            and _is_edit(events[edit])
            and any(
                order[cp["completedEvent"]] < order[decl] < order[edit] for decl in declarations
            )
        )

    def edited_after(before, after):
        return any(
            timely(before, edit)
            and order[before["completedEvent"]] < order[edit] < order[after["startedEvent"]]
            for cp in checkpoints
            if before["checkIndex"] < cp["checkIndex"] <= after["checkIndex"]
            for edit in cp["editEventRefs"]
        )

    branch = claim["branchId"]
    valid = False
    if branch == "confirm_contract#3":
        edits = [ref for cp in checkpoints for ref in cp["editEventRefs"]]
        valid = (
            bool(edits)
            and observed(first)
            and first["groupStatus"]["scoped"] == "failed"
            and timely(first, edits[0])
            and _check_refs(first, mapping, "scoped") <= refs
        )
    elif branch in {"repair_scoped_reads#0", "repair_dream_update#0"}:
        group = "scoped" if branch == "repair_scoped_reads#0" else "dream"
        for before in checkpoints:
            if (
                not observed(before)
                or before["groupStatus"][group] != "failed"
                or before["cyclesUsed"] >= 2
            ):
                continue
            if group == "dream" and not any(
                row["status"] == "failed"
                and row["name"]
                in {"dream.caller_contract", "dream.fake_contract", "dream.caller_with_fake"}
                for row in before["checks"]
            ):
                continue
            for after in checkpoints:
                required_group = group if group == "scoped" else None
                if (
                    after["checkIndex"] > before["checkIndex"]
                    and after["cyclesUsed"] <= 2
                    and after["groupStatus"][group] == "passed"
                    and (group == "scoped" or after["groupStatus"]["scoped"] == "passed")
                    and _check_refs(after, mapping, required_group) <= refs
                    and edited_after(before, after)
                ):
                    valid = True
    elif branch == "verify_scoped_reads#0":
        for middle in checkpoints:
            hidden = next(
                (
                    row
                    for row in assessment["intermediateVerifications"]
                    if row["snapshot"] == middle["snapshot"]
                ),
                None,
            )
            if (
                observed(middle)
                and middle["cyclesUsed"] == 1
                and _transition_checks(middle["checks"])
                and hidden
                and hidden["matches"]
                and hidden["status"] == "failed"
                and _transition_checks(hidden["checks"])
                and final["groupStatus"] == {"scoped": "passed", "dream": "passed"}
                and _check_refs(middle, mapping) | _check_refs(final, mapping) <= refs
                and edited_after(middle, final)
            ):
                valid = True
    elif branch == "verify_dream_update#0":
        valid = (
            observed(final)
            and final["groupStatus"] == {"scoped": "passed", "dream": "passed"}
            and _check_refs(final, mapping) <= refs
            and any(
                order[final["completedEvent"]] < order[decl] < order[action]
                for decl in declarations
                for action in actions
                if events[action]["kind"] == "assistant_text"
            )
        )
    require(valid, "extension_branch_process_insufficient", 422)
