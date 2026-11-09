"""Versioned host execution limits, separate from whether the patch is correct."""

from __future__ import annotations

import hashlib

from pathlib import Path

from memos.skill_drafting.domain import hash_json


VERSION = "c2-execution-policy-1"
PLAN_VERSION = "c2-plan-1"
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
        issues.append("behavior_not_passed")
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
