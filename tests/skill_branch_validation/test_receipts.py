"""New branch receipts bind the same patch and cannot borrow legacy acceptance."""

import hashlib
import json
import sys

import pytest

from memos.skill_branch_validation.tasks import materialize
from memos.skill_branch_validation.verifier import check_receipt, verify_task
from memos.skill_evaluation.verifier import check_receipt as legacy_check
from memos.skill_verification.verifier import check_receipt as p41_check


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")


def sealed(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_task(
        "branch-scoped-table", candidate, artifacts, run_id="run", attempt_id="one"
    )
    return candidate, artifacts, result


def rewrite(path, value):
    path.write_text(json.dumps(value))
    path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")


def test_reference_receipt_rechecks_and_legacy_protocols_reject_it(tmp_path):
    candidate, artifacts, result = sealed(tmp_path)
    assert result.status == "passed"
    assert check_receipt(result.receipt_path, candidate, artifacts).matches
    assert not legacy_check(result.receipt_path, candidate, artifacts).matches
    assert not p41_check(result.receipt_path, candidate, artifacts).matches
    assert not check_receipt(result.receipt_path, candidate, tmp_path / "other").matches


def test_initial_failure_is_a_valid_failed_receipt(tmp_path):
    candidate = materialize("branch-dream-journal", tmp_path / "candidate")
    result = verify_task("branch-dream-journal", candidate, tmp_path / "artifacts")
    assert result.status == "failed"
    checked = check_receipt(result.receipt_path, candidate, tmp_path / "artifacts")
    assert checked.matches and checked.verification_status == "failed"


@pytest.mark.parametrize("change", ["patch", "status", "checks", "execution", "profile", "program"])
def test_changed_patch_or_rehashed_forgery_cannot_pass(tmp_path, change):
    candidate, artifacts, result = sealed(tmp_path)
    receipt = json.loads(result.receipt_path.read_text())
    if change == "patch":
        (candidate / "fake_graph.py").write_text("# changed final patch\n")
    elif change == "status":
        receipt["status"] = "failed"
    elif change == "checks":
        receipt["checks"] = []
    elif change == "execution":
        receipt["execution"]["limits"]["fork_allowed"] = True
    else:
        path = (
            result.receipt_path.parent
            / "phase"
            / ("isolation.sb" if change == "profile" else "program.py")
        )
        path.write_text(
            "(version 1)\n(allow default)\n" if change == "profile" else "print('forged')\n"
        )
        # Rehashing the artifact manifest is not permission to change the fixed program/profile.
        ref = receipt["phaseArtifacts"][path.name]
        ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        ref["size"] = path.stat().st_size
    rewrite(result.receipt_path, receipt)
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


def test_protected_public_checks_remain_rejected_even_with_correct_repair(tmp_path):
    candidate = materialize("branch-scoped-table", tmp_path / "candidate", "reference")
    (candidate / "visible_checks.py").write_text("raise SystemExit(0)\n")
    result = verify_task("branch-scoped-table", candidate, tmp_path / "artifacts")
    assert result.status == "failed"
    assert check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches
