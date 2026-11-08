"""Final patch receipts and conservative denominators drive P4 scoring."""

import hashlib
import json

import pytest

from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_evaluation.verifier import verify_candidate
from memos.skill_experiments.scoring import aggregate, score_run


def report_fixture(tmp_path, *, case_id="holdout-record", variant="reference", group="none"):
    case = get_case(case_id)
    candidate = materialize_case(case_id, tmp_path / "candidate", variant)
    result = verify_candidate(
        case_id, candidate, tmp_path / "validation", run_id="run", attempt_id="attempt"
    )
    receipt = json.loads(result.receipt_path.read_text())
    events = tmp_path / "events.jsonl"
    events.write_text("")
    report = {
        "group": group,
        "experiment_id": "a" * 64,
        "case_asset_digest": case_digest(case),
        "run_context": {
            "case_id": case_id,
            "case_revision": case.revision,
            "lineage_id": case.lineage_id,
            "run_id": "run",
            "attempt_id": "attempt",
            "split": case.split,
        },
        "initial_snapshot": receipt["initial_snapshot"],
        "candidate_snapshot": receipt["candidate_snapshot"],
        "receipt": str(result.receipt_path),
        "receipt_sha256": hashlib.sha256(result.receipt_path.read_bytes()).hexdigest(),
        "events_sha256": hashlib.sha256(events.read_bytes()).hexdigest(),
        "task_accepted": result.status == "passed",
        "context_matches": True,
        "loading": {"status": "verified" if group == "skill" else "not_requested"},
        "execution": {"returncode": 0, "timed_out": False, "elapsed_seconds": 4.2},
        "capture": {
            "status": "complete",
            "native_outcome": "reported_success",
            "reasons": [],
            "rule_declarations": [],
            "tool_action_count": 3,
            "native_result": {"total_cost_usd": 0.1},
        },
        "experience": {"provided": group != "none", "readObserved": group != "none"},
    }
    p = tmp_path / "report.json"
    p.write_text(json.dumps(report))
    return p, report, case


def write(p, report):
    p.write_text(json.dumps(report))


def test_real_receipt_scores_repair_without_inventing_manual_judgments(tmp_path):
    p, _, case = report_fixture(tmp_path)
    score = score_run(p, group="none", expected_case_digest=case_digest(case))
    assert score["status"] == "passed" and score["repairPass"] == 1
    assert score["receiptVerified"] is True
    assert score["boundaryPass"] is score["branchSupported"] is score["criticalViolation"] is None
    assert score["manualReview"] == "pending"


def test_changed_final_patch_cannot_reuse_passed_receipt(tmp_path):
    p, _, case = report_fixture(tmp_path)
    path = tmp_path / "candidate" / case.editable_paths[0]
    path.write_text(path.read_text() + "\n# changed after acceptance\n")
    score = score_run(p, group="none", expected_case_digest=case_digest(case))
    assert score["status"] == "unknown" and score["repairPass"] == 0
    assert score["receiptVerified"] is False


def test_mismatched_run_and_partial_capture_never_pass(tmp_path):
    p, report, case = report_fixture(tmp_path)
    report["run_context"]["attempt_id"] = "other"
    write(p, report)
    assert score_run(p, group="none", expected_case_digest=case_digest(case))["status"] == "unknown"
    report["run_context"]["attempt_id"] = "attempt"
    report["capture"]["status"] = "partial"
    write(p, report)
    score = score_run(p, group="none", expected_case_digest=case_digest(case))
    assert (
        score["status"] == "unknown" and score["taskAccepted"] is True and score["repairPass"] == 0
    )


def test_real_initial_failure_remains_failure(tmp_path):
    p, _, case = report_fixture(tmp_path, variant="initial")
    score = score_run(p, group="none", expected_case_digest=case_digest(case))
    assert score["status"] == "failed" and score["repairPass"] == 0
    assert score["receiptVerified"] is True


def test_boundary_stop_is_not_repair_success(tmp_path):
    p, _, case = report_fixture(tmp_path, case_id="holdout-ambiguous")
    score = score_run(p, group="none", expected_case_digest=case_digest(case))
    assert score["repairPass"] is None and score["boundaryPass"] is None
    assert score["taskAccepted"] is False and score["manualRubric"]


def test_group_task_drift_and_missing_raw_read_are_rejected(tmp_path):
    p, report, case = report_fixture(tmp_path, group="raw_memory")
    with pytest.raises(ValueError):
        score_run(p, group="skill", expected_case_digest=case_digest(case))
    with pytest.raises(ValueError):
        score_run(p, group="raw_memory", expected_case_digest="f" * 64)
    report["experience"]["readObserved"] = False
    write(p, report)
    assert (
        score_run(p, group="raw_memory", expected_case_digest=case_digest(case))["status"]
        == "unknown"
    )


def test_agent_timeout_is_not_infrastructure_invalid(tmp_path):
    p, report, case = report_fixture(tmp_path)
    report["execution"].update(timed_out=True, returncode=-9)
    write(p, report)
    score = score_run(p, group="none", expected_case_digest=case_digest(case))
    assert score["status"] == "failed" and score["repairPass"] == 0


def test_aggregate_keeps_unknown_denominator_and_unknown_cost():
    rows = [
        {
            "group": "none",
            "caseId": "holdout-record",
            "assessmentKind": "repair",
            "status": "passed",
            "repairPass": 1,
            "taskAccepted": True,
            "elapsedSeconds": 2.0,
            "reportedCostUsd": 0.1,
            "boundaryPass": None,
        },
        {
            "group": "none",
            "caseId": "holdout-deadline",
            "assessmentKind": "repair",
            "status": "unknown",
            "repairPass": 0,
            "taskAccepted": False,
            "elapsedSeconds": 5.0,
            "reportedCostUsd": None,
            "boundaryPass": None,
        },
        {
            "group": "none",
            "caseId": "holdout-migration",
            "assessmentKind": "repair",
            "status": "infra_invalid",
            "repairPass": 0,
            "taskAccepted": False,
            "elapsedSeconds": 0.0,
            "reportedCostUsd": None,
            "boundaryPass": None,
        },
        {
            "group": "none",
            "caseId": "holdout-fixture",
            "assessmentKind": "boundary_repair",
            "status": "passed",
            "repairPass": 1,
            "taskAccepted": True,
            "elapsedSeconds": 3.0,
            "reportedCostUsd": 0.2,
            "boundaryPass": None,
        },
    ]
    summary = aggregate(rows)
    none = next(g for g in summary["groups"] if g["group"] == "none")
    assert none["primaryRepair"] == {"passed": 1, "denominator": 2, "planned": 3}
    assert none["boundary"] == {"taskSolved": 1, "planned": 4, "pendingReview": 1}
    assert none["elapsedSeconds"] == 10.0 and none["successfulElapsedSeconds"] == 5.0
    assert none["reportedCostUsd"] is None and none["knownCostSubtotalUsd"] == pytest.approx(0.3)
    assert none["unknownCostCount"] == 2
    assert summary["completePairedTasks"] == 0
