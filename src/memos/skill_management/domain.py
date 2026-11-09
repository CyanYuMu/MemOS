"""Management contracts: provenance and task success never imply publication."""

from __future__ import annotations

import copy
import json
import re

from datetime import datetime, timezone

from memos.skill_drafting.domain import hash_json
from memos.skill_drafting.generation import assert_public_json


BOUNDARIES = frozenset({"not_applicable", "needs_evidence", "unresolved"})
TERMINAL_JOBS = frozenset({"passed", "failed", "unknown"})


class ManagementError(ValueError):
    """Safe public categories; transport code must never return driver messages."""

    def __init__(self, code: str, http_status: int = 400, details: dict | None = None):
        self.code = code
        self.http_status = http_status
        self.details = details or {}
        super().__init__(code)


def require(condition, code="invalid_input", status=400, details=None):
    if not condition:
        raise ManagementError(code, status, details)


def text(value, *, maximum=2000):
    require(
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and "\x00" not in value
    )
    return value


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))
    return value


def identity(skill_id, revision):
    require(isinstance(skill_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", skill_id))
    digest(revision)
    return f"{skill_id}@{revision}"


def operation(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value))
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def public_copy(value, *, maximum=2_000_000):
    try:
        assert_public_json(value)
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        require(len(raw.encode()) <= maximum)
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, ManagementError):
            raise
        raise ManagementError("invalid_input") from None


def branches(candidate):
    return {
        f"{rule['rule_id']}#{index}"
        for rule in candidate["rules"]
        for index in range(len(rule["branches"]))
    }


def validate_registration(candidate, manifest, project_id):
    require(isinstance(candidate, dict) and isinstance(manifest, dict))
    identity(candidate.get("skill_id"), candidate.get("revision"))
    require(
        candidate.get("project_id") == project_id and manifest.get("project_id") == project_id,
        "project_mismatch",
        403,
    )
    require(candidate.get("status") == "candidate")
    require(
        candidate.get("content_hash")
        == hash_json({k: v for k, v in candidate.items() if k != "content_hash"}),
        "artifact_mismatch",
        409,
    )
    require(
        manifest.get("candidate_content_hash") == candidate["content_hash"]
        and manifest.get("skill_id") == candidate["skill_id"]
        and manifest.get("revision") == candidate["revision"],
        "artifact_mismatch",
        409,
    )
    digest(manifest.get("manifest_id"))
    require(isinstance(candidate.get("rules"), list) and 0 < len(candidate["rules"]) <= 64)
    require(
        isinstance(candidate.get("requires"), list)
        and isinstance(candidate.get("source_refs"), list)
    )
    require(len(branches(candidate)) > 0)
    for source in candidate["source_refs"]:
        digest(source["source_id"])
    public_copy(candidate)
    public_copy(manifest)


