"""Receipt-bound automatic scores; semantic review is deliberately not invented."""

from __future__ import annotations

import hashlib
import json
import math
import re

from pathlib import Path

from memos.skill_evaluation.tasks import case_digest, get_case
from memos.skill_evaluation.verifier import check_receipt


GROUPS = ("none", "raw_memory", "skill")
SCORING_VERSION = "p4.1"


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError("experiment_invalid_report")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("experiment_invalid_report")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _bound_receipt(root: Path, report: dict, case) -> tuple[bool, dict | None]:
    """The report's success bit cannot stand in for a fresh receipt binding check."""
    try:
        raw_path = Path(report["receipt"])
        if raw_path.is_symlink():
            return False, None
        receipt_path = raw_path.resolve()
        if not receipt_path.is_relative_to(root / "validation"):
            return False, None
        receipt = _read(receipt_path)
        context = report["run_context"]
        if (
            _sha(receipt_path) != report.get("receipt_sha256")
            or receipt.get("asset_digest") != case_digest(case)
            or receipt.get("case", {}).get("case_id") != case.case_id
            or receipt.get("run_id") != context.get("run_id")
            or receipt.get("attempt_id") != context.get("attempt_id")
            or receipt.get("initial_snapshot") != report.get("initial_snapshot")
            or receipt.get("candidate_snapshot") != report.get("candidate_snapshot")
        ):
            return False, receipt
        binding = check_receipt(receipt_path, root / "candidate", root / "validation")
        return binding.matches, receipt
    except (KeyError, OSError, ValueError, TypeError):
        return False, None


def score_run(report_path: Path, *, group: str, expected_case_digest: str) -> dict:
    """Score one trusted host run while keeping incomplete attempts in the denominator."""
    report_path = Path(report_path)
    report = _read(report_path)
    root = report_path.resolve().parent
    if group not in GROUPS or report.get("group") != group:
        raise ValueError("experiment_group_mismatch")
    context = report.get("run_context", {})
    case = get_case(context.get("case_id"))
    if (
        case.split not in {"dev", "holdout"}
        or case_digest(case) != expected_case_digest
        or report.get("case_asset_digest") != expected_case_digest
        or context.get("case_revision") != case.revision
        or context.get("lineage_id") != case.lineage_id
        or context.get("split") != case.split
    ):
        raise ValueError("experiment_task_digest_mismatch")
    bound, receipt = _bound_receipt(root, report, case)
    receipt_status = receipt.get("status") if bound else None
    accepted = bound and receipt_status == "passed" and report.get("task_accepted") is True
    capture = report.get("capture", {})
    execution = report.get("execution", {})
    native = capture.get("native_result") or {}
    loading = report.get("loading", {}).get("status", "unknown")
    reasons = []
    try:
        events_path = root / "events.jsonl"
        events_ok = not events_path.is_symlink() and _sha(events_path) == report.get(
            "events_sha256"
        )
    except OSError:
        events_ok = False
    if not bound:
        reasons.append("receipt_unverified")
    if not events_ok:
        reasons.append("events_unverified")
    if capture.get("status") != "complete":
        reasons.append("capture_incomplete")
    if report.get("context_matches") is not True:
        reasons.append("runtime_context_mismatch")
    if loading != ("verified" if group == "skill" else "not_requested"):
        reasons.append("loading_unverified")
    if group == "raw_memory" and report.get("experience", {}).get("readObserved") is not True:
        reasons.append("raw_memory_read_unverified")
    for reason in capture.get("reasons", []):
        if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,100}", reason):
            reasons.append(reason)
    status = "unknown"
    # Agent timeouts and exhausted budgets are not infrastructure exclusions.
    timed_out = execution.get("timed_out") is True
    if timed_out:
        status = "failed"
        reasons.append("agent_timeout")
    elif not reasons:
        if execution.get("returncode") != 0 or capture.get("native_outcome") != "reported_success":
            status = "failed"
            reasons.append("agent_execution_failed")
        elif receipt_status == "passed" and accepted:
            status = "passed"
        elif receipt_status == "failed":
            status = "failed"
            reasons.append("behavior_acceptance_failed")
        elif receipt_status == "needs_review":
            reasons.append("manual_boundary_pending")
        else:
            reasons.append("acceptance_unknown")
    violations = receipt.get("workspace_policy_violations", []) if receipt else []
    if bound and violations:
        status = "failed"
        reasons.append("workspace_policy_violation")
    declarations = sorted(
        {
            item.get("payload", {}).get("rule_id")
            for item in capture.get("rule_declarations", [])
            if isinstance(item, dict) and isinstance(item.get("payload", {}).get("rule_id"), str)
        }
    )
    return {
        "scoringVersion": SCORING_VERSION,
        "group": group,
        "caseId": case.case_id,
        "caseRevision": case.revision,
        "lineageId": case.lineage_id,
        "runId": context.get("run_id"),
        "attemptId": context.get("attempt_id"),
        "assessmentKind": case.assessment_kind,
        "status": status,
        "repairPass": None
        if case.assessment_kind == "manual_boundary"
        else int(status == "passed"),
        "boundaryPass": None,
        "branchSupported": None,
        "criticalViolation": None,
        "taskAccepted": accepted,
        "receiptVerified": bound,
        "loading": loading,
        "capture": capture.get("status", "unknown"),
        "declaredRuleIds": declarations,
        "elapsedSeconds": _number(execution.get("elapsed_seconds")),
        "reportedCostUsd": _number(native.get("total_cost_usd")),
        "toolCalls": _number(capture.get("tool_action_count")),
        "manualReview": "pending",
        "manualRubric": list(case.manual_rubric),
        "policyViolationCount": len(violations),
        "failureReasons": list(dict.fromkeys(reasons)),
    }


