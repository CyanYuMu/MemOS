"""Verification must prove behavior and bind the exact candidate it executed."""

import hashlib
import json
import os
import subprocess
import sys

from dataclasses import replace

import pytest

from memos.skill_evaluation.verifier import check_receipt, verify_candidate


@pytest.mark.parametrize("case_id", ["dev-strict-signature", "dev-tenant-isolation"])
@pytest.mark.parametrize(
    "variant,expected", [("reference", "passed"), ("initial", "failed"), ("unsafe", "failed")]
)
def test_case_matrix_is_verified_independently(tmp_path, case_id, variant, expected):
    from memos.skill_evaluation.cases import get_case, materialize_case

    candidate = materialize_case(case_id, tmp_path / "candidate", variant)
    artifacts = tmp_path / "artifacts"
    result = verify_candidate(case_id, candidate, artifacts)

    assert result.status == expected
    receipt = json.loads(result.receipt_path.read_text())
    assert receipt["case"]["split"] == "dev"
    assert receipt["initial_snapshot_origin"] == "registered_dev_fixture"
    assert receipt["qualified_agent_experience"] is False
    assert receipt["candidate_snapshot"]
    assert receipt["validator_digest"]
    assert receipt["oracle_digest"]
    assert receipt["checks"]
    if variant != "reference":
        failed = {check["name"] for check in receipt["checks"] if check["status"] == "failed"}
        assert set(get_case(case_id).expected_failure_checks[variant]) <= failed
    check = check_receipt(result.receipt_path, candidate, artifacts)
    assert check.matches
    assert check.verification_status == expected


