"""Registry policy and manual outcomes must survive verification and receipt checks."""

import hashlib
import json

from dataclasses import replace

import pytest

from memos.skill_evaluation import verifier
from memos.skill_evaluation.cases import get_case, materialize_case


def _registered_task(monkeypatch, **changes):
    case = replace(
        get_case("dev-strict-signature"),
        split="source",
        editable_paths=("caller.py", "fake_graph.py"),
        **changes,
    )
    monkeypatch.setattr(verifier, "get_case", lambda _: case)
    return case


def _candidate(tmp_path):
    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    (candidate / "TASK.md").unlink()
    return candidate


def test_receipt_uses_registered_source_identity_and_scoring(tmp_path, monkeypatch):
    _registered_task(monkeypatch)
    candidate = _candidate(tmp_path)
    artifacts = tmp_path / "artifacts"
    result = verifier.verify_candidate("dev-strict-signature", candidate, artifacts)
    receipt = json.loads(result.receipt_path.read_text())
    assert result.status == "passed"
    assert receipt["initial_snapshot_origin"] == "registered_source_task"
    assert receipt["asset_digest"]
    assert receipt["scoring"]["repair_pass"] == 1
    assert receipt["scoring"]["boundary_pass"] is None
    assert verifier.check_receipt(result.receipt_path, candidate, artifacts).matches


@pytest.mark.parametrize("change", ["protected", "unknown", "deleted"])
def test_passing_behavior_cannot_override_invalid_workspace(tmp_path, monkeypatch, change):
    _registered_task(monkeypatch)
    candidate = _candidate(tmp_path)
    if change == "protected":
        (candidate / "visible_checks.py").write_text("pass\n")
    elif change == "unknown":
        (candidate / ".hidden").write_text("a secret input")
    else:
        (candidate / "visible_checks.py").unlink()
    artifacts = tmp_path / "artifacts"
    result = verifier.verify_candidate("dev-strict-signature", candidate, artifacts)
    assert result.status == "failed"
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["workspace_policy_violations"]
    assert verifier.check_receipt(result.receipt_path, candidate, artifacts).matches


def test_manual_gate_never_becomes_automatic_repair_or_boundary_success(tmp_path, monkeypatch):
    _registered_task(
        monkeypatch, assessment_kind="manual_boundary", manual_rubric=("requires evidence",)
    )
    candidate = _candidate(tmp_path)
    artifacts = tmp_path / "artifacts"
    result = verifier.verify_candidate("dev-strict-signature", candidate, artifacts)
    assert result.status == "needs_review"
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["scoring"]["repair_pass"] is None
    assert receipt["scoring"]["boundary_pass"] is None
    check = verifier.check_receipt(result.receipt_path, candidate, artifacts)
    assert check.matches and check.verification_status == "needs_review"


def test_changed_rubric_with_same_oracle_invalidates_old_receipt(tmp_path, monkeypatch):
    case = _registered_task(monkeypatch)
    candidate = _candidate(tmp_path)
    artifacts = tmp_path / "artifacts"
    result = verifier.verify_candidate("dev-strict-signature", candidate, artifacts)
    monkeypatch.setattr(
        verifier, "get_case", lambda _: replace(case, manual_rubric=("new evidence required",))
    )
    check = verifier.check_receipt(result.receipt_path, candidate, artifacts)
    assert not check.matches and "asset_digest_mismatch" in check.reasons


def test_rehashed_receipt_cannot_upgrade_pending_manual_scores(tmp_path, monkeypatch):
    _registered_task(
        monkeypatch, assessment_kind="manual_boundary", manual_rubric=("requires evidence",)
    )
    candidate = _candidate(tmp_path)
    artifacts = tmp_path / "artifacts"
    result = verifier.verify_candidate("dev-strict-signature", candidate, artifacts)
    receipt = json.loads(result.receipt_path.read_text())
    receipt["scoring"]["boundary_pass"] = 1
    receipt["scoring"]["repair_pass"] = 1
    result.receipt_path.write_text(json.dumps(receipt))
    result.receipt_path.with_suffix(".sha256").write_text(
        hashlib.sha256(result.receipt_path.read_bytes()).hexdigest()
    )
    checked = verifier.check_receipt(result.receipt_path, candidate, artifacts)
    assert not checked.matches and checked.invalid
    assert "scoring_mismatch" in checked.reasons