def validate_report(candidate: dict, report: dict) -> dict:
    required = {
        "kind",
        "status",
        "contentHash",
        "exportHash",
        "caseId",
        "caseRevision",
        "lineageId",
        "split",
        "runId",
        "attemptId",
        "taskResult",
        "skillUsage",
        "receiptVerified",
        "loadVerified",
        "branchCoverage",
        "boundaryCoverage",
        "scope",
        "purpose",
    }
    require(
        isinstance(report, dict) and required <= set(report) <= required | {"receipt", "errorCode"}
    )
    result = public_copy(report)
    require(
        isinstance(result["kind"], str) and result["kind"] in {"isolated_dev", "independent_task"}
    )
    require(isinstance(result["status"], str) and result["status"] in TERMINAL_JOBS)
    digest(result["contentHash"])
    digest(result["exportHash"])
    require(result["contentHash"] == candidate["content_hash"], "artifact_mismatch", 409)
    for key in ("caseId", "caseRevision", "lineageId", "runId", "attemptId"):
        text(result[key], maximum=300)
    require(result["split"] == ("dev" if result["kind"] == "isolated_dev" else "holdout"))
    require(
        isinstance(result["purpose"], str) and result["purpose"] in {"actual", "management_test"}
    )
    require(type(result["receiptVerified"]) is bool and type(result["loadVerified"]) is bool)
    require(isinstance(result["taskResult"], dict) and isinstance(result["skillUsage"], dict))
    task, usage = result["taskResult"], result["skillUsage"]
    require(set(task) == {"status", "accepted", "receiptMatches"})
    require(isinstance(task["status"], str) and task["status"] in TERMINAL_JOBS)
    require(type(task["accepted"]) is bool and type(task["receiptMatches"]) is bool)
    require(not task["accepted"] or (task["status"] == "passed" and task["receiptMatches"]))
    usage_fields = {"loading", "capture", "usageReview", "declaredRuleIds"}
    require(usage_fields <= set(usage) <= usage_fields | {"endedAdmission"})
    for key, values in (
        ("loading", {"verified", "unknown"}),
        ("capture", {"complete", "partial", "unknown"}),
        ("usageReview", {"unreviewed"}),
    ):
        require(isinstance(usage[key], str) and usage[key] in values)
    if "endedAdmission" in usage:
        require(usage["endedAdmission"] in ("allowed", "needs_review"))
    declared = usage["declaredRuleIds"]
    require(isinstance(declared, list) and all(isinstance(item, str) for item in declared))
    require(len(declared) == len(set(declared)))
    require(set(declared) <= {rule["rule_id"] for rule in candidate["rules"]})
    require(result["loadVerified"] == (usage["loading"] == "verified"))
    for key, possible in (
        ("branchCoverage", branches(candidate)),
        ("boundaryCoverage", BOUNDARIES),
    ):
        items = result[key]
        require(isinstance(items, list) and all(isinstance(item, str) for item in items))
        require(len(set(items)) == len(items) and set(items) <= possible)
    scope = result["scope"]
    require(isinstance(scope, dict) and set(scope) == {"caseIds", "agent", "mode"})
    require(scope["agent"] == "claude_code" and scope["mode"] == "controlled_task")
    ids = scope["caseIds"]
    require(
        isinstance(ids, list) and 0 < len(ids) <= 100 and all(isinstance(item, str) for item in ids)
    )
    require(len(set(ids)) == len(ids) and result["caseId"] in ids)
    for item in ids:
        text(item, maximum=300)
    receipt = result.get("receipt")
    if result["receiptVerified"]:
        require(isinstance(receipt, dict) and set(receipt) == {"uri", "sha256"})
        require(text(receipt["uri"]).startswith("file:///"))
        digest(receipt["sha256"])
    elif receipt is not None:
        require(isinstance(receipt, dict) and set(receipt) == {"uri", "sha256"})
        text(receipt["uri"])
        digest(receipt["sha256"])
    if result["status"] == "passed":
        require(result["receiptVerified"] and result["loadVerified"])
        require(
            result["taskResult"].get("accepted") is True
            and result["taskResult"].get("receiptMatches") is True
        )
        require(
            usage["capture"] == "complete" and usage.get("endedAdmission", "allowed") == "allowed"
        )
    if result["kind"] == "isolated_dev":
        # Model declarations stay available for review, but they do not become
        # independently verified branch or boundary coverage.
        require(not result["branchCoverage"] and not result["boundaryCoverage"])
    else:
        # An independently labelled run cannot silently reuse its training task
        # or a renamed derivative from the same task family.
        require(
            all(
                source.get("case_id") != result["caseId"]
                and source.get("lineage_id") != result["lineageId"]
                for source in candidate["source_refs"]
            ),
            "source_task_overlap",
            422,
        )
    if "errorCode" in result and result["errorCode"] is not None:
        text(result["errorCode"], maximum=200)
    result.update(skillId=candidate["skill_id"], revision=candidate["revision"])
    result["validationId"] = hash_json(result)
    return result


