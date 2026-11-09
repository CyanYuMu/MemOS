"""Healthy Dream coverage requires actual observations and ordered declarations."""

import copy

import pytest

from c2_governance_fixture import c2_facts

from memos.skill_branch_c2 import governance
from memos.skill_governance.repository import _contract_for
from memos.skill_management.domain import ManagementError


@pytest.fixture(autouse=True)
def synthetic_collector(monkeypatch):
    monkeypatch.setattr(governance, "_collector_digest", lambda: "6" * 64)


def sealed_result(c, v, a, d):
    return governance.adjudicate_assessment(c, v, governance.validate_assessment(c, v, a), d)


def test_two_checkpoints_support_four_reviewed_c2_branches_without_dream_repair():
    c, v, a, d = c2_facts()
    result = sealed_result(c, v, a, d)
    assert result["branchCoverage"] == sorted(a["branchCandidates"])
    assert "verify_scoped_reads#1" in result["branchCoverage"]
    assert "repair_dream_update#0" not in result["branchCoverage"]
    assert result["repairConfirmed"] and result["newIndependentSamples"] == 0
    assert _contract_for(a) is governance


@pytest.mark.parametrize("field", ["hidden", "public"])
@pytest.mark.parametrize(
    "change", ["dream_failure", "wrong_scoped_failure", "missing", "duplicate"]
)
def test_initial_qualification_requires_exact_scoped_defect_and_healthy_dream(field, change):
    c, v, a, d = c2_facts()
    rows = (
        a["initialVerification"]["checks"]
        if field == "hidden"
        else a["processVerification"]["checkpoints"][0]["checks"]
    )
    if change == "dream_failure":
        next(row for row in rows if row["name"] == "dream.caller_contract")["status"] = "failed"
        if field == "public":
            a["processVerification"]["checkpoints"][0]["groupStatus"]["dream"] = "failed"
    elif change == "wrong_scoped_failure":
        next(row for row in rows if row["name"] == "scoped.caller_contract")["status"] = "failed"
    elif change == "missing":
        rows.pop()
    else:
        rows.append(copy.deepcopy(rows[-1]))
    a["outcome"] = governance.assessment_outcome(a)
    assert a["outcome"] == "incomplete"
    with pytest.raises(ManagementError):
        sealed_result(c, v, a, d)


@pytest.mark.parametrize(
    "change",
    [
        "late_repair",
        "early_verify",
        "reversed_verify",
        "missing_transition",
        "missing_dream_checks",
        "wrong_branch",
        "no_edit_action",
        "missing_condition",
    ],
)
def test_c2_branch_claims_cannot_be_inferred_from_final_green(change):
    c, v, a, d = c2_facts()
    claim = d["branches"][2]
    d["branches"] = [claim]
    events = a["events"]
    if change == "late_repair":
        claim = c2_facts()[3]["branches"][1]
        d["branches"] = [claim]
        event = next(e for e in events if e["id"] == "repair_scoped_reads_decl")
        events.remove(event)
        events.append(event)
    elif change == "early_verify":
        event = next(e for e in events if e["id"] == "verify_scoped_reads_decl")
        events.remove(event)
        events.insert(0, event)
    elif change == "reversed_verify":
        event = next(e for e in events if e["id"] == "verify_scoped_reads_decl")
        events.remove(event)
        events.insert(-1, event)
    elif change == "missing_transition":
        claim["actionRefs"] = ["verify_scoped_reads_decl", "exit"]
    elif change == "missing_dream_checks":
        claim["checkRefs"] = [ref for ref in claim["checkRefs"] if ":scoped." in ref]
    elif change == "wrong_branch":
        claim["branchId"] = "verify_scoped_reads#0"
    elif change == "no_edit_action":
        d["branches"] = [c2_facts()[3]["branches"][1]]
        d["branches"][0]["actionRefs"] = ["repair_scoped_reads_decl"]
    else:
        claim["conditionRefs"] = ["check1_result"]
    with pytest.raises(ManagementError):
        sealed_result(c, v, a, d)


def test_behavior_repair_and_usage_are_separate():
    c, v, a, d = c2_facts()
    d.update(usageDecision="misused", branches=[])
    assert sealed_result(c, v, a, d)["repairConfirmed"]


