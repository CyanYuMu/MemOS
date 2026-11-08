"""New receipts bind each no-fork phase and preserve independent task outcomes."""

import hashlib
import json
import sys

import pytest

from memos.skill_evaluation.tasks import list_cases, materialize_case
from memos.skill_evaluation.verifier import check_receipt as legacy_check
from memos.skill_verification.verifier import check_receipt, verify_candidate


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real macOS sandbox required")


@pytest.mark.parametrize("case", list_cases("holdout"), ids=lambda case: case.case_id)
@pytest.mark.parametrize("variant", ["initial", "reference", "unsafe"])
def test_seven_tasks_use_real_isolation_and_keep_negative_detection(tmp_path, case, variant):
    candidate = materialize_case(case.case_id, tmp_path / "candidate", variant)
    root = tmp_path / "artifacts"
    result = verify_candidate(case.case_id, candidate, root)
    expected = (
        "failed"
        if variant != "reference"
        else ("needs_review" if case.case_id == "holdout-ambiguous" else "passed")
    )
    assert result.status == expected
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["validator_version"].startswith("p41")
    assert len(receipt["phases"]) == (5 if case.case_id == "holdout-fixture" else 1)
    assert receipt["qualified_agent_experience"] is False
    checked = check_receipt(result.receipt_path, candidate, root)
    assert checked.matches, checked.reasons
    assert checked.verification_status == expected
    assert not legacy_check(result.receipt_path, candidate, root).matches


def _receipt(tmp_path, case_id="holdout-fixture"):
    candidate = materialize_case(case_id, tmp_path / "candidate", "reference")
    root = tmp_path / "artifacts"
    result = verify_candidate(case_id, candidate, root)
    assert result.status == "passed"
    return candidate, root, result.receipt_path, json.loads(result.receipt_path.read_text())


def _write_receipt(path, receipt):
    path.write_text(json.dumps(receipt))
    path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize("target", ["program.py", "isolation.sb", "stdout.txt", "execution.json"])
def test_phase_artifacts_cannot_change_after_acceptance(tmp_path, target):
    candidate, root, path, receipt = _receipt(tmp_path)
    artifact = root / receipt["phases"][1]["job"] / target
    artifact.write_bytes(artifact.read_bytes() + b"\nchanged\n")
    assert not check_receipt(path, candidate, root).matches


@pytest.mark.parametrize("field", ["status", "phases", "validator_version", "plan_digest"])
def test_rehashed_receipt_cannot_substitute_its_result_order_or_version(tmp_path, field):
    candidate, root, path, receipt = _receipt(tmp_path)
    if field == "phases":
        receipt[field][1], receipt[field][2] = receipt[field][2], receipt[field][1]
    else:
        receipt[field] = "tampered"
    _write_receipt(path, receipt)
    assert not check_receipt(path, candidate, root).matches


def test_changed_final_patch_invalidates_receipt(tmp_path):
    candidate, root, path, _ = _receipt(tmp_path, "holdout-record")
    with (candidate / "caller.py").open("a") as stream:
        stream.write("\n# after acceptance\n")
    assert not check_receipt(path, candidate, root).matches


def test_each_fixture_order_retains_actual_pytest_stages(tmp_path):
    _, root, _, receipt = _receipt(tmp_path)
    for record, count in zip(receipt["phases"][1:], (1, 1, 2, 2), strict=True):
        audit = json.loads((root / record["job"] / "report.json").read_text())
        assert audit["runner_exit_code"] == 0
        assert len(audit["events"]) == count * 3
        assert all(row[2:] == ["passed", False] for row in audit["events"])


@pytest.mark.parametrize("change", ["drop_event", "skip", "xfail", "relax_profile"])
def test_rehashed_phase_cannot_hide_missing_audit_or_relaxed_isolation(tmp_path, change):
    from memos.skill_verification.verifier import _phase_artifacts

    candidate, root, path, receipt = _receipt(tmp_path)
    record = receipt["phases"][1]
    job = root / record["job"]
    if change == "relax_profile":
        profile = job / "isolation.sb"
        profile.write_text(profile.read_text() + "\n(allow process-fork)\n")
        execution = json.loads((job / "execution.json").read_text())
        execution["isolation"]["profile_sha256"] = hashlib.sha256(profile.read_bytes()).hexdigest()
        (job / "execution.json").write_text(json.dumps(execution))
        record["execution"] = execution
    else:
        report = job / "report.json"
        audit = json.loads(report.read_text())
        if change == "drop_event":
            audit["events"].pop()
        elif change == "skip":
            audit["events"][1][2] = "skipped"
        else:
            audit["events"][1][3] = True
        report.write_text(json.dumps(audit))
    record["artifacts"] = _phase_artifacts(job)
    _write_receipt(path, receipt)
    assert not check_receipt(path, candidate, root).matches


@pytest.mark.parametrize("shortcut", ["skip", "xfail", "hook", "constant"])
def test_fixture_shortcuts_do_not_become_successful_repairs(tmp_path, shortcut):
    candidate = materialize_case("holdout-fixture", tmp_path / "candidate", "reference")
    fixture = candidate / "conftest.py"
    if shortcut in {"skip", "xfail"}:
        fixture.write_text(
            fixture.read_text().replace("return FakeGraph()", f"pytest.{shortcut}('bypass')")
        )
    elif shortcut == "hook":
        fixture.write_text(
            fixture.read_text() + "\ndef pytest_collection_modifyitems(items):\n    items.clear()\n"
        )
    else:
        fake = candidate / "fake_graph.py"
        fake.write_text(fake.read_text().replace("self.remaining.clear()", "pass"))
    result = verify_candidate("holdout-fixture", candidate, tmp_path / "artifacts")
    assert result.status in {"failed", "error"}
    assert check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


def test_missing_oracle_output_and_cancellation_never_pass(tmp_path):
    import threading

    candidate = materialize_case("holdout-record", tmp_path / "candidate", "reference")
    cancel = threading.Event()
    cancel.set()
    result = verify_candidate(
        "holdout-record", candidate, tmp_path / "artifacts", cancellation_event=cancel
    )
    assert result.status == "error"
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["execution"]["cancelled"]
    assert not receipt["phases"]
    assert check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches
