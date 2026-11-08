"""Retained branch artifacts must remain complete and obey the fixed contract."""

import hashlib
import json
import sys
import threading

import pytest

from memos.skill_branch_validation.tasks import materialize
from memos.skill_branch_validation.verifier import check_receipt, verify_task
from memos.skill_evaluation.tasks import materialize_case
from memos.skill_verification.verifier import verify_candidate


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")


def _sealed(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_task(
        "branch-scoped-table", candidate, artifacts, run_id="run", attempt_id="one"
    )
    assert result.status == "passed"
    assert check_receipt(result.receipt_path, candidate, artifacts).matches
    return candidate, artifacts, result


def _rewrite_receipt(path, receipt):
    path.write_text(json.dumps(receipt))
    path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")


def _rehash_phase_file(receipt, path):
    reference = receipt["phaseArtifacts"][path.name]
    reference["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    reference["size"] = path.stat().st_size


@pytest.mark.parametrize(
    "change", ["missing_report", "missing_check", "duplicate_check", "wrong_import", "check_error"]
)
def test_rehashed_incomplete_or_error_report_cannot_retain_pass(tmp_path, change):
    candidate, artifacts, result = _sealed(tmp_path)
    receipt = json.loads(result.receipt_path.read_text())
    path = result.receipt_path.parent / "phase/report.json"
    report = json.loads(path.read_text())
    if change == "missing_report":
        path.unlink()
        receipt["phaseArtifacts"].pop("report.json")
    else:
        if change == "missing_check":
            report["checks"].pop()
        elif change == "duplicate_check":
            report["checks"][1] = report["checks"][0]
        elif change == "wrong_import":
            report["imported"]["fake_graph"] = "/unrelated/fake_graph.py"
        else:
            report["checks"][0]["status"] = "error"
            report["checks"][0]["error"] = "RuntimeError"
        path.write_text(json.dumps(report))
        _rehash_phase_file(receipt, path)
        # Matching the copied checks is insufficient: the host recomputes completeness/status.
        receipt["checks"] = report["checks"]
    _rewrite_receipt(result.receipt_path, receipt)
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert checked.invalid and not checked.matches


@pytest.mark.parametrize("change", ["fork_limit", "timeout", "cancelled", "output_limited"])
def test_rehashed_execution_cannot_relax_limits_or_hide_execution_fault(tmp_path, change):
    candidate, artifacts, result = _sealed(tmp_path)
    receipt = json.loads(result.receipt_path.read_text())
    path = result.receipt_path.parent / "phase/execution.json"
    execution = json.loads(path.read_text())
    if change == "fork_limit":
        execution["limits"]["fork_allowed"] = True
    elif change == "timeout":
        execution["timed_out"] = True
    else:
        execution[change] = True
    path.write_text(json.dumps(execution))
    _rehash_phase_file(receipt, path)
    receipt["execution"] = execution
    _rewrite_receipt(result.receipt_path, receipt)
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


@pytest.mark.parametrize("change", ["extra_file", "extra_directory", "missing_file", "symlink"])
def test_complete_snapshot_does_not_authorize_changed_task_inventory(tmp_path, change):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    if change == "extra_file":
        (candidate / "extra.txt").write_text("unregistered")
    elif change == "extra_directory":
        (candidate / "empty").mkdir()
    elif change == "missing_file":
        (candidate / "TASK.md").unlink()
    else:
        (candidate / "TASK.md").unlink()
        (candidate / "TASK.md").symlink_to("visible_checks.py")
    artifacts = tmp_path / "artifacts"
    result = verify_task("branch-scoped-table", candidate, artifacts)
    assert result.status == "failed"
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["policyViolations"]
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert checked.matches and checked.verification_status == "failed"


@pytest.mark.parametrize("change", ["task", "protocol", "run", "attempt", "qualification"])
def test_rehashed_receipt_cannot_change_task_run_or_qualification(tmp_path, change):
    candidate, artifacts, result = _sealed(tmp_path)
    receipt = json.loads(result.receipt_path.read_text())
    if change == "task":
        receipt["task"]["revision"] = "invented-revision"
    elif change == "protocol":
        receipt["protocol"] = "p41.1"
    elif change == "run":
        receipt["runId"] = "other"
    elif change == "attempt":
        receipt["attemptId"] = "other"
    else:
        receipt["qualifiedAgentExperience"] = True
    _rewrite_receipt(result.receipt_path, receipt)
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


def test_actual_check_exception_is_error_not_a_business_failure(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    path = candidate / "fake_graph.py"
    path.write_text(
        path.read_text().replace("pair = self.table.get(memory_id)", "raise RuntimeError('broken')")
    )
    artifacts = tmp_path / "artifacts"
    result = verify_task("branch-scoped-table", candidate, artifacts)
    assert result.status == "error"
    receipt = json.loads(result.receipt_path.read_text())
    assert any(row["status"] == "error" for row in receipt["checks"])
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert checked.matches and checked.verification_status == "error"


def test_actual_cancelled_execution_is_retained_as_error(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    cancelled = threading.Event()
    cancelled.set()
    artifacts = tmp_path / "artifacts"
    result = verify_task("branch-scoped-table", candidate, artifacts, cancellation_event=cancelled)
    assert result.status == "error"
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert checked.matches and checked.verification_status == "error"


def test_new_checker_rejects_an_actual_p41_receipt(tmp_path):
    candidate = materialize_case("holdout-record", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    old = verify_candidate("holdout-record", candidate, artifacts)
    assert old.status == "passed"
    checked = check_receipt(old.receipt_path, candidate, artifacts)
    assert checked.invalid and not checked.matches


@pytest.mark.parametrize("nested", ["artifacts", "candidate"])
def test_verification_rejects_overlapping_workspace_and_artifacts(tmp_path, nested):
    if nested == "artifacts":
        candidate = materialize("branch-scoped-table", tmp_path / "candidate")
        artifacts = candidate / "artifacts"
    else:
        artifacts = tmp_path / "artifacts"
        candidate = materialize("branch-scoped-table", artifacts / "candidate")
    with pytest.raises(ValueError, match="overlap"):
        verify_task("branch-scoped-table", candidate, artifacts)


@pytest.mark.parametrize("alias", ["artifact_root", "snapshot_store", "candidate"])
def test_checker_rejects_symlink_aliases_before_resolving_them(tmp_path, alias):
    candidate, artifacts, result = _sealed(tmp_path)
    if alias == "artifact_root":
        link = tmp_path / "artifact-alias"
        link.symlink_to(artifacts, target_is_directory=True)
        artifacts = link
    elif alias == "candidate":
        link = tmp_path / "candidate-alias"
        link.symlink_to(candidate, target_is_directory=True)
        candidate = link
    else:
        original = artifacts / "snapshots"
        moved = tmp_path / "moved-snapshots"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert checked.invalid and not checked.matches


@pytest.mark.parametrize("value", [{"arbitrary": "data"}, ["passed"], "invented"])
def test_invalid_status_does_not_escape_as_a_public_result(tmp_path, value):
    candidate, artifacts, result = _sealed(tmp_path)
    receipt = json.loads(result.receipt_path.read_text())
    receipt["status"] = value
    _rewrite_receipt(result.receipt_path, receipt)
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert checked.invalid and checked.verification_status == "unknown"


def test_deeply_nested_json_returns_invalid_receipt_instead_of_raising(tmp_path):
    candidate, artifacts, result = _sealed(tmp_path)
    raw = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
    result.receipt_path.write_text(raw)
    result.receipt_path.with_suffix(".sha256").write_text(
        hashlib.sha256(raw.encode()).hexdigest() + "\n"
    )
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert checked.invalid and checked.verification_status == "unknown"
