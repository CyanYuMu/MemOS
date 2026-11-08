"""Versioned review contract for the two frozen core branch tasks.

Legacy assessments keep their original producer and semantics. This contract
adds initial-state and public-delivery facts; a declaration alone is never a
verified branch, and human review cannot waive a failed automatic requirement.
"""

from __future__ import annotations

from memos.skill_branch_validation.tasks import CANDIDATE_REVISION, get_task, task_digest
from memos.skill_branch_validation.verifier import PROTOCOL
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
}
_STATUSES = {"passed", "failed", "error"}


def _collector_digest():
    from memos.skill_branch_validation.collector import collector_digest

    return collector_digest()


def assessment_outcome(value: dict) -> str:
    """Project only automatic facts; this is not a human assessment of Skill use."""
    initial, final, public = (
        value["initialVerification"],
        value["verification"],
        value["publicVerification"],
    )
    complete = (
        value["captureComplete"] is True
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
        require(public[key] is None or (type(public[key]) is int and public[key] in (0, 1)))
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
            len(deliveries) >= 2 and indices == list(range(1, len(deliveries) + 1)),
            "invalid_delivery_reference",
        )
    else:
        require(not deliveries, "invalid_delivery_reference")


def validate_assessment(candidate: dict, validation: dict, assessment: dict) -> dict:
    """Validate schema two without reinterpreting a legacy producer or receipt."""
    require(isinstance(assessment, dict) and set(assessment) == _KEYS)
    value = public_copy(assessment)
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 2)
    require(value["collectorVersion"] == "branch-review-1")
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
        task = get_task(value["caseId"])
    except ValueError:
        require(False, "branch_task_mismatch", 409)
    require(
        value["caseRevision"] == task.revision
        and value["lineageId"] == task.lineage_id
        and value["taskDigest"] == task_digest(task),
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
    require(isinstance(checks, list) and len(checks) <= len(task.required_checks))
    names = []
    for check in checks:
        require(isinstance(check, dict) and set(check) == {"name", "status"})
        require(
            isinstance(check["name"], str)
            and check["name"] in task.required_checks
            and check["name"] not in names
        )
        _enum(check["status"], _STATUSES)
        names.append(check["name"])
    if final["status"] != "error":
        require(names == list(task.required_checks))
        require(all(check["status"] in {"passed", "failed"} for check in checks))
    if final["status"] == "passed":
        require(all(check["status"] == "passed" for check in checks))
    events = _validate_events(value["events"])
    _validate_public(value["publicVerification"], events)
    eligible = _refs(value["branchCandidates"], task.target_branches, nonempty=False)
    require(set(eligible) <= branches(candidate))
    require(
        value["coveragePlanHash"]
        == hash_json(
            {
                "candidateRevision": candidate["revision"],
                "caseId": task.task_id,
                "caseDigest": task_digest(task),
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
        rule_id = claim["branchId"].rsplit("#", 1)[0]
        require(
            any(_declares_rule(events[ref], rule_id) for ref in conditions + actions),
            "rule_declaration_missing",
            422,
        )
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
