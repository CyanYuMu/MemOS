"""Pure evidence/decision contracts; human judgement cannot waive factual binding.

The host evidence collector is trusted to verify execution artifacts. No HTTP
endpoint accepts an assessment. These contracts validate its references and
constrain what a later authenticated human judgement may contribute.
"""

from __future__ import annotations

from memos.skill_drafting.domain import hash_json
from memos.skill_management.domain import (
    BOUNDARIES,
    branches,
    digest,
    public_copy,
    require,
    text,
)


_STATUSES = {"passed", "failed", "error", "needs_review"}
_ASSESSMENT_KEYS = {
    "schemaVersion",
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
    "reportSha256",
    "eventsSha256",
    "candidateSnapshot",
    "captureComplete",
    "loadVerified",
    "behaviorChanged",
    "originalStatus",
    "verification",
    "checks",
    "events",
    "branchCandidates",
    "coveragePlanHash",
    "collectorVersion",
    "collectorDigest",
}
_DECISION_KEYS = {
    "taskDecision",
    "historyDecision",
    "usageDecision",
    "applicability",
    "boundary",
    "violations",
    "reason",
    "eventRefs",
    "branches",
}


def _enum(value, allowed):
    require(isinstance(value, str) and value in allowed)


def _refs(value, allowed, *, nonempty=True):
    require(isinstance(value, list) and (not nonempty or value) and len(value) <= 100)
    require(all(isinstance(item, str) for item in value))
    require(
        len(set(value)) == len(value) and set(value) <= set(allowed), "invalid_evidence_reference"
    )
    return value


def validate_assessment(candidate: dict, validation: dict, assessment: dict) -> dict:
    """Validate a host-owned assessment without inventing missing evidence."""
    require(isinstance(assessment, dict) and set(assessment) == _ASSESSMENT_KEYS)
    value = public_copy(assessment)
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1)
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
    require(value["contentHash"] == candidate["content_hash"], "assessment_binding_mismatch", 409)
    for key in ("reportSha256", "eventsSha256", "candidateSnapshot", "coveragePlanHash"):
        digest(value[key])
    require(value["collectorVersion"] == "host-review-1")
    digest(value["collectorDigest"])
    if value["purpose"] == "actual":
        from memos.skill_governance.collector import collector_digest

        require(value["collectorDigest"] == collector_digest(), "collector_changed", 409)
    for key in ("captureComplete", "loadVerified", "behaviorChanged"):
        require(type(value[key]) is bool)
    _enum(value["originalStatus"], _STATUSES)
    verification = value["verification"]
    require(
        isinstance(verification, dict)
        and set(verification)
        == {
            "status",
            "receiptSha256",
            "protocol",
            "matches",
            "sameFinalPatch",
            "posthoc",
        }
    )
    _enum(verification["status"], _STATUSES)
    digest(verification["receiptSha256"])
    text(verification["protocol"], maximum=100)
    for key in ("matches", "sameFinalPatch", "posthoc"):
        require(type(verification[key]) is bool)
    checks = value["checks"]
    require(isinstance(checks, list) and 0 < len(checks) <= 100)
    names = set()
    for check in checks:
        require(isinstance(check, dict) and set(check) == {"name", "status"})
        text(check["name"], maximum=200)
        _enum(check["status"], {"passed", "failed", "error"})
        require(check["name"] not in names)
        names.add(check["name"])
    events = value["events"]
    require(isinstance(events, list) and len(events) <= 2000)
    ids = set()
    for event in events:
        require(isinstance(event, dict) and set(event) == {"id", "kind", "value"})
        text(event["id"], maximum=300)
        _enum(
            event["kind"], {"tool_started", "tool_completed", "assistant_text", "rule_declaration"}
        )
        require(isinstance(event["value"], dict) and event["id"] not in ids)
        ids.add(event["id"])
    _refs(value["branchCandidates"], branches(candidate), nonempty=False)
    value["assessmentId"] = hash_json(value)
    return value


def validate_judgment_shape(decision: dict) -> dict:
    """Transport-level validation; evidence semantics are checked in the transaction."""
    require(isinstance(decision, dict) and set(decision) == _DECISION_KEYS)
    decision = public_copy(decision, maximum=32768)
    for key, allowed in (
        ("taskDecision", {"repair_confirmed", "stop_confirmed", "failed", "needs_evidence"}),
        ("historyDecision", {"retain", "infra_explained"}),
        ("usageDecision", {"correct", "misused", "not_used", "unknown"}),
        ("applicability", {"in_scope", "out_of_scope", "unknown"}),
        ("violations", {"none", "serious", "unknown"}),
    ):
        _enum(decision[key], allowed)
    require(
        decision["boundary"] is None
        or (isinstance(decision["boundary"], str) and decision["boundary"] in BOUNDARIES)
    )
    text(decision["reason"])
    require(isinstance(decision["eventRefs"], list))
    _refs(decision["eventRefs"], decision["eventRefs"], nonempty=False)
    claims = decision["branches"]
    require(isinstance(claims, list) and len(claims) <= 100)
    for claim in claims:
        require(
            isinstance(claim, dict)
            and set(claim)
            == {
                "branchId",
                "conditionRefs",
                "actionRefs",
                "checkRefs",
                "reason",
            }
        )
        text(claim["branchId"], maximum=200)
        text(claim["reason"])
        for name in ("conditionRefs", "actionRefs", "checkRefs"):
            require(isinstance(claim[name], list))
            _refs(claim[name], claim[name])
    return decision


