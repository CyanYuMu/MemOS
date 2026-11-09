"""New branch evidence cannot borrow legacy facts or turn declarations into coverage."""

import copy

import pytest

from management_fixtures import candidate_for, report

from memos.skill_branch_validation import governance
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION, get_task, task_digest
from memos.skill_drafting.domain import hash_json
from memos.skill_governance import domain as legacy
from memos.skill_governance.repository import _contract_for
from memos.skill_management.domain import ManagementError, validate_report


@pytest.fixture(autouse=True)
def synthetic_collector(monkeypatch):
    monkeypatch.setattr(governance, "_collector_digest", lambda: "6" * 64)


def branch_facts(task_id="branch-scoped-table"):
    task = get_task(task_id)
    candidate, _ = candidate_for("synthetic-branch-review")
    candidate["revision"] = CANDIDATE_REVISION
    candidate["rules"] = [
        {
            "rule_id": name,
            "condition": "Synthetic",
            "action": "Synthetic",
            "branches": [{"when": "Synthetic", "target": target} for target in targets],
        }
        for name, targets in (
            (
                "confirm_contract",
                [
                    "exit:not_applicable",
                    "exit:needs_evidence",
                    "exit:success",
                    "repair_scoped_reads",
                    "repair_dream_update",
                    "handle_unresolved",
                ],
            ),
            (
                "repair_scoped_reads",
                ["verify_scoped_reads", "exit:needs_evidence", "handle_unresolved"],
            ),
            (
                "verify_scoped_reads",
                [
                    "repair_dream_update",
                    "verify_dream_update",
                    "exit:success",
                    "handle_unresolved",
                    "exit:needs_evidence",
                ],
            ),
            (
                "repair_dream_update",
                ["verify_dream_update", "exit:needs_evidence", "handle_unresolved"],
            ),
            ("verify_dream_update", ["exit:success", "exit:needs_evidence", "handle_unresolved"]),
            ("handle_unresolved", ["exit:needs_evidence", "exit:unresolved"]),
        )
    ]
    candidate["content_hash"] = hash_json(
        {k: v for k, v in candidate.items() if k != "content_hash"}
    )
    raw = report(candidate)
    raw.update(
        caseId=task.task_id,
        caseRevision=task.revision,
        lineageId=task.lineage_id,
        purpose="actual",
        branchCoverage=[],
        boundaryCoverage=[],
    )
    raw["scope"]["caseIds"] = [task.task_id]
    rules = [branch.rsplit("#", 1)[0] for branch in task.target_branches]
    raw["skillUsage"]["declaredRuleIds"] = rules
    validation = validate_report(candidate, raw)
    events = [
        {
            "id": "before_start",
            "kind": "tool_started",
            "value": {"display": {"tool_name": "mcp__memos_run__run_visible_checks"}},
        },
        {
            "id": "before_result",
            "kind": "tool_completed",
            "value": {"display": {"result": "initial failed"}},
        },
        {"id": "edit", "kind": "tool_started", "value": {"display": {"tool_name": "Edit"}}},
        {
            "id": "after_start",
            "kind": "tool_started",
            "value": {"display": {"tool_name": "mcp__memos_run__run_visible_checks"}},
        },
        {
            "id": "after_result",
            "kind": "tool_completed",
            "value": {"display": {"result": "final passed"}},
        },
        *[
            {
                "id": rule + "_decl",
                "kind": "rule_declaration",
                "value": {"display": {"rule_id": rule}},
            }
            for rule in rules
        ],
    ]
    plan = {
        "candidateRevision": candidate["revision"],
        "caseId": task.task_id,
        "caseDigest": task_digest(task),
        "eligibleBranches": list(task.target_branches),
    }
    assessment = {
        "schemaVersion": 2,
        "collectorVersion": "branch-review-1",
        "collectorDigest": "6" * 64,
        **{
            key: validation[key]
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
            )
        },
        "reportSha256": "1" * 64,
        "eventsSha256": "2" * 64,
        "candidateSnapshot": "3" * 64,
        "captureComplete": True,
        "loadVerified": True,
        "behaviorChanged": True,
        "originalStatus": "passed",
        "verification": {
            "status": "passed",
            "receiptSha256": "4" * 64,
            "protocol": "branch-receipt-1",
            "matches": True,
            "sameFinalPatch": True,
            "posthoc": False,
        },
        "initialVerification": {
            "status": "failed",
            "receiptSha256": "7" * 64,
            "protocol": "branch-receipt-1",
            "matches": True,
            "candidateSnapshot": "8" * 64,
        },
        "publicVerification": {
            "initialExitCode": 1,
            "finalExitCode": 0,
            "recordsVerified": True,
            "deliveriesVerified": True,
            "deliveryRefs": [
                {
                    "checkIndex": 1,
                    "startedEvent": "before_start",
                    "completedEvent": "before_result",
                },
                {"checkIndex": 2, "startedEvent": "after_start", "completedEvent": "after_result"},
            ],
        },
        "outcome": "repair_candidate",
        "batchId": "9" * 64,
        "taskDigest": task_digest(task),
        "checks": [{"name": name, "status": "passed"} for name in task.required_checks],
        "events": events,
        "branchCandidates": list(task.target_branches),
        "coveragePlanHash": hash_json(plan),
    }
    decision = {
        "taskDecision": "repair_confirmed",
        "historyDecision": "retain",
        "usageDecision": "correct",
        "applicability": "in_scope",
        "boundary": None,
        "violations": "none",
        "reason": "Synthetic review only",
        "eventRefs": [event["id"] for event in events],
        "branches": [
            {
                "branchId": branch,
                "conditionRefs": ["before_result" if i < 2 else "after_result", rules[i] + "_decl"],
                "actionRefs": ["edit" if i < 2 else "after_start"],
                "checkRefs": list(task.required_checks),
                "reason": "Synthetic condition, action and check",
            }
            for i, branch in enumerate(task.target_branches)
        ],
    }
    return candidate, validation, assessment, decision


