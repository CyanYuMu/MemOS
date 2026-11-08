"""Human decisions cannot manufacture execution evidence or branch eligibility."""

import copy

import pytest

from management_fixtures import candidate_for, report

from memos.skill_drafting.domain import hash_json
from memos.skill_governance.domain import adjudicate_assessment, validate_assessment
from memos.skill_management.domain import ManagementError, validate_report


def facts():
    candidate, _ = candidate_for("synthetic-governance")
    validation = validate_report(candidate, report(candidate, status="unknown"))
    assessment = {
        "schemaVersion": 1,
        "validationId": validation["validationId"],
        "contentHash": validation["contentHash"],
        "exportHash": validation["exportHash"],
        "runId": validation["runId"],
        "attemptId": validation["attemptId"],
        "caseId": validation["caseId"],
        "caseRevision": validation["caseRevision"],
        "lineageId": validation["lineageId"],
        "split": "holdout",
        "purpose": "management_test",
        "reportSha256": "1" * 64,
        "eventsSha256": "2" * 64,
        "candidateSnapshot": "3" * 64,
        "captureComplete": True,
        "loadVerified": True,
        "behaviorChanged": True,
        "originalStatus": "error",
        "verification": {
            "status": "passed",
            "receiptSha256": "4" * 64,
            "protocol": "p41.1",
            "matches": True,
            "sameFinalPatch": True,
            "posthoc": True,
        },
        "checks": [{"name": "behavior", "status": "passed"}],
        "events": [
            {"id": "observe", "kind": "tool_completed", "value": {"output": "contract"}},
            {"id": "act", "kind": "tool_started", "value": {"tool_name": "Edit"}},
            {"id": "say", "kind": "rule_declaration", "value": {"rule_id": "R1"}},
        ],
        "branchCandidates": ["R1#0"],
        "coveragePlanHash": "5" * 64,
    }
    decision = {
        "taskDecision": "repair_confirmed",
        "historyDecision": "infra_explained",
        "usageDecision": "correct",
        "applicability": "in_scope",
        "boundary": None,
        "violations": "none",
        "reason": "Synthetic human decision for transaction tests only",
        "eventRefs": ["observe", "act"],
        "branches": [
            {
                "branchId": "R1#0",
                "conditionRefs": ["observe"],
                "actionRefs": ["act"],
                "checkRefs": ["behavior"],
                "reason": "Observed condition, action and final check",
            }
        ],
    }
    return candidate, validation, assessment, decision


def test_posthoc_explanation_is_distinct_from_repair_and_coverage():
    c, v, a, d = facts()
    original = copy.deepcopy(v)
    a = validate_assessment(c, v, a)
    result = adjudicate_assessment(c, v, a, d)
    assert result["resolvedHistory"] is True
    assert result["taskResolved"] is True
    assert result["repairConfirmed"] is True
    assert result["branchCoverage"] == ["R1#0"]
    assert result["newIndependentSamples"] == 0
    assert v == original and v["status"] == "unknown"


@pytest.mark.parametrize("field", ["captureComplete", "loadVerified"])
def test_human_cannot_approve_missing_execution_or_loading(field):
    c, v, a, d = facts()
    a[field] = False
    with pytest.raises(ManagementError, match="evidence_insufficient"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)


@pytest.mark.parametrize("field", ["matches", "sameFinalPatch"])
def test_human_cannot_approve_unbound_final_patch(field):
    c, v, a, d = facts()
    a["verification"][field] = False
    with pytest.raises(ManagementError, match="evidence_insufficient"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)


@pytest.mark.parametrize(
    "change",
    ["unknown_branch", "not_registered", "declaration_only", "unknown_event", "unknown_check"],
)
def test_branch_claim_requires_registered_branch_and_real_observations(change):
    c, v, a, d = facts()
    branch = d["branches"][0]
    if change == "unknown_branch":
        branch["branchId"] = "R9#0"
    elif change == "not_registered":
        branch["branchId"] = "R1#1"
    elif change == "declaration_only":
        branch["conditionRefs"] = ["say"]
    elif change == "unknown_event":
        branch["actionRefs"] = ["missing"]
    else:
        branch["checkRefs"] = ["missing"]
    with pytest.raises(ManagementError):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)


def test_reasonable_stop_does_not_become_repair_success():
    c, v, a, d = facts()
    a["originalStatus"] = a["verification"]["status"] = "needs_review"
    a["verification"]["posthoc"] = False
    a["behaviorChanged"] = False
    a["checks"][0]["status"] = "failed"
    a["branchCandidates"] = ["R1#3"]
    d.update(taskDecision="stop_confirmed", historyDecision="retain", boundary="unresolved")
    d["branches"][0]["branchId"] = "R1#3"
    result = adjudicate_assessment(c, v, validate_assessment(c, v, a), d)
    assert result["taskResolved"] and result["resolvedHistory"]
    assert not result["repairConfirmed"]
    assert result["boundaryCoverage"] == ["unresolved"]


def test_scope_and_misuse_cannot_approve_repair_branch():
    c, v, a, d = facts()
    d["applicability"] = "out_of_scope"
    with pytest.raises(ManagementError, match="branch_not_applicable"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)
    d["applicability"] = "in_scope"
    d["usageDecision"] = "misused"
    with pytest.raises(ManagementError, match="branch_not_applicable"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)


def test_source_or_dev_cannot_contribute_independent_coverage():
    c, v, a, d = facts()
    c["source_refs"][0]["lineage_id"] = a["lineageId"]
    with pytest.raises(ManagementError, match="source_task_overlap"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)
    c["source_refs"][0].pop("lineage_id")
    v["split"] = a["split"] = "dev"
    with pytest.raises(ManagementError, match="independent_evidence_required"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)


def test_uncertainty_can_be_retained_without_qualifying_evidence():
    c, v, a, d = facts()
    a["captureComplete"] = False
    d.update(
        taskDecision="needs_evidence",
        historyDecision="retain",
        usageDecision="unknown",
        applicability="unknown",
        violations="unknown",
        branches=[],
    )
    result = adjudicate_assessment(c, v, validate_assessment(c, v, a), d)
    assert not result["taskResolved"] and not result["resolvedHistory"]
    assert result["branchCoverage"] == []


def test_assessment_binds_validation_and_has_content_identity():
    c, v, a, _ = facts()
    validated = validate_assessment(c, v, a)
    assert validated["assessmentId"] == hash_json(a)
    a["runId"] = "another-run"
    with pytest.raises(ManagementError, match="assessment_binding_mismatch"):
        validate_assessment(c, v, a)


def test_stop_cannot_hide_changed_behavior_and_invalid_json_types_are_rejected():
    c, v, a, d = facts()
    d.update(taskDecision="stop_confirmed", boundary="unresolved", branches=[])
    with pytest.raises(ManagementError, match="evidence_insufficient"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)
    d["boundary"] = []
    with pytest.raises(ManagementError, match="invalid_input"):
        adjudicate_assessment(c, v, validate_assessment(c, v, a), d)
