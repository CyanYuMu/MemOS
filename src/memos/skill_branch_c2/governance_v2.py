"""Schema-five review separates execution-plan qualification from patch correctness.

The schema-four producer and its real records remain immutable. Only its pure
fact validators and per-branch predicate are reused; no relabelled schema-four
assessment is validated or represented as historical evidence.
"""

from __future__ import annotations

from memos.skill_branch_c2 import execution_policy
from memos.skill_branch_c2 import governance as prior
from memos.skill_branch_c2.tasks import get_suite, suite_digest
from memos.skill_branch_c2.verifier import PROTOCOL
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION
from memos.skill_drafting.domain import hash_json
from memos.skill_governance.domain import _enum, _refs, validate_judgment_shape
from memos.skill_management.domain import branches, digest, public_copy, require, text


def _collector_digest():
    from memos.skill_branch_c2.collector_v2 import collector_digest

    return collector_digest()


def assessment_outcome(value):
    # A rejected host calibration cannot turn a correct patch into a failed patch.
    return prior.assessment_outcome(value)


_UNIT_KEYS = {
    "unit",
    "group",
    "runId",
    "attemptId",
    "reportSha256",
    "eventsSha256",
    "initialReceiptSha256",
    "receiptSha256",
    "status",
    "taskAccepted",
    "captureComplete",
    "loading",
    "processComplete",
    "maxCycles",
    "cyclesUsed",
    "budgetExceeded",
    "unverifiedEdits",
    "policyMatches",
    "qualified",
    "issues",
}


def _validate_execution(value, validation):
    plan = value["executionQualification"]
    require(
        isinstance(plan, dict)
        and set(plan)
        == {
            "protocol",
            "policyDigest",
            "batchProtocol",
            "batchId",
            "manifestSha256",
            "calibration",
            "trial",
            "planQualified",
            "planIssues",
        },
        "execution_plan_invalid",
    )
    require(
        plan["protocol"] == "c2-plan-1"
        and plan["batchProtocol"] == "c2-batch-2"
        and plan["policyDigest"] == execution_policy.policy_identity()["digest"]
        and plan["batchId"] == value["batchId"],
        "execution_plan_binding",
        409,
    )
    digest(plan["manifestSha256"])
    for key, group in (("calibration", "none"), ("trial", "skill")):
        unit = plan[key]
        require(isinstance(unit, dict) and set(unit) == _UNIT_KEYS, "execution_unit_invalid")
        require(
            unit["unit"] == "c2-" + group and unit["group"] == group, "execution_unit_binding", 409
        )
        for field in ("runId", "attemptId"):
            text(unit[field], maximum=300)
        for field in ("reportSha256", "eventsSha256", "initialReceiptSha256", "receiptSha256"):
            digest(unit[field])
        _enum(unit["status"], {"passed", "failed", "incomplete"})
        _enum(unit["loading"], {"not_requested", "verified", "unknown"})
        for field in ("maxCycles", "cyclesUsed"):
            require(type(unit[field]) is int and unit[field] >= 0, "execution_unit_invalid")
        for field in (
            "taskAccepted",
            "captureComplete",
            "processComplete",
            "budgetExceeded",
            "unverifiedEdits",
            "policyMatches",
            "qualified",
        ):
            require(type(unit[field]) is bool, "execution_unit_invalid")
        issues = execution_policy.unit_issues(unit)
        require(
            unit["issues"] == issues and unit["qualified"] is (not issues),
            "execution_unit_qualification",
            409,
        )
    require(
        plan["calibration"]["runId"] != plan["trial"]["runId"]
        and plan["calibration"]["attemptId"] != plan["trial"]["attemptId"],
        "execution_units_not_distinct",
        409,
    )
    trial = plan["trial"]
    expected = {
        "runId": value["runId"],
        "attemptId": value["attemptId"],
        "reportSha256": value["reportSha256"],
        "eventsSha256": value["eventsSha256"],
        "initialReceiptSha256": value["initialVerification"]["receiptSha256"],
        "receiptSha256": value["verification"]["receiptSha256"],
        "taskAccepted": validation["taskResult"]["accepted"],
        "captureComplete": value["captureComplete"],
        "processComplete": value["processVerification"]["complete"],
        **{
            field: value["processVerification"][field]
            for field in ("maxCycles", "cyclesUsed", "budgetExceeded", "unverifiedEdits")
        },
    }
    require(
        all(trial[field] == fact for field, fact in expected.items())
        and (trial["loading"] == "verified") is value["loadVerified"]
        and (validation["status"] == "passed")
        is (
            trial["status"] == "passed"
            and trial["taskAccepted"]
            and trial["captureComplete"]
            and trial["loading"] == "verified"
        ),
        "execution_trial_binding",
        409,
    )
    issues = execution_policy.plan_issues(plan["calibration"], trial)
    require(
        type(plan["planQualified"]) is bool
        and plan["planIssues"] == issues
        and plan["planQualified"] is (not issues),
        "execution_plan_qualification",
        409,
    )


