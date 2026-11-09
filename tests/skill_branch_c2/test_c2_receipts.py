"""Only this selected suite's retained execution can validate its receipt."""

import hashlib
import json
import sys

import pytest

from memos.skill_branch_c2.tasks import get_suite, materialize
from memos.skill_branch_c2.verifier import check_receipt, verify_suite
from memos.skill_branch_validation.verifier import check_receipt as core_check


SUITE = "scoped-healthy-dream-c2-v1"
MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS fixed no-fork executor")


def workspace(root, variant="reference"):
    candidate = materialize(SUITE, root / "candidate")
    for name, value in get_suite(SUITE).variants[variant].items():
        (candidate / name).write_text(value)
    return candidate


@MACOS
@pytest.mark.parametrize(
    "variant",
    [
        "initial",
        "reference",
        "unsafe_empty_read",
        "unsafe_update_arguments",
    ],
)
def test_receipt_preserves_the_exact_grouped_outcomes(tmp_path, variant):
    candidate = workspace(tmp_path, variant)
    result = verify_suite(
        SUITE, candidate, tmp_path / "artifacts", run_id="run", attempt_id="attempt"
    )
    receipt = json.loads(result.receipt_path.read_text())
    expected = get_suite(SUITE).expected_failures[variant]["hidden"]
    assert result.status == ("failed" if expected else "passed")
    assert [c["name"] for c in receipt["checks"] if c["status"] == "failed"] == list(expected)
    assert receipt["checkMapping"] == list(get_suite(SUITE).checks)
    assert check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches
    assert not core_check(result.receipt_path, candidate, tmp_path / "artifacts").matches
    assert receipt["branchCoverage"] == []


@MACOS
def test_receipt_rejects_a_changed_final_patch(tmp_path):
    candidate = workspace(tmp_path)
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts")
    (candidate / "caller.py").write_text("raise RuntimeError('different patch')\n")
    assert not check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


@MACOS
@pytest.mark.parametrize("field", ["groupStatus", "suite", "checkMapping", "status"])
def test_rehashing_the_outer_receipt_cannot_change_semantics(tmp_path, field):
    candidate = workspace(tmp_path)
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts")
    receipt = json.loads(result.receipt_path.read_text())
    receipt[field] = {} if field != "status" else "failed"
    result.receipt_path.write_text(json.dumps(receipt))
    result.receipt_path.with_suffix(".sha256").write_text(
        hashlib.sha256(result.receipt_path.read_bytes()).hexdigest() + "\n"
    )
    assert not check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), None])
def test_invalid_timeout_fails_before_execution(tmp_path, timeout):
    with pytest.raises(ValueError):
        verify_suite(SUITE, tmp_path / "missing", tmp_path / "artifacts", timeout=timeout)
    assert not (tmp_path / "artifacts").exists()


@MACOS
@pytest.mark.parametrize(
    ("source", "timeout", "flag"),
    [
        ("raise SystemExit(0)\n", 5, None),
        ("raise RuntimeError('broken import')\n", 5, None),
        ("while True: pass\n", 0.25, "timed_out"),
        ("while True: print('x' * 8192, flush=True)\n", 5, "output_limited"),
    ],
)
def test_actual_resource_and_report_faults_are_not_business_failures(
    tmp_path, source, timeout, flag
):
    candidate = workspace(tmp_path)
    (candidate / "caller.py").write_text(source)
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts", timeout=timeout)
    receipt = json.loads(result.receipt_path.read_text())
    assert result.status == "error"
    assert receipt["groupStatus"] == {"scoped": "error", "dream": "error"}
    if flag:
        assert receipt["execution"][flag]
    assert check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


@MACOS
def test_cancelled_execution_is_retained_as_error(tmp_path):
    import threading

    candidate = workspace(tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts", cancellation_event=cancelled)
    receipt = json.loads(result.receipt_path.read_text())
    assert result.status == "error"
    assert receipt["execution"]["cancelled"]
    assert check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


@MACOS
@pytest.mark.parametrize("artifact", ["program.py", "isolation.sb", "report.json"])
def test_rehashing_artifacts_does_not_replace_trusted_program_or_checks(tmp_path, artifact):
    from memos.skill_verification.verifier import _phase_artifacts

    candidate = workspace(tmp_path)
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts")
    receipt = json.loads(result.receipt_path.read_text())
    target = result.receipt_path.parent / "phase" / artifact
    if artifact == "report.json":
        data = json.loads(target.read_text())
        data["checks"].pop()
        target.write_text(json.dumps(data))
    else:
        target.write_text(target.read_text() + "\n# modified\n")
    receipt["phaseArtifacts"] = _phase_artifacts(target.parent)
    result.receipt_path.write_text(json.dumps(receipt))
    result.receipt_path.with_suffix(".sha256").write_text(
        hashlib.sha256(result.receipt_path.read_bytes()).hexdigest() + "\n"
    )
    assert not check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


@MACOS
def test_executable_or_protected_task_changes_never_yield_pass(tmp_path):
    candidate = workspace(tmp_path)
    (candidate / "caller.py").chmod(0o700)
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts")
    receipt = json.loads(result.receipt_path.read_text())
    assert result.status == "failed"
    assert receipt["groupStatus"] == {"scoped": "error", "dream": "error"}
    assert "task_executable_changed:caller.py" in receipt["policyViolations"]
    assert check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


@MACOS
@pytest.mark.parametrize("field,value", [("schemaVersion", True), ("qualifiedAgentExperience", 0)])
def test_receipt_schema_rejects_json_boolean_numeric_aliases(tmp_path, field, value):
    candidate = workspace(tmp_path)
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts")
    receipt = json.loads(result.receipt_path.read_text())
    receipt[field] = value
    result.receipt_path.write_text(json.dumps(receipt))
    result.receipt_path.with_suffix(".sha256").write_text(
        hashlib.sha256(result.receipt_path.read_bytes()).hexdigest() + "\n"
    )
    assert not check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches


@pytest.mark.parametrize("value", ["", True, [], 1])
def test_invalid_run_identity_is_not_replaced_with_random_identity(tmp_path, value):
    candidate = workspace(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        verify_suite(SUITE, candidate, tmp_path / "artifacts", run_id=value)
    assert not (tmp_path / "artifacts").exists()


@MACOS
def test_rebinding_verifier_during_execution_invalidates_receipt(tmp_path, monkeypatch):
    from memos.skill_branch_c2 import verifier as module

    candidate = workspace(tmp_path)
    actual = module.execute_program

    def execute_and_change(*args, **kwargs):
        execution = actual(*args, **kwargs)
        monkeypatch.setattr(module, "implementation_digest", lambda: "new-implementation")
        return execution

    monkeypatch.setattr(module, "execute_program", execute_and_change)
    result = verify_suite(SUITE, candidate, tmp_path / "artifacts")
    assert result.status == "error"
    assert not check_receipt(result.receipt_path, candidate, tmp_path / "artifacts").matches