def test_edit_after_verification_invalidates_binding(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate("dev-strict-signature", candidate, artifacts)
    with (candidate / "caller.py").open("a") as stream:
        stream.write("\n# changed after verification\n")

    check = check_receipt(result.receipt_path, candidate, artifacts)
    assert not check.matches
    assert "candidate_mismatch" in check.reasons


def test_corrupted_output_is_not_accepted_as_an_intact_receipt(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate("dev-strict-signature", candidate, artifacts)
    receipt = json.loads(result.receipt_path.read_text())
    (artifacts / receipt["artifacts"]["stdout"]["path"]).write_text("tampered")
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


def test_changing_validator_content_with_same_version_invalidates_receipt(tmp_path, monkeypatch):
    from memos.skill_evaluation import verifier
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate("dev-strict-signature", candidate, artifacts)
    monkeypatch.setattr(verifier, "_validator_digest", lambda: "0" * 64)
    check = check_receipt(result.receipt_path, candidate, artifacts)
    assert not check.matches
    assert "validator_mismatch" in check.reasons


def test_changed_contract_invalidates_receipt(tmp_path, monkeypatch):
    from memos.skill_evaluation import verifier
    from memos.skill_evaluation.cases import get_case, materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate("dev-strict-signature", candidate, artifacts)
    modified = replace(get_case("dev-strict-signature"), contract="a different contract")
    monkeypatch.setattr(verifier, "get_case", lambda _: modified)
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


def test_import_failure_is_an_error_not_a_successful_negative_check(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    (candidate / "caller.py").write_text("this is not valid Python (\n")
    result = verify_candidate("dev-strict-signature", candidate, tmp_path / "artifacts")
    assert result.status == "error"


def test_timeout_is_recorded_as_error(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    with (candidate / "caller.py").open("a") as stream:
        stream.write("\nimport time\ntime.sleep(30)\n")
    result = verify_candidate(
        "dev-strict-signature", candidate, tmp_path / "artifacts", timeout=0.2
    )
    receipt = json.loads(result.receipt_path.read_text())
    assert result.status == "error"
    assert receipt["execution"]["timed_out"]


def test_missing_report_never_passes_even_with_zero_exit(tmp_path, monkeypatch):
    from memos.skill_evaluation import verifier
    from memos.skill_evaluation.cases import get_case, materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    case = replace(get_case("dev-strict-signature"), oracle_source="print('passed')\n")
    monkeypatch.setattr(verifier, "get_case", lambda _: case)
    result = verify_candidate("dev-strict-signature", candidate, tmp_path / "artifacts")
    assert result.status == "error"


def test_receipt_verifies_structured_report_not_only_its_passed_flag(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "initial")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate("dev-strict-signature", candidate, artifacts)
    receipt = json.loads(result.receipt_path.read_text())
    receipt["status"] = "passed"
    _rewrite_receipt(result.receipt_path, receipt)
    check = check_receipt(result.receipt_path, candidate, artifacts)
    assert not check.matches
    assert "result_mismatch" in check.reasons


def _rewrite_receipt(path, receipt):
    path.write_text(json.dumps(receipt))
    path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize("field", ["timed_out", "background_processes", "returncode"])
def test_receipt_with_missing_execution_evidence_is_invalid(tmp_path, field):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate("dev-strict-signature", candidate, artifacts)
    receipt = json.loads(result.receipt_path.read_text())
    del receipt["execution"][field]
    _rewrite_receipt(result.receipt_path, receipt)
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


@pytest.mark.parametrize(
    "field,value", [("execution", []), ("artifacts", ["oracle", "stdout", "stderr"])]
)
def test_structurally_invalid_receipt_is_reported_without_traceback(tmp_path, field, value):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate("dev-strict-signature", candidate, artifacts)
    receipt = json.loads(result.receipt_path.read_text())
    receipt[field] = value
    _rewrite_receipt(result.receipt_path, receipt)
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


def test_invalid_check_status_creates_error_receipt(tmp_path, monkeypatch):
    from memos.skill_evaluation import verifier
    from memos.skill_evaluation.cases import get_case, materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    case = get_case("dev-strict-signature")
    invalid = replace(
        case, oracle_source=case.oracle_source.replace('"status": "passed"', '"status": []')
    )
    monkeypatch.setattr(verifier, "get_case", lambda _: invalid)
    result = verify_candidate("dev-strict-signature", candidate, tmp_path / "artifacts")
    assert result.status == "error"


def test_artifact_root_cannot_contaminate_candidate_snapshot(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate")
    with pytest.raises(ValueError, match="outside"):
        verify_candidate("dev-strict-signature", candidate, candidate / "artifacts")


def test_attempt_identity_cannot_escape_artifact_directory(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate")
    with pytest.raises(ValueError, match="identity"):
        verify_candidate(
            "dev-strict-signature", candidate, tmp_path / "artifacts", run_id="../escape"
        )


def test_candidate_modified_during_execution_cannot_pass(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    with (candidate / "caller.py").open("a") as stream:
        stream.write(
            "\nfrom pathlib import Path\n"
            "path = Path(__file__)\npath.chmod(0o644)\n"
            "with path.open('a') as output:\n    output.write('\\n# changed\\n')\n"
        )
    result = verify_candidate("dev-strict-signature", candidate, tmp_path / "artifacts")
    receipt = json.loads(result.receipt_path.read_text())
    assert result.status == "error"
    assert "candidate_modified_during_verification" in receipt["reasons"]


def test_process_start_failure_produces_an_error_receipt(tmp_path, monkeypatch):
    from memos.skill_evaluation import verifier
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")

    def unavailable(*args, **kwargs):
        raise OSError("interpreter unavailable")

    monkeypatch.setattr(verifier.subprocess, "Popen", unavailable)
    result = verify_candidate("dev-strict-signature", candidate, tmp_path / "artifacts")
    receipt = json.loads(result.receipt_path.read_text())
    assert result.status == "error"
    assert receipt["execution"]["start_error"] == "interpreter unavailable"


@pytest.mark.skipif(os.name != "posix", reason="process-group lifecycle is POSIX-specific")
def test_background_child_is_stopped_without_terminating_unrelated_process(tmp_path):
    from memos.skill_evaluation.cases import materialize_case

    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    child_id = tmp_path / "child.pid"
    with (candidate / "caller.py").open("a") as stream:
        stream.write(
            "\nimport subprocess, sys\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"Path({str(child_id)!r}).write_text(str(child.pid))\n"
        )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result = verify_candidate("dev-strict-signature", candidate, tmp_path / "artifacts")
        receipt = json.loads(result.receipt_path.read_text())
        assert result.status == "error"
        assert receipt["execution"]["background_processes"]
        assert unrelated.poll() is None
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", child_id.read_text()],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