def qualified_validations(record: dict, validations: list[dict]) -> list[dict]:
    """Actual coverage requires a bound human decision; synthetic tests stay explicit."""
    contributions = record.get("qualification", {}).get("contributions", {})
    current = record.get("currentAssessments", {})
    result = []
    for item in validations:
        validation_id = item.get("validationId")
        if validation_id in contributions:
            contribution = contributions[validation_id]
            result.append(
                {
                    **item,
                    "status": "passed",
                    "receiptVerified": True,
                    "branchCoverage": contribution["branchCoverage"],
                    "boundaryCoverage": contribution["boundaryCoverage"],
                    "scope": {**item["scope"], "caseIds": [item["caseId"]]},
                }
            )
        elif (
            item.get("purpose") == "management_test"
            and validation_id not in current
            and item["kind"] == "independent_task"
            and item["status"] == "passed"
            and item.get("receiptVerified") is True
            and item.get("loadVerified") is True
        ):
            result.append(item)
    return result


def readiness(record: dict, validations: list[dict], jobs: list[dict]) -> dict:
    """Readiness is an automatic gate, never a human approval substitute."""
    candidate = record["candidate"]
    reasons = []
    qualification = record.get("qualification", {})
    hard_block = record["state"] in {"blocked", "paused", "withdrawn"}
    if hard_block:
        reasons.append("revision_" + record["state"])
    open_challenges = [
        item for item in record.get("challenges", []) if item.get("status") == "open"
    ]
    if open_challenges:
        reasons.append("open_challenges")
        hard_block = hard_block or any(item.get("severity") == "block" for item in open_challenges)
    lifecycle = record.get("lifecycle")
    if candidate["requires"] and lifecycle is None:
        reasons.append("unsupported_dependencies")
    if lifecycle and lifecycle["gate"]["status"] != "PASS":
        reasons.extend(lifecycle["gate"]["reasons"])
        hard_block = True
    if not record.get("exportHash"):
        reasons.append("export_missing")
    if any(job["status"] in {"queued", "running"} for job in jobs):
        reasons.append("evaluation_pending")
    # This separate cache is derived only from a current schema 6 independent
    # adjudication of an expected stop. Raw results and resolvedHistory remain
    # untouched; old unknowns and unreviewed failures still block publication.
    expected_stops = set(qualification.get("verifiedExpectedStopValidationIds", []))
    if any(
        item["status"] == "failed" and item.get("validationId") not in expected_stops
        for item in validations
    ):
        reasons.append("validation_failure_unresolved")
    if any(
        item["status"] == "unknown"
        and item.get("validationId") not in qualification.get("resolvedValidationIds", [])
        and item.get("validationId") not in expected_stops
        for item in validations
    ):
        reasons.append("validation_unknown")
    if qualification.get("counterexamples"):
        reasons.append("counterexample_unresolved")
        hard_block = True
    if qualification.get("seriousViolations"):
        reasons.append("serious_violation_unresolved")
        hard_block = True
    approved = qualified_validations(record, validations)
    if not approved:
        reasons.append("independent_validation_missing")
    else:
        coverage = {branch for item in approved for branch in item["branchCoverage"]}
        boundaries = {boundary for item in approved for boundary in item["boundaryCoverage"]}
        tested_cases = {item["caseId"] for item in approved}
        scope_cases = {case for item in approved for case in item["scope"]["caseIds"]}
        if coverage != branches(candidate):
            reasons.append("branch_coverage_missing")
        if boundaries != BOUNDARIES:
            reasons.append("boundary_coverage_missing")
        if not scope_cases <= tested_cases:
            reasons.append("scope_coverage_missing")
        if any(
            item["contentHash"] != candidate["content_hash"]
            or item["exportHash"] != record["exportHash"]
            for item in approved
        ):
            reasons.append("validation_binding_mismatch")
    return {
        "status": "BLOCK" if hard_block else "NEEDS_EVIDENCE" if reasons else "PASS",
        "reasons": reasons,
    }


def summary(record: dict, validations: list[dict], jobs: list[dict]) -> dict:
    candidate = record["candidate"]
    return {
        "skillId": candidate["skill_id"],
        "revision": candidate["revision"],
        "title": candidate["title"],
        "purpose": candidate["purpose"],
        "tags": copy.deepcopy(candidate["tags"]),
        "state": record["state"],
        "sequence": record["sequence"],
        "readiness": readiness(record, validations, jobs),
        "ruleCount": len(candidate["rules"]),
        "validationCount": len(validations),
        "updatedAt": record.get("updatedAt"),
    }