def validate_assessment(candidate: dict, validation: dict, assessment: dict) -> dict:
    """Validate host-derived schema five while preserving older producer contracts."""
    require(
        isinstance(assessment, dict) and set(assessment) == prior._KEYS | {"executionQualification"}
    )
    value = public_copy(assessment)
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 5)
    require(value["collectorVersion"] == "c2-review-2")
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
    _enum(value["originalStatus"], prior._STATUSES)
    final = value["verification"]
    require(
        isinstance(final, dict)
        and set(final)
        == {"status", "receiptSha256", "protocol", "matches", "sameFinalPatch", "posthoc"}
    )
    _enum(final["status"], prior._STATUSES)
    digest(final["receiptSha256"])
    require(final["protocol"] == PROTOCOL and final["posthoc"] is False)
    require(value["originalStatus"] == final["status"])
    for key in ("matches", "sameFinalPatch"):
        require(type(final[key]) is bool)
    initial = value["initialVerification"]
    require(
        isinstance(initial, dict)
        and set(initial)
        == {"status", "receiptSha256", "protocol", "matches", "candidateSnapshot", "checks"}
    )
    _enum(initial["status"], prior._STATUSES)
    digest(initial["receiptSha256"])
    digest(initial["candidateSnapshot"])
    require(initial["protocol"] == PROTOCOL and type(initial["matches"]) is bool)
    initial_states = prior._check_rows(
        initial["checks"], task, complete=initial["status"] != "error"
    )
    if initial["status"] != "error":
        require("error" not in initial_states.values())
        require(
            (initial["status"] == "passed")
            is all(status == "passed" for status in initial_states.values()),
            "c2_initial_status_mismatch",
            409,
        )
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
        _enum(check["status"], prior._STATUSES)
        names.append(check["name"])
    if final["status"] != "error":
        require(names == [row["id"] for row in task.checks])
        require(all(check["status"] in {"passed", "failed"} for check in checks))
    if final["status"] == "passed":
        require(all(check["status"] == "passed" for check in checks))
    require(value["checkMapping"] == list(task.checks), "extension_check_mapping_changed", 409)
    events = prior._validate_events(value["events"])
    prior._validate_public(value["publicVerification"], events)
    prior._validate_process(value, events, task)
    prior._validate_intermediates(value, task)
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
    _validate_execution(value, validation)
    value["assessmentId"] = hash_json(value)
    return value


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
        require(
            assessment["executionQualification"]["planQualified"], "execution_plan_unqualified", 422
        )
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
        prior._validate_claim(assessment, claim, events)
        coverage.append(claim["branchId"])
    return {
        "assessmentId": assessment["assessmentId"],
        "validationId": validation["validationId"],
        "decision": decision,
        "taskResolved": confirmed,
        "repairConfirmed": confirmed,
        "resolvedHistory": False,
        "qualifiedIndependent": assessment["executionQualification"]["planQualified"]
        and confirmed
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