def adjudicate_assessment(
    candidate: dict, validation: dict, assessment: dict, decision: dict
) -> dict:
    """Compute a contribution; original Validation and experiment scores stay immutable."""
    require(
        assessment.get("assessmentId")
        == hash_json({key: value for key, value in assessment.items() if key != "assessmentId"}),
        "assessment_binding_mismatch",
        409,
    )
    checked = validate_assessment(
        candidate,
        validation,
        {key: value for key, value in assessment.items() if key != "assessmentId"},
    )
    require(checked == assessment, "assessment_binding_mismatch", 409)
    decision = validate_judgment_shape(decision)
    events = {event["id"]: event for event in assessment["events"]}
    _refs(decision["eventRefs"], events, nonempty=decision["taskDecision"] != "needs_evidence")
    verification = assessment["verification"]
    bound = verification["matches"] and verification["sameFinalPatch"]
    confirmed = decision["taskDecision"] in {"repair_confirmed", "stop_confirmed"}
    if confirmed:
        require(
            bound and assessment["captureComplete"] and assessment["loadVerified"],
            "evidence_insufficient",
            422,
        )
        require(decision["violations"] == "none", "evidence_insufficient", 422)
        if decision["taskDecision"] == "repair_confirmed":
            require(
                verification["status"] == "passed" and decision["boundary"] is None,
                "evidence_insufficient",
                422,
            )
        else:
            require(
                verification["status"] in {"passed", "needs_review"}
                and not assessment["behaviorChanged"]
                and decision["boundary"] in BOUNDARIES,
                "evidence_insufficient",
                422,
            )
    explained = decision["historyDecision"] == "infra_explained"
    if explained:
        require(
            validation["status"] == "unknown"
            and assessment["originalStatus"] == "error"
            and verification["posthoc"]
            and bound
            and verification["status"] != "error",
            "history_explanation_unverified",
            422,
        )
    claims = decision["branches"]
    require(isinstance(claims, list) and len(claims) <= 100)
    coverage = []
    for claim in claims:
        require(
            isinstance(claim, dict)
            and set(claim)
            == {
                "branchId",
                "conditionRefs",
                "actionRefs",
                "checkRefs",
                "reason",
            }
        )
        _refs([claim["branchId"]], assessment["branchCandidates"])
        require(claim["branchId"] not in coverage)
        require(
            confirmed
            and decision["usageDecision"] == "correct"
            and decision["applicability"] != "unknown",
            "branch_not_applicable",
            422,
        )
        require(assessment["split"] == "holdout", "independent_evidence_required", 422)
        require(
            all(
                source.get("case_id") != assessment["caseId"]
                and source.get("lineage_id") != assessment["lineageId"]
                for source in candidate["source_refs"]
            ),
            "source_task_overlap",
            422,
        )
        conditions = _refs(claim["conditionRefs"], events)
        actions = _refs(claim["actionRefs"], events)
        _refs(claim["checkRefs"], [check["name"] for check in assessment["checks"]])
        require(
            any(events[ref]["kind"] == "tool_completed" for ref in conditions),
            "observation_missing",
            422,
        )
        require(
            any(events[ref]["kind"] in {"tool_started", "assistant_text"} for ref in actions),
            "action_missing",
            422,
        )
        rule_id, index = claim["branchId"].rsplit("#", 1)
        rule = next(rule for rule in candidate["rules"] if rule["rule_id"] == rule_id)
        target = rule["branches"][int(index)]["target"]
        if decision["applicability"] == "out_of_scope":
            require(
                target == "exit:not_applicable" and decision["boundary"] == "not_applicable",
                "branch_not_applicable",
                422,
            )
        if target.startswith("exit:"):
            expected = target.removeprefix("exit:")
            require(
                (expected == "success" and decision["taskDecision"] == "repair_confirmed")
                or (
                    expected == decision["boundary"]
                    and decision["taskDecision"] == "stop_confirmed"
                ),
                "branch_outcome_mismatch",
                422,
            )
        text(claim["reason"])
        coverage.append(claim["branchId"])
    independent = assessment["split"] == "holdout" and all(
        source.get("case_id") != assessment["caseId"]
        and source.get("lineage_id") != assessment["lineageId"]
        for source in candidate["source_refs"]
    )
    qualified = (
        confirmed and independent and decision["usageDecision"] == "correct" and bool(coverage)
    )
    return {
        "assessmentId": assessment["assessmentId"],
        "validationId": validation["validationId"],
        "decision": decision,
        "taskResolved": confirmed,
        "repairConfirmed": decision["taskDecision"] == "repair_confirmed",
        "resolvedHistory": explained or (confirmed and verification["status"] == "needs_review"),
        "qualifiedIndependent": qualified,
        "branchCoverage": sorted(coverage),
        "boundaryCoverage": [decision["boundary"]] if qualified and decision["boundary"] else [],
        "counterexample": decision["taskDecision"] == "failed"
        and decision["applicability"] == "in_scope",
        "seriousViolation": decision["violations"] == "serious",
        "newIndependentSamples": 0,
    }
