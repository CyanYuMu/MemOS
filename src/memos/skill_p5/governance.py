"""Schema-seven review separates healthy, repaired and stopped task outcomes.

Earlier producers and their actual records remain immutable. This independent
contract distinguishes patch correctness, expected stopping, actual rule use
and publication coverage; no earlier assessment is relabelled or reinterpreted.
"""

from __future__ import annotations

from memos.skill_drafting.domain import hash_json
from memos.skill_governance.domain import _enum, _refs
from memos.skill_governance.domain import validate_judgment_shape as frozen_judgment_shape
from memos.skill_management.domain import branches, digest, public_copy, require, text
from memos.skill_p5 import contracts as prior
from memos.skill_p5 import execution_policy
from memos.skill_p5.tasks import get_suite, suite_digest
from memos.skill_p5.verifier import PROTOCOL


def _collector_digest():
    from memos.skill_p5.collector import collector_digest

    return collector_digest()


def outcome_facts(value):
    initial, final = value["initialVerification"], value["verification"]
    process, public = value["processVerification"], value["publicVerification"]
    checkpoints = process["checkpoints"]
    return {
        "initialChecks": initial["checks"],
        "finalChecks": value["checks"],
        "initialStatus": initial["status"],
        "finalStatus": final["status"],
        "initialSnapshot": initial["candidateSnapshot"],
        "finalSnapshot": value["candidateSnapshot"],
        "changed": value["behaviorChanged"],
        "receiptsMatch": initial["matches"] and final["matches"] and final["sameFinalPatch"],
        "publicVerified": public["recordsVerified"] and public["deliveriesVerified"],
        "faultVerified": public["fault"] is not None,
        "publicInitialChecks": checkpoints[0]["checks"] if checkpoints else [],
        "publicFinalChecks": checkpoints[-1]["checks"] if checkpoints else [],
        "processComplete": process["complete"],
        "budgetExceeded": process["budgetExceeded"],
        "unverifiedEdits": process["unverifiedEdits"],
    }


def assessment_outcome(value):
    actual = execution_policy.task_outcome(value["suiteId"], outcome_facts(value))
    if not actual["eligible"] or not value["captureComplete"] or not value["loadVerified"]:
        return "incomplete"
    if actual["kind"] in {"healthy", "repair"}:
        return actual["kind"] + "_candidate"
    if actual["kind"] == "unresolved" and value["behaviorChanged"]:
        return "unresolved_candidate"
    return "stop_candidate"


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
        plan["protocol"] == "p5-plan-1"
        and plan["batchProtocol"] == "p5-batch-1"
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
            unit["unit"] == ("p5-none" if group == "none" else value["suiteId"] + "-skill")
            and unit["group"] == group,
            "execution_unit_binding",
            409,
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
        "taskAccepted": value["taskOutcome"]["eligible"],
        "captureComplete": value["captureComplete"],
        "processComplete": value["processVerification"]["complete"],
        **{
            field: value["processVerification"][field]
            for field in ("maxCycles", "cyclesUsed", "budgetExceeded", "unverifiedEdits")
        },
    }
    require(
        all(trial[field] == fact for field, fact in expected.items())
        and (trial["loading"] == "verified") is value["loadVerified"],
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
    """Validate host-derived schema seven while preserving older producer contracts."""
    require(
        isinstance(assessment, dict) and set(assessment) == prior._KEYS | {"executionQualification"}
    )
    value = public_copy(assessment)
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 7)
    require(value["collectorVersion"] == "p5-review-1")
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
        value["contentHash"] == candidate["content_hash"],
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
            "release_initial_status_mismatch",
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
    _validate_public(value, events, task)
    prior._validate_process(value, events, task)
    prior._validate_intermediates(value, task)
    eligible = _refs(value["branchCandidates"], branches(candidate), nonempty=False)
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
    _enum(
        value["outcome"],
        {
            "repair_candidate",
            "healthy_candidate",
            "stop_candidate",
            "unresolved_candidate",
            "failed",
            "incomplete",
        },
    )
    task_outcome = value["taskOutcome"]
    require(
        isinstance(task_outcome, dict)
        and set(task_outcome) == {"kind", "eligible", "reasons"}
        and type(task_outcome["eligible"]) is bool
        and isinstance(task_outcome["reasons"], list),
        "task_outcome_invalid",
    )
    require(
        task_outcome == execution_policy.task_outcome(value["suiteId"], outcome_facts(value)),
        "task_outcome_mismatch",
        409,
    )
    require(value["outcome"] == assessment_outcome(value), "assessment_outcome_mismatch", 409)
    _validate_execution(value, validation)
    value["assessmentId"] = hash_json(value)
    return value


