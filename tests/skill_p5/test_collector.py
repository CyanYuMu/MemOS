"""Synthetic native events plus real OS checks are explicitly not model evidence."""

import json

import pytest

from p5_run_fixture import make_pair

from memos.skill_management.domain import validate_report
from memos.skill_p5 import collector
from memos.skill_p5.governance import validate_assessment


def collect(f):
    return collector.collect_trial(
        f["candidate"],
        f["export_hash"],
        f["run"],
        batch_directory=f["batch"],
        qualification_directory=f["qualification"],
        allowed_root=f["allowed"],
    )


@pytest.mark.parametrize(
    "suite,expected",
    [
        ("update-repair-r1", "repair_candidate"),
        ("update-healthy-r1", "healthy_candidate"),
        ("scope-outside-r1", "stop_candidate"),
        ("contract-missing-r1", "stop_candidate"),
        ("update-unresolved-r1", "unresolved_candidate"),
        ("check-unavailable-r1", "stop_candidate"),
    ],
)
def test_each_outcome_rereads_native_delivery_and_final_patch(
    tmp_path, monkeypatch, suite, expected
):
    f = make_pair(tmp_path, monkeypatch, trial_options={"suite_id": suite})
    monkeypatch.setattr(
        collector, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    result = collect(f)
    a = result["assessment"]
    assert a["schemaVersion"] == 7
    assert a["outcome"] == expected
    assert a["taskOutcome"]["eligible"]
    assert a["executionQualification"]["planQualified"]
    v = validate_report(f["candidate"], result["validation"])
    validate_assessment(f["candidate"], v, a)
    from copy import deepcopy

    from memos.skill_management.domain import ManagementError

    numeric_boolean = deepcopy(a)
    numeric_boolean["taskOutcome"]["eligible"] = 1
    with pytest.raises(ManagementError):
        validate_assessment(f["candidate"], v, numeric_boolean)
    assert not result["validation"]["branchCoverage"]
    if suite in {"update-unresolved-r1", "scope-outside-r1"}:
        assert result["validation"]["taskResult"]["status"] == "failed"
        assert result["validation"]["taskResult"]["accepted"] is False
    if suite == "check-unavailable-r1":
        assert a["publicVerification"]["fault"]["checkIndex"] == 2
        assert a["verification"]["status"] == "passed"
        assert not result["validation"]["taskResult"]["accepted"]


def test_rehashed_report_cannot_assert_unobserved_process_or_hide_final_edit(tmp_path, monkeypatch):
    f = make_pair(tmp_path, monkeypatch)
    monkeypatch.setattr(
        collector, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    path = f["run"] / "report.json"
    report = json.loads(path.read_text())
    report["process_evidence"]["cyclesUsed"] = 0
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError):
        collect(f)


def decision_for(f, assessment):
    kind = assessment["taskOutcome"]["kind"]
    task = {
        "repair": "repair_confirmed",
        "healthy": "healthy_confirmed",
        "unresolved": "unresolved_confirmed",
    }.get(kind, "stop_confirmed")
    boundary = {
        "out_of_scope": "not_applicable",
        "missing_evidence": "needs_evidence",
        "tool_failure": "needs_evidence",
        "unresolved": "unresolved",
    }.get(kind)
    events = assessment["events"]
    refs = [e["id"] for e in events]
    conditions = [e["id"] for e in events if e["kind"] == "tool_completed"]
    checks = [
        f"check:{cp['checkIndex']}:{row['name']}"
        for cp in assessment["processVerification"]["checkpoints"]
        for row in cp["checks"]
    ]
    fault = assessment["publicVerification"]["fault"]
    if fault:
        checks.append(f"fault:{fault['checkIndex']}:{fault['code']}")
    return {
        "taskDecision": task,
        "historyDecision": "retain",
        "usageDecision": "correct",
        "applicability": "out_of_scope" if kind == "out_of_scope" else "in_scope",
        "boundary": boundary,
        "violations": "none",
        "reason": "Synthetic review of actual retained local check receipts.",
        "eventRefs": refs,
        "branches": [
            {
                "branchId": branch,
                "conditionRefs": conditions,
                "actionRefs": refs,
                "checkRefs": checks,
                "reason": "Synthetic rule condition and action were observed.",
            }
            for branch in assessment["branchCandidates"]
        ],
    }


@pytest.mark.parametrize(
    "suite",
    [
        "update-repair-r1",
        "update-healthy-r1",
        "scope-outside-r1",
        "contract-missing-r1",
        "update-unresolved-r1",
        "check-unavailable-r1",
    ],
)
def test_review_distinguishes_repair_health_and_stopping_without_migrating_coverage(
    tmp_path, monkeypatch, suite
):
    from copy import deepcopy

    from memos.skill_management.domain import ManagementError
    from memos.skill_p5.governance import adjudicate_assessment

    f = make_pair(tmp_path, monkeypatch, trial_options={"suite_id": suite})
    monkeypatch.setattr(
        collector, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    pair = collect(f)
    validation = validate_report(f["candidate"], pair["validation"])
    assessment = validate_assessment(f["candidate"], validation, pair["assessment"])
    decision = decision_for(f, assessment)
    contribution = adjudicate_assessment(f["candidate"], validation, assessment, decision)
    assert contribution["qualifiedIndependent"]
    assert contribution["repairConfirmed"] is (suite == "update-repair-r1")
    assert contribution["healthyConfirmed"] is (suite == "update-healthy-r1")
    assert contribution["unresolvedConfirmed"] is (suite == "update-unresolved-r1")
    assert contribution["newIndependentSamples"] == 0
    assert set(contribution["branchCoverage"]) == set(assessment["branchCandidates"])
    bad = deepcopy(decision)
    bad["branches"][0]["actionRefs"] = [
        e["id"] for e in assessment["events"] if e["kind"] != "rule_declaration"
    ]
    with pytest.raises(ManagementError, match="rule_declaration_missing"):
        adjudicate_assessment(f["candidate"], validation, assessment, bad)
    bad = deepcopy(decision)
    bad["branches"][0]["checkRefs"] = ["final:" + c["name"] for c in assessment["checks"]]
    with pytest.raises(ManagementError, match="delivered_check_required"):
        adjudicate_assessment(f["candidate"], validation, assessment, bad)
    if suite != "update-repair-r1":
        bad = deepcopy(decision)
        bad["taskDecision"] = "repair_confirmed"
        bad["boundary"] = None
        with pytest.raises(ManagementError):
            adjudicate_assessment(f["candidate"], validation, assessment, bad)


def test_late_entry_and_target_declarations_do_not_establish_pre_edit_selection(
    tmp_path, monkeypatch
):
    from memos.skill_management.domain import ManagementError
    from memos.skill_p5.governance import _validate_claim

    f = make_pair(tmp_path, monkeypatch)
    monkeypatch.setattr(
        collector, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    assessment = collect(f)["assessment"]
    decision = decision_for(f, assessment)
    declarations = [e for e in assessment["events"] if e["kind"] == "rule_declaration"]
    events = {e["id"]: e for e in assessment["events"] if e["kind"] != "rule_declaration"}
    events.update({e["id"]: e for e in declarations})
    claim = next(c for c in decision["branches"] if c["branchId"] == "check_update_contract#2")
    with pytest.raises(ManagementError, match="selection_after_edit|target_rule_not_observed"):
        _validate_claim(f["candidate"], assessment, claim, events, decision)


@pytest.mark.parametrize("suite", ["update-unresolved-r1", "check-unavailable-r1"])
def test_complete_native_chain_cannot_trade_target_failure_for_new_scoped_regression(
    tmp_path, monkeypatch, suite
):
    from memos.skill_branch_extensions.combined import CALLER

    bad = CALLER.replace("return True", "return False", 1).replace(
        "graph.update_node(target, fields, user_name=cube)",
        'graph.update_node(target, fields, user_name=cube)\n        if content and hasattr(graph, "docs"):\n            graph.docs[target]["fields"]["memory"] = content',
        1,
    )
    f = make_pair(tmp_path, monkeypatch, trial_options={"suite_id": suite, "final_caller": bad})
    monkeypatch.setattr(
        collector, "require_qualification", lambda *a, **k: f["proofs"][k["suite_id"]]
    )
    result = collect(f)
    assessment = result["assessment"]
    assert all(
        row["status"] == "passed"
        for row in assessment["checks"]
        if row["name"].startswith("dream.")
    )
    assert any(
        row["status"] == "failed"
        for row in assessment["checks"]
        if row["name"].startswith("scoped.")
    )
    assert not assessment["taskOutcome"]["eligible"]
    assert "new_business_regression" in assessment["taskOutcome"]["reasons"]
    assert not assessment["executionQualification"]["planQualified"]
