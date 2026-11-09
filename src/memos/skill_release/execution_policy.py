"""Versioned host execution limits, separate from whether the patch is correct."""

from __future__ import annotations

import hashlib

from pathlib import Path

from memos.skill_drafting.domain import hash_json


VERSION = "release-execution-policy-1"
PLAN_VERSION = "release-plan-1"
PROMPT = (
    "Host execution budget applies equally with or without a Skill: at most two "
    "repair-and-recheck cycles. All Edit/Write attempts between two delivered public "
    "checks count as one cycle, including failed, no-op, or reverted attempts. "
    "Multiple edits before the next check form one cycle. Obtain the initial check "
    "before editing. Once all applicable checks pass, stop unnecessary edits and "
    "report the result. If unresolved after two cycles, stop editing, preserve the "
    "remaining failures, and report the limitation. "
)


def policy_identity() -> dict:
    identity = {
        "protocol": VERSION,
        "maxRepairCycles": 2,
        "cycleDefinition": "edit_attempt_batch_between_delivered_checks",
        "stopWhenAllChecksPass": True,
        "promptSha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "implementationSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    return {**identity, "digest": hash_json(identity)}


def unit_issues(value: dict) -> list[str]:
    """Evaluate flat, host-recomputed facts; truthiness must never grant admission."""
    issues = []
    if value.get("group") not in {"none", "skill"}:
        issues.append("invalid_group")
    if value.get("status") != "passed":
        issues.append("task_outcome_not_qualified")
    if value.get("taskAccepted") is not True:
        issues.append("task_not_accepted")
    if value.get("captureComplete") is not True:
        issues.append("capture_incomplete")
    expected_loading = "not_requested" if value.get("group") == "none" else "verified"
    if value.get("loading") != expected_loading:
        issues.append("loading_unverified")
    if value.get("processComplete") is not True:
        issues.append("process_incomplete")
    if type(value.get("maxCycles")) is not int or value["maxCycles"] != 2:
        issues.append("invalid_cycle_limit")
    cycles = value.get("cyclesUsed")
    if type(cycles) is not int or cycles < 0:
        issues.append("invalid_cycle_count")
    if value.get("budgetExceeded") is not False or (type(cycles) is int and cycles > 2):
        issues.append("repair_budget_exceeded")
    if value.get("unverifiedEdits") is not False:
        issues.append("unverified_edits")
    if value.get("policyMatches") is not True:
        issues.append("policy_mismatch")
    return issues


def report_eligibility(report: dict, group: str) -> dict:
    """Immediate batch gate; the collector later rederives these facts from artifacts."""
    process = report.get("process_evidence")
    process = process if isinstance(process, dict) else {}
    capture = report.get("capture")
    loading = report.get("loading")
    facts = {
        "group": group if report.get("group") == group else None,
        "status": report.get("status"),
        "taskAccepted": report.get("task_accepted"),
        "captureComplete": isinstance(capture, dict) and capture.get("status") == "complete",
        "loading": loading.get("status") if isinstance(loading, dict) else None,
        "processComplete": process.get("complete"),
        "maxCycles": process.get("maxCycles"),
        "cyclesUsed": process.get("cyclesUsed"),
        "budgetExceeded": process.get("budgetExceeded"),
        "unverifiedEdits": process.get("unverifiedEdits"),
        "policyMatches": report.get("execution_policy") == policy_identity(),
    }
    issues = unit_issues(facts)
    return {**facts, "qualified": not issues, "issues": issues}


def plan_issues(calibration: dict, trial: dict) -> list[str]:
    return [
        f"{name}:{issue}"
        for name, value in (("calibration", calibration), ("trial", trial))
        for issue in unit_issues(value)
    ]


def task_outcome(suite_id: str, facts: dict) -> dict:
    """Recompute outcome from retained facts; test-green alone does not mean repair."""
    from memos.skill_release.process import initial_checks_match
    from memos.skill_release.tasks import get_suite

    task = get_suite(suite_id)
    issues = []

    def need(condition, reason):
        if not condition:
            issues.append(reason)

    need(facts.get("receiptsMatch") is True, "receipt_unbound")
    need(facts.get("publicVerified") is True, "public_unverified")
    need(facts.get("processComplete") is True, "process_incomplete")
    need(facts.get("budgetExceeded") is False, "repair_budget_exceeded")
    need(facts.get("unverifiedEdits") is False, "unverified_edits")
    need(initial_checks_match(suite_id, facts.get("initialChecks")), "initial_contract_mismatch")
    initial, final = facts.get("initialChecks"), facts.get("finalChecks")

    def status(rows):
        if (
            not isinstance(rows, list)
            or [r.get("name") for r in rows] != [r["id"] for r in task.checks]
            or any(r.get("status") not in {"passed", "failed"} for r in rows)
        ):
            return "error"
        return "failed" if any(r["status"] == "failed" for r in rows) else "passed"

    need(facts.get("initialStatus") == status(initial) != "error", "initial_status_mismatch")
    need(facts.get("finalStatus") == status(final) != "error", "final_status_mismatch")
    initial_failed = (
        {r["name"] for r in initial if r["status"] == "failed"}
        if status(initial) != "error"
        else set()
    )
    final_failed = (
        {r["name"] for r in final if r["status"] == "failed"} if status(final) != "error" else set()
    )
    if task.outcome in {"unresolved", "tool_failure"}:
        need(final_failed <= initial_failed, "new_business_regression")
    changed = facts.get("changed")
    need(
        type(changed) is bool
        and changed is (facts.get("initialSnapshot") != facts.get("finalSnapshot")),
        "snapshot_change_mismatch",
    )
    if task.outcome == "tool_failure":
        need(facts.get("faultVerified") is True, "fault_delivery_unverified")
        need(facts.get("publicInitialChecks") == initial, "fault_baseline_unverified")
        need(facts.get("publicFinalChecks") == [], "fault_checks_fabricated")
    else:
        need(facts.get("faultVerified") is False, "unexpected_tool_fault")
        need(facts.get("publicInitialChecks") == initial, "public_initial_mismatch")
        need(facts.get("publicFinalChecks") == final, "public_final_mismatch")
        if task.outcome == "repair":
            need(
                changed is True and status(initial) == "failed" and status(final) == "passed",
                "repair_unproven",
            )
        elif task.outcome == "healthy":
            need(
                changed is False and status(initial) == status(final) == "passed", "health_unproven"
            )
        elif task.outcome in {"out_of_scope", "missing_evidence"}:
            need(changed is False and initial == final, "stop_task_changed")
        elif task.outcome == "unresolved":
            need(
                status(final) == "failed"
                and bool(final_failed & {"dream.caller_contract", "dream.caller_with_fake"}),
                "remaining_dream_failure_unproven",
            )
    return {"kind": task.outcome, "eligible": not issues, "reasons": issues}


def outcome_facts(
    task,
    initial,
    final,
    initial_id,
    final_id,
    changed,
    records,
    process,
    public_verified,
    receipts_match,
):
    """One field adapter shared by producer and collector; inputs are independently read."""
    from memos.skill_release.broker import decode_public_result

    first = decode_public_result(task.suite_id, records[0]["result"]) if records else {}
    last = decode_public_result(task.suite_id, records[-1]["result"]) if records else {}

    def clean(rows):
        return [{"name": r["name"], "status": r["status"]} for r in rows]

    return {
        "initialChecks": clean(initial["checks"]),
        "finalChecks": clean(final["checks"]),
        "initialStatus": initial["status"],
        "finalStatus": final["status"],
        "initialSnapshot": initial_id,
        "finalSnapshot": final_id,
        "changed": changed,
        "receiptsMatch": receipts_match,
        "publicVerified": public_verified,
        "faultVerified": bool(
            task.fault and len(records) == 2 and records[-1].get("fault") == task.fault
        ),
        "publicInitialChecks": clean(first.get("checks", [])),
        "publicFinalChecks": clean(last.get("checks", [])),
        "processComplete": process["complete"],
        "budgetExceeded": process["budgetExceeded"],
        "unverifiedEdits": process["unverifiedEdits"],
    }