def _validate_public(value, events, task):
    public = value["publicVerification"]
    require(isinstance(public, dict) and "fault" in public)
    prior._validate_public({k: v for k, v in public.items() if k != "fault"}, events)
    fault = public["fault"]
    if fault is None:
        return
    require(
        isinstance(fault, dict)
        and set(fault) == {"checkIndex", "code", "recordSha256", "startedEvent", "completedEvent"}
    )
    require(type(fault["checkIndex"]) is int)
    require(
        task.fault is not None and {k: fault[k] for k in ("checkIndex", "code")} == task.fault,
        "unregistered_tool_fault",
        409,
    )
    digest(fault["recordSha256"])
    cp = next(
        (
            c
            for c in value["processVerification"]["checkpoints"]
            if c["checkIndex"] == fault["checkIndex"]
        ),
        None,
    )
    require(
        cp is not None
        and cp["recordSha256"] == fault["recordSha256"]
        and cp["checks"] == []
        and cp["startedEvent"] == fault["startedEvent"]
        and cp["completedEvent"] == fault["completedEvent"],
        "fault_reference_mismatch",
        409,
    )


def validate_judgment_shape(decision):
    """Add outcome names only at the new transport; old transaction enums remain strict."""
    require(isinstance(decision, dict))
    task = decision.get("taskDecision")
    value = public_copy(decision)
    if task in {"healthy_confirmed", "unresolved_confirmed"}:
        value["taskDecision"] = "repair_confirmed"
    value = frozen_judgment_shape(value)
    value["taskDecision"] = task
    return value


def _validate_claim(candidate, assessment, claim, events, decision):
    conditions = _refs(claim["conditionRefs"], events)
    actions = _refs(claim["actionRefs"], events)
    require(
        any(events[r]["kind"] == "tool_completed" for r in conditions), "observation_missing", 422
    )
    rule_id, index = claim["branchId"].rsplit("#", 1)
    rule = next(r for r in candidate["rules"] if r["rule_id"] == rule_id)
    target = rule["branches"][int(index)]["target"]
    declarations = [r for r in actions if prior._declares_rule(events[r], rule_id)]
    require(declarations, "rule_declaration_missing", 422)
    order = {key: i for i, key in enumerate(events)}
    require(
        any(order[c] < order[d] for c in conditions for d in declarations),
        "declaration_observation_order",
        422,
    )
    cp = assessment["processVerification"]["checkpoints"]
    checks = {f"check:{row['checkIndex']}:{c['name']}": row for row in cp for c in row["checks"]}
    fault = assessment["publicVerification"]["fault"]
    if fault:
        checks[f"fault:{fault['checkIndex']}:{fault['code']}"] = next(
            row for row in cp if row["checkIndex"] == fault["checkIndex"]
        )
    final_refs = {"final:" + c["name"] for c in assessment["checks"]}
    _refs(claim["checkRefs"], set(checks) | final_refs)
    public_refs = [r for r in claim["checkRefs"] if r in checks]
    require(public_refs, "delivered_check_required", 422)
    require(
        any(checks[r]["completedEvent"] in set(conditions + actions) for r in public_refs),
        "check_event_reference_missing",
        422,
    )
    require(
        any(
            events[r]["kind"] in {"tool_started", "tool_completed", "assistant_text"}
            for r in actions
        ),
        "action_missing",
        422,
    )
    if not target.startswith("exit:"):
        observed_refs = [ref for ref in conditions if events[ref]["kind"] == "tool_completed"]
        target_declarations = [ref for ref in actions if prior._declares_rule(events[ref], target)]
        require(
            any(
                order[observed] < order[source] < order[target_ref]
                for observed in observed_refs
                for source in declarations
                for target_ref in target_declarations
            ),
            "target_rule_not_observed",
            422,
        )
        edits = [ref for ref, event in events.items() if prior._is_edit(event)]
        if edits:
            first_edit = min(order[ref] for ref in edits)
            selections = [
                ref for ref in declarations if prior._display(events[ref]).get("phase") == "select"
            ]
            targets = [
                ref
                for ref in target_declarations
                if prior._display(events[ref]).get("phase") == "select"
            ]
            require(
                any(
                    order[observed] < order[source] < order[target_ref] < first_edit
                    for observed in observed_refs
                    for source in selections
                    for target_ref in targets
                ),
                "selection_after_edit",
                422,
            )
            require(any(ref in actions for ref in edits), "observed_repair_action_missing", 422)
    if target.startswith("exit:"):
        expected = target.removeprefix("exit:")
        require(
            (
                expected == "success"
                and decision["taskDecision"] in {"repair_confirmed", "healthy_confirmed"}
            )
            or (
                expected == decision["boundary"]
                and decision["taskDecision"] in {"stop_confirmed", "unresolved_confirmed"}
            ),
            "branch_outcome_mismatch",
            422,
        )
        if expected == "success":
            require(
                any(
                    checks[r]["snapshot"] == assessment["candidateSnapshot"]
                    and checks[r]["checks"]
                    and all(c["status"] == "passed" for c in checks[r]["checks"])
                    for r in public_refs
                ),
                "final_public_acceptance_missing",
                422,
            )
        elif assessment["taskOutcome"]["kind"] == "tool_failure":
            require(any(r.startswith("fault:") for r in public_refs), "fault_delivery_missing", 422)
            require(
                any(order[d] < order[fault["startedEvent"]] for d in declarations),
                "fault_rule_order",
                422,
            )
        if assessment["behaviorChanged"]:
            require(
                any(
                    prior._is_edit(events[r]) and any(order[d] < order[r] for d in declarations)
                    for r in actions
                ),
                "observed_repair_action_missing",
                422,
            )
    if decision["applicability"] == "out_of_scope":
        require(target == "exit:not_applicable", "branch_not_applicable", 422)


