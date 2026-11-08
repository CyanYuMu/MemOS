"""Agent-authored candidates stay confined during independent acceptance."""

import hashlib
import json
import sys

from functools import partial
from pathlib import Path

import pytest

from memos.skill_adapter.verification import execute_guarded
from memos.skill_evaluation.tasks import materialize_case
from memos.skill_evaluation.verifier import check_receipt, verify_candidate


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS isolation backend")


@pytest.mark.parametrize("case_id", ["source-dream-update", "source-scoped-update"])
def test_real_project_source_reference_runs_inside_guard(tmp_path, case_id):
    candidate = materialize_case(case_id, tmp_path / "candidate", "reference")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate(case_id, candidate, artifacts, executor=execute_guarded)
    assert result.status == "passed"
    assert check_receipt(result.receipt_path, candidate, artifacts).matches


def verify(tmp_path, addition="", *, timeout=10, secret_values=()):
    candidate = materialize_case("dev-strict-signature", tmp_path / "candidate", "reference")
    with (candidate / "caller.py").open("a") as stream:
        stream.write("\n" + addition + "\n")
    artifacts = tmp_path / "artifacts"
    result = verify_candidate(
        "dev-strict-signature",
        candidate,
        artifacts,
        timeout=timeout,
        executor=partial(execute_guarded, secret_values=secret_values),
    )
    return result, json.loads(result.receipt_path.read_text()), candidate, artifacts


def test_real_guarded_reference_and_profile_are_bound_to_receipt(tmp_path):
    result, receipt, candidate, artifacts = verify(tmp_path)
    assert result.status == "passed"
    guard = receipt["execution"]["isolation"]
    assert guard["backend"] == "macos-sandbox-exec"
    profile = artifacts / receipt["artifacts"]["isolation_profile"]["path"]
    assert guard["profile_sha256"] == hashlib.sha256(profile.read_bytes()).hexdigest()
    assert check_receipt(result.receipt_path, candidate, artifacts).matches
    profile.write_text(profile.read_text() + "\n; modified\n")
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


@pytest.mark.parametrize(
    "operation",
    [
        "import os; os.fork()",
        "import subprocess; subprocess.run(['/bin/true'], check=True)",
        "import socket; socket.create_connection(('127.0.0.1', 9), timeout=0.1)",
        "from pathlib import Path; Path(__file__).write_text('changed')",
    ],
)
def test_candidate_process_cannot_spawn_connect_or_rewrite_inputs(tmp_path, operation):
    # Catch only an actual OS permission denial, not a different test failure.
    code = (
        "try:\n    "
        + operation
        + "\nexcept PermissionError:\n    pass\nelse:\n    raise RuntimeError('escape allowed')"
    )
    result, receipt, candidate, artifacts = verify(tmp_path, code)
    assert result.status == "passed", receipt["reasons"]
    assert check_receipt(result.receipt_path, candidate, artifacts).matches


def test_candidate_cannot_read_known_repository_or_other_run_material(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    other = tmp_path / "another-run-secret.txt"
    other.write_text("only a collector-created sentinel")
    code = "from pathlib import Path\n"
    for target in (repository / "README.md", other):
        code += f"try:\n    Path({str(target)!r}).read_text()\nexcept PermissionError:\n    pass\nelse:\n    raise RuntimeError('hidden read allowed')\n"
    result, receipt, _, _ = verify(tmp_path, code)
    assert result.status == "passed", receipt["reasons"]


@pytest.mark.parametrize(
    "code, reason",
    [("import time; time.sleep(30)", "timeout"), ("print('x' * 400000)", "output_limit")],
)
def test_guarded_timeout_and_output_overflow_fail_closed(tmp_path, code, reason):
    result, receipt, candidate, artifacts = verify(tmp_path, code, timeout=0.2)
    assert result.status == "error"
    assert reason in receipt["reasons"]
    assert (artifacts / receipt["artifacts"]["stdout"]["path"]).stat().st_size <= 131072
    assert check_receipt(result.receipt_path, candidate, artifacts).matches


def test_resealed_profile_hash_mismatch_is_detected(tmp_path):
    result, receipt, candidate, artifacts = verify(tmp_path)
    receipt["execution"]["isolation"]["profile_sha256"] = "0" * 64
    result.receipt_path.write_text(json.dumps(receipt))
    result.receipt_path.with_suffix(".sha256").write_text(
        hashlib.sha256(result.receipt_path.read_bytes()).hexdigest()
    )
    checked = check_receipt(result.receipt_path, candidate, artifacts)
    assert not checked.matches
    assert "isolation_profile_mismatch" in checked.reasons


@pytest.mark.parametrize("value", [[], {"backend": "unconfined"}])
def test_invalid_isolation_metadata_cannot_be_resealed_as_valid(tmp_path, value):
    result, receipt, candidate, artifacts = verify(tmp_path)
    receipt["execution"]["isolation"] = value
    result.receipt_path.write_text(json.dumps(receipt))
    result.receipt_path.with_suffix(".sha256").write_text(
        hashlib.sha256(result.receipt_path.read_bytes()).hexdigest()
    )
    assert not check_receipt(result.receipt_path, candidate, artifacts).matches


def test_guarded_logs_are_sanitized_before_persistent_artifacts(tmp_path):
    secret = "synthetic-provider-secret-48"
    addition = (
        "import sys\n"
        "print('Authorization: Bearer synthetic-secret')\n"
        f"print('observed {secret}', file=sys.stderr)\n"
    )
    result, receipt, candidate, artifacts = verify(tmp_path, addition, secret_values=(secret,))
    assert result.status == "passed"
    for name in ("stdout", "stderr", "report"):
        text = (artifacts / receipt["artifacts"][name]["path"]).read_text()
        assert "synthetic-secret" not in text
        assert secret not in text
    assert "[REDACTED]" in (artifacts / receipt["artifacts"]["stdout"]["path"]).read_text()
    assert "[REDACTED]" in (artifacts / receipt["artifacts"]["stderr"]["path"]).read_text()
    assert check_receipt(result.receipt_path, candidate, artifacts).matches


def test_guarded_failure_details_are_sanitized_before_receipt(tmp_path):
    secret = "synthetic-provider-secret-48"
    result, receipt, candidate, artifacts = verify(
        tmp_path, f"raise RuntimeError('observed {secret}')", secret_values=(secret,)
    )
    assert result.status == "error"
    for name in ("stdout", "stderr", "report"):
        text = (artifacts / receipt["artifacts"][name]["path"]).read_text()
        assert secret not in text
    assert secret not in result.receipt_path.read_text()
    assert "[REDACTED]" in json.dumps(receipt["checks"])
    assert check_receipt(result.receipt_path, candidate, artifacts).matches


def test_invalid_oracle_report_does_not_persist_an_unstructured_secret(tmp_path):
    secret = "synthetic-provider-secret-48"
    addition = (
        "import os, sys\nfrom pathlib import Path\n"
        f"Path(sys.argv[-1]).write_text({secret!r})\n"
        "os._exit(0)\n"
    )
    result, receipt, candidate, artifacts = verify(tmp_path, addition, secret_values=(secret,))
    assert result.status == "error"
    text = (artifacts / receipt["artifacts"]["report"]["path"]).read_text()
    assert secret not in text
    assert json.loads(text) == {"invalid_report": True}
    assert check_receipt(result.receipt_path, candidate, artifacts).matches