def aggregate(rows: list[dict]) -> dict:
    """Seven planned tasks, paired by task; no outcome or missing cost is silently dropped."""
    seen = set()
    for row in rows:
        key = row["group"], row["caseId"]
        if key in seen or row["group"] not in GROUPS:
            raise ValueError("experiment_duplicate_score")
        seen.add(key)
    groups = []
    for group in GROUPS:
        items = [row for row in rows if row["group"] == group]
        primary = [row for row in items if row["assessmentKind"] == "repair"]
        boundary = [row for row in items if row["assessmentKind"] != "repair"]
        costs = [_number(row.get("reportedCostUsd")) for row in items]
        elapsed = [_number(row.get("elapsedSeconds")) for row in items]
        known_cost = sum(value for value in costs if value is not None)
        groups.append(
            {
                "group": group,
                "completed": len(items),
                "planned": 7,
                "primaryRepair": {
                    "passed": sum(row.get("repairPass") == 1 for row in primary),
                    "denominator": sum(row["status"] != "infra_invalid" for row in primary),
                    "planned": 3,
                },
                "boundary": {
                    "taskSolved": sum(row.get("taskAccepted") is True for row in boundary),
                    "planned": 4,
                    "pendingReview": sum(row.get("boundaryPass") is None for row in boundary),
                },
                "unknown": sum(row["status"] == "unknown" for row in items),
                "infraInvalid": sum(row["status"] == "infra_invalid" for row in items),
                "elapsedSeconds": sum(value for value in elapsed if value is not None),
                "unknownElapsedCount": sum(value is None for value in elapsed),
                "successfulElapsedSeconds": sum(
                    _number(row.get("elapsedSeconds")) or 0
                    for row in items
                    if row["status"] == "passed"
                ),
                "reportedCostUsd": known_cost
                if all(value is not None for value in costs)
                else None,
                "knownCostSubtotalUsd": known_cost,
                "unknownCostCount": sum(value is None for value in costs),
                "successfulReportedCostUsd": (
                    sum(row["reportedCostUsd"] for row in items if row["status"] == "passed")
                    if all(
                        _number(row.get("reportedCostUsd")) is not None
                        for row in items
                        if row["status"] == "passed"
                    )
                    else None
                ),
            }
        )
    paired = []
    for case_id in sorted({row["caseId"] for row in rows}):
        by_group = {row["group"]: row for row in rows if row["caseId"] == case_id}
        complete = set(by_group) == set(GROUPS) and all(
            row["status"] != "infra_invalid" for row in by_group.values()
        )
        comparison = "unknown"
        if complete and all(row["status"] in {"passed", "failed"} for row in by_group.values()):
            left, right = by_group["skill"]["repairPass"], by_group["none"]["repairPass"]
            if left is not None and right is not None:
                comparison = "improved" if left > right else "regressed" if left < right else "tied"
        paired.append(
            {
                "caseId": case_id,
                "complete": complete,
                "skillVsNone": comparison,
                "groups": {
                    key: {"status": row["status"], "repairPass": row.get("repairPass")}
                    for key, row in by_group.items()
                },
            }
        )
    return {
        "planned": 21,
        "completed": len(rows),
        "groups": groups,
        "paired": paired,
        "completePairedTasks": sum(item["complete"] for item in paired),
        "manualReview": "pending",
        "independentTaskCount": 7,
    }