def adjudicate_assessment(candidate, validation, assessment, decision):
    checked = validate_assessment(
        candidate, validation, {k: v for k, v in assessment.items() if k != "assessmentId"}
    )
    require(checked == assessment, "assessment_binding_mismatch", 409)
    decision = validate_judgment_shape(decision)
    require(decision["historyDecision"] == "retain", "history_explanation_unverified", 422)
    events = {event["id"]: event for event in assessment["events"]}
    _refs(decision["eventRefs"], events, nonempty=decision["taskDecision"] != "needs_evidence")
    wanted = {
        "repair_confirmed": "repair_candidate",
        "healthy_confirmed": "healthy_candidate",
        "stop_confirmed": "stop_candidate",
        "unresolved_confirmed": "unresolved_candidate",
    }
    confirmed = decision["taskDecision"] in wanted
    if confirmed:
        require(
            assessment["outcome"] == wanted[decision["taskDecision"]]
            and decision["violations"] == "none",
            "evidence_insufficient",
            422,
        )
        kind = assessment["taskOutcome"]["kind"]
        expected_boundary = {
            "out_of_scope": "not_applicable",
            "missing_evidence": "needs_evidence",
            "tool_failure": "needs_evidence",
            "unresolved": "unresolved",
        }.get(kind)
        require(decision["boundary"] == expected_boundary, "boundary_mismatch", 422)
        require(
            decision["applicability"] == ("out_of_scope" if kind == "out_of_scope" else "in_scope"),
            "applicability_mismatch",
            422,
        )
    independent = all(
        s.get("case_id") != assessment["caseId"] and s.get("lineage_id") != assessment["lineageId"]
        for s in candidate["source_refs"]
    )
    coverage = []
    for claim in decision["branches"]:
        require(
            confirmed
            and decision["usageDecision"] == "correct"
            and independent
            and assessment["executionQualification"]["planQualified"],
            "branch_not_qualified",
            422,
        )
        _refs([claim["branchId"]], assessment["branchCandidates"])
        require(claim["branchId"] not in coverage)
        _validate_claim(candidate, assessment, claim, events, decision)
        coverage.append(claim["branchId"])
    boundary = decision["boundary"]
    boundary_covered = any(
        next(r for r in candidate["rules"] if r["rule_id"] == b.rsplit("#", 1)[0])["branches"][
            int(b.rsplit("#", 1)[1])
        ]["target"]
        == "exit:" + str(boundary)
        for b in coverage
    )
    return {
        "assessmentId": assessment["assessmentId"],
        "validationId": validation["validationId"],
        "decision": decision,
        "taskResolved": decision["taskDecision"] in {"repair_confirmed", "healthy_confirmed"},
        "repairConfirmed": decision["taskDecision"] == "repair_confirmed",
        "healthyConfirmed": decision["taskDecision"] == "healthy_confirmed",
        "unresolvedConfirmed": decision["taskDecision"] == "unresolved_confirmed",
        "expectedStopConfirmed": confirmed
        and decision["taskDecision"] in {"stop_confirmed", "unresolved_confirmed"}
        and independent
        and decision["usageDecision"] == "correct"
        and bool(coverage)
        and boundary_covered
        and assessment["executionQualification"]["planQualified"],
        "resolvedHistory": False,
        "qualifiedIndependent": confirmed
        and independent
        and decision["usageDecision"] == "correct"
        and bool(coverage)
        and assessment["executionQualification"]["planQualified"],
        "branchCoverage": sorted(coverage),
        "boundaryCoverage": [boundary] if boundary_covered else [],
        "counterexample": decision["taskDecision"] == "failed"
        and decision["applicability"] == "in_scope",
        "seriousViolation": decision["violations"] == "serious",
        "newIndependentSamples": 0,
    }