def test_observed_dream_regression_then_repair_keeps_behavior_but_never_c2_coverage():
    c, v, a, d = c2_facts()
    process = a["processVerification"]
    first, final = process["checkpoints"]
    middle = copy.deepcopy(final)
    middle.update(snapshot="a" * 64)
    middle["checks"][-1]["status"] = "failed"
    middle["groupStatus"]["dream"] = "failed"
    final.update(
        checkIndex=3,
        cyclesUsed=2,
        startedEvent="check3_start",
        completedEvent="check3_result",
        editEventRefs=["edit_restore"],
    )
    process.update(cyclesUsed=2, checkpoints=[first, middle, final])
    insert_at = next(i for i, e in enumerate(a["events"]) if e["id"] == "verify_scoped_reads_decl")
    a["events"][insert_at:insert_at] = [
        {"id": "edit_restore", "kind": "tool_started", "value": {"display": {"tool_name": "Edit"}}},
        {"id": "restore_done", "kind": "tool_completed", "value": {"display": {"is_error": False}}},
        {
            "id": "check3_start",
            "kind": "tool_started",
            "value": {"display": {"tool_name": "mcp__memos_run__run_visible_checks"}},
        },
        {
            "id": "check3_result",
            "kind": "tool_completed",
            "value": {"display": {"is_error": False}},
        },
    ]
    a["publicVerification"]["deliveryRefs"] = [
        {key: cp[key] for key in ("checkIndex", "startedEvent", "completedEvent")}
        for cp in process["checkpoints"]
    ]
    d["eventRefs"] = [event["id"] for event in a["events"]]
    with pytest.raises(ManagementError, match="c2_healthy_dream_path_unverified"):
        sealed_result(c, v, a, d)
    d.update(usageDecision="misused", branches=[])
    assert sealed_result(c, v, a, d)["repairConfirmed"]


def test_wrong_but_well_formed_baseline_is_retained_as_incomplete():
    c, v, a, d = c2_facts()
    a["initialVerification"]["checks"][-1]["status"] = "failed"
    a["outcome"] = governance.assessment_outcome(a)
    assert a["outcome"] == "incomplete"
    d.update(taskDecision="needs_evidence", usageDecision="unknown", branches=[])
    result = sealed_result(c, v, a, d)
    assert not result["repairConfirmed"] and result["branchCoverage"] == []


@pytest.mark.parametrize("exit_code", [7, -9])
def test_fault_facts_are_retained_without_repair_qualification(exit_code):
    c, v, a, d = c2_facts()
    a["publicVerification"]["finalExitCode"] = exit_code
    a["publicVerification"]["recordsVerified"] = False
    a["processVerification"].update(complete=False, issues=["public_result_error"])
    a["outcome"] = governance.assessment_outcome(a)
    assert a["outcome"] == "incomplete"
    d.update(taskDecision="needs_evidence", usageDecision="unknown", branches=[])
    assert not sealed_result(c, v, a, d)["repairConfirmed"]


def test_missing_initial_hidden_checks_cannot_be_hidden_by_incomplete_status():
    c, v, a, _ = c2_facts()
    del a["initialVerification"]["checks"]
    a["outcome"] = "incomplete"
    with pytest.raises(ManagementError):
        governance.validate_assessment(c, v, a)


@pytest.mark.parametrize("exit_code", [True, False])
def test_boolean_public_exit_is_rejected_even_when_incomplete(exit_code):
    c, v, a, _ = c2_facts()
    a["publicVerification"]["finalExitCode"] = exit_code
    a["processVerification"].update(complete=False, issues=["public_result_error"])
    a["outcome"] = "incomplete"
    with pytest.raises(ManagementError):
        governance.validate_assessment(c, v, a)


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "receipt",
        "process",
        "collector",
        "intermediate",
        "bool_cycle",
        "mapping",
        "fake_cycle",
        "missing_edit",
        "foreign_delivery",
    ],
)
def test_c2_rejects_cross_protocol_and_inconsistent_process(change):
    c, v, a, _ = c2_facts()
    if change == "schema":
        a["schemaVersion"] = 3
    elif change == "receipt":
        a["initialVerification"]["protocol"] = "extension-receipt-1"
    elif change == "process":
        a["processVerification"]["version"] = "extension-process-1"
    elif change == "collector":
        a["collectorVersion"] = "extension-review-1"
    elif change == "intermediate":
        a["intermediateVerifications"] = [{"snapshot": "a" * 64}]
    elif change == "bool_cycle":
        a["processVerification"]["cyclesUsed"] = True
    elif change == "mapping":
        a["checkMapping"][0]["group"] = "dream"
    elif change == "fake_cycle":
        a["processVerification"]["checkpoints"][-1]["cyclesUsed"] = 0
    elif change == "missing_edit":
        a["processVerification"]["checkpoints"][-1]["editEventRefs"] = []
    else:
        a["processVerification"]["checkpoints"][-1]["completedEvent"] = "check1_result"
    with pytest.raises(ManagementError):
        governance.validate_assessment(c, v, a)