@pytest.mark.parametrize("task_id", ["branch-scoped-table", "branch-dream-journal"])
def test_exact_core_evidence_supports_only_reviewed_target_branches(task_id):
    c, v, a, d = branch_facts(task_id)
    before = copy.deepcopy(v)
    sealed = governance.validate_assessment(c, v, a)
    assert sealed["assessmentId"] == hash_json(a)
    result = governance.adjudicate_assessment(c, v, sealed, d)
    assert result["branchCoverage"] == sorted(get_task(task_id).target_branches)
    assert result["repairConfirmed"] and result["qualifiedIndependent"]
    assert not result["resolvedHistory"] and result["newIndependentSamples"] == 0
    assert v == before and a.get("assessmentId") is None


@pytest.mark.parametrize(
    "path,value",
    [
        (("captureComplete",), False),
        (("loadVerified",), False),
        (("behaviorChanged",), False),
        (("initialVerification", "matches"), False),
        (("initialVerification", "status"), "passed"),
        (("verification", "matches"), False),
        (("verification", "sameFinalPatch"), False),
        (("publicVerification", "recordsVerified"), False),
        (("publicVerification", "deliveriesVerified"), False),
        (("publicVerification", "initialExitCode"), 0),
        (("publicVerification", "finalExitCode"), 1),
    ],
)
def test_human_cannot_waive_missing_or_failed_automatic_facts(path, value):
    c, v, a, d = branch_facts()
    target = a
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    if path == ("publicVerification", "deliveriesVerified"):
        a["publicVerification"]["deliveryRefs"] = []
    a["outcome"] = governance.assessment_outcome(a)
    sealed = governance.validate_assessment(c, v, a)
    with pytest.raises(ManagementError, match="evidence_insufficient"):
        governance.adjudicate_assessment(c, v, sealed, d)


@pytest.mark.parametrize(
    "change",
    [
        "task",
        "lineage",
        "revision",
        "digest",
        "collector",
        "protocol",
        "plan",
        "delivery",
        "bool_exit",
        "outcome",
        "missing_check",
    ],
)
def test_identity_delivery_and_outcome_cannot_be_relabeled(change):
    c, v, a, _ = branch_facts()
    if change == "task":
        a["caseId"] = "holdout-fixture"
    elif change == "lineage":
        a["lineageId"] = "renamed"
    elif change == "revision":
        c["revision"] = "a" * 64
    elif change == "digest":
        a["taskDigest"] = "a" * 64
    elif change == "collector":
        a["collectorDigest"] = "a" * 64
    elif change == "protocol":
        a["initialVerification"]["protocol"] = "p41.1"
    elif change == "plan":
        a["coveragePlanHash"] = "a" * 64
    elif change == "delivery":
        a["publicVerification"]["deliveryRefs"][0]["completedEvent"] = "edit"
    elif change == "bool_exit":
        a["publicVerification"]["finalExitCode"] = False
    elif change == "outcome":
        a["outcome"] = "failed"
    else:
        a["checks"].pop()
    with pytest.raises(ManagementError):
        governance.validate_assessment(c, v, a)


@pytest.mark.parametrize(
    "change",
    [
        "foreign_branch",
        "uncited_declaration",
        "wrong_rule",
        "declaration_only",
        "out_of_scope",
        "misused",
        "stop",
        "history",
    ],
)
def test_human_branch_claim_requires_local_rule_and_observed_action(change):
    c, v, a, d = branch_facts()
    if change == "foreign_branch":
        d["branches"][0]["branchId"] = "confirm_contract#4"
    elif change == "uncited_declaration":
        d["branches"][0]["conditionRefs"] = ["before_result"]
    elif change == "wrong_rule":
        a["events"][5]["value"]["display"]["rule_id"] = "repair_scoped_reads"
    elif change == "declaration_only":
        d["branches"][0]["conditionRefs"] = ["confirm_contract_decl"]
    elif change == "out_of_scope":
        d["applicability"] = "out_of_scope"
    elif change == "misused":
        d["usageDecision"] = "misused"
    elif change == "stop":
        d["taskDecision"] = "stop_confirmed"
    else:
        d["historyDecision"] = "infra_explained"
    sealed = governance.validate_assessment(c, v, a)
    with pytest.raises(ManagementError):
        governance.adjudicate_assessment(c, v, sealed, d)


def test_missing_evidence_can_be_retained_without_confirming_or_covering():
    c, v, a, d = branch_facts()
    a["captureComplete"] = False
    a["outcome"] = "incomplete"
    d.update(taskDecision="needs_evidence", usageDecision="unknown", branches=[], eventRefs=[])
    result = governance.adjudicate_assessment(c, v, governance.validate_assessment(c, v, a), d)
    assert result["branchCoverage"] == [] and not result["taskResolved"]


@pytest.mark.parametrize("version", [None, True, 0, 6, "2"])
def test_repository_rejects_unknown_or_noninteger_versions(version):
    with pytest.raises(ManagementError, match="unsupported_assessment_version"):
        _contract_for({"schemaVersion": version})


def test_repository_dispatch_preserves_legacy_contract_identity():
    assert _contract_for({"schemaVersion": 1}) is legacy
    assert _contract_for({"schemaVersion": 2}) is governance
