"""Cross-module evidence collection with real local broker and receipt artifacts."""

import hashlib
import json
import sys

from pathlib import Path

import pytest

from branch_run_fixture import make_run, write_json

from memos.skill_branch_validation import collector
from memos.skill_branch_validation.tasks import get_task
from memos.skill_drafting.domain import hash_json
from memos.skill_management.domain import validate_report


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")


@pytest.fixture
def trial(tmp_path, monkeypatch):
    fixture = make_run(tmp_path, monkeypatch)
    monkeypatch.setattr(collector, "require_qualification", lambda _: fixture["proof"])
    return fixture


def collect(fixture):
    return collector.collect_trial(
        fixture["candidate"],
        fixture["export_hash"],
        fixture["run"],
        batch_directory=fixture["batch"],
        qualification_directory=fixture["qualification"],
        allowed_root=fixture["allowed"],
    )


def test_real_retained_pipeline_projects_dual_receipts_and_deliveries_without_coverage(trial):
    result = collect(trial)
    validation = validate_report(trial["candidate"], result["validation"])
    facts = result["assessment"]
    assert facts["schemaVersion"] == 2 and facts["collectorVersion"] == "branch-review-1"
    assert facts["validationId"] == validation["validationId"]
    assert facts["initialVerification"]["status"] == "failed"
    assert facts["verification"]["status"] == "passed"
    assert facts["outcome"] == "repair_candidate"
    assert facts["publicVerification"]["initialExitCode"] == 1
    assert facts["publicVerification"]["finalExitCode"] == 0
    assert facts["publicVerification"]["recordsVerified"]
    assert facts["publicVerification"]["deliveriesVerified"]
    assert len(facts["publicVerification"]["deliveryRefs"]) == 2
    assert set(facts["branchCandidates"]) == set(get_task("branch-scoped-table").target_branches)
    assert validation["branchCoverage"] == validation["boundaryCoverage"] == []
    assert facts["captureComplete"] and facts["loadVerified"]


@pytest.mark.parametrize(
    "change", ["task", "export", "initial", "public_record", "delivery", "receipt", "batch"]
)
def test_changed_binding_is_rejected_after_outer_report_is_rewritten(trial, change):
    assert collect(trial)["assessment"]["outcome"] == "repair_candidate"
    path = trial["run"] / "report.json"
    report = json.loads(path.read_text())
    if change == "task":
        report["case_asset_digest"] = "f" * 64
    elif change == "export":
        report["skill_export"]["export_hash"] = "f" * 64
    elif change == "initial":
        report["initial_snapshot"] = report["candidate_snapshot"]
    elif change == "public_record":
        report["visible_checks"][0]["result"]["exit_code"] = 0
    elif change == "delivery":
        report["visible_delivery_refs"] = []
    elif change == "receipt":
        receipt_path = Path(report["receipt"])
        receipt = json.loads(receipt_path.read_text())
        receipt["checks"] = []
        write_json(receipt_path, receipt)
        new_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        receipt_path.with_suffix(".sha256").write_text(new_digest + "\n")
        report["receipt_sha256"] = new_digest
    else:
        batch_path = trial["batch"] / "manifest.json"
        manifest = json.loads(batch_path.read_text())
        manifest["units"][1]["taskDigest"] = "f" * 64
        manifest["experimentId"] = hash_json(
            {k: v for k, v in manifest.items() if k != "experimentId"}
        )
        report["experiment_id"] = manifest["experimentId"]
        write_json(batch_path, manifest)
    write_json(path, report)
    with pytest.raises(ValueError):
        collect(trial)


def test_no_skill_calibration_cannot_be_imported_as_candidate_evidence(tmp_path, monkeypatch):
    fixture = make_run(tmp_path, monkeypatch, group="none")
    monkeypatch.setattr(collector, "require_qualification", lambda _: fixture["proof"])
    with pytest.raises(ValueError):
        collect(fixture)


def test_coherent_public_failure_preserves_hidden_pass_but_never_confirms_repair(
    tmp_path, monkeypatch
):
    fixture = make_run(tmp_path, monkeypatch, public_regression=True)
    monkeypatch.setattr(collector, "require_qualification", lambda _: fixture["proof"])
    result = collect(fixture)
    facts = result["assessment"]
    assert facts["verification"]["status"] == "passed"
    assert facts["publicVerification"]["finalExitCode"] == 1
    assert facts["outcome"] != "repair_candidate"
    assert result["validation"]["taskResult"]["accepted"] is False
    assert result["validation"]["branchCoverage"] == []


@pytest.mark.parametrize("change", ["session_model", "session_tools", "reported_model"])
def test_context_success_flag_cannot_override_rehashed_native_runtime_facts(trial, change):
    assert collect(trial)["assessment"]["outcome"] == "repair_candidate"
    path = trial["run"] / "report.json"
    report = json.loads(path.read_text())
    event_path = trial["run"] / "events.jsonl"
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    if change in {"session_model", "session_tools"}:
        session = next(event for event in events if event["kind"] == "session_init")
        if change == "session_model":
            session["payload"]["model"] = "different-native-model"
        else:
            session["payload"]["tools"].append("Bash")
        report["capture"]["discovered"] = [session]
    else:
        result = next(event for event in events if event["kind"] == "native_result")
        result["payload"]["modelUsage"] = {"different-native-model": {"inputTokens": 1}}
        report["capture"]["native_result"] = result["payload"]
        report["reported_models"] = ["different-native-model"]
    # Keep every copied observation and the outer hash coherent. Only a fresh
    # check against the frozen runtime can reject the stale success assertion.
    event_path.write_text("".join(json.dumps(event) + "\n" for event in events))
    report["events_sha256"] = hashlib.sha256(event_path.read_bytes()).hexdigest()
    assert report["context_matches"] is True
    write_json(path, report)
    with pytest.raises(ValueError):
        collect(trial)


@pytest.mark.parametrize("delivery_mode", ["missing", "wrong"])
def test_actual_incomplete_delivery_is_retained_without_becoming_repair_evidence(
    tmp_path, monkeypatch, delivery_mode
):
    fixture = make_run(tmp_path, monkeypatch, delivery_mode=delivery_mode)
    monkeypatch.setattr(collector, "require_qualification", lambda _: fixture["proof"])
    result = collect(fixture)
    facts = result["assessment"]
    assert facts["outcome"] == "incomplete"
    assert facts["verification"]["status"] == "passed"
    assert facts["publicVerification"]["recordsVerified"] is True
    assert facts["publicVerification"]["deliveriesVerified"] is False
    assert facts["captureComplete"] is False
    assert facts["loadVerified"] is False
    assert result["validation"]["status"] == "unknown"
    assert result["validation"]["branchCoverage"] == []
