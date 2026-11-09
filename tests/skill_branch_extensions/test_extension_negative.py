"""Negative asset checks must not masquerade as expected business failures."""

import json
import sys

from dataclasses import replace
from pathlib import Path

import pytest

from memos.skill_branch_extensions import selftest
from memos.skill_branch_extensions.tasks import MODES, VARIANTS, get_suite, materialize
from memos.skill_branch_validation.qualification import require_qualification as core_qualification
from memos.skill_verification.selftest import require_qualification as p41_qualification


SUITE_ID = "combined-c1-v1"
MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS fixed no-fork executor")


def variant_workspace(tmp_path, variant="reference"):
    suite = get_suite(SUITE_ID)
    candidate = materialize(SUITE_ID, tmp_path / "candidate")
    for name, source in suite.variants[variant].items():
        (candidate / name).write_text(source)
    return candidate


def test_unsafe_mutations_start_from_complete_reference_and_keep_the_correct_caller():
    suite = get_suite(SUITE_ID)
    reference = suite.variants["reference"]
    for name in ("unsafe_empty_read", "unsafe_update_arguments"):
        unsafe = suite.variants[name]
        assert set(unsafe) == set(reference)
        assert {key for key in reference if unsafe[key] != reference[key]} == {"fake_graph.py"}
        assert unsafe["caller.py"] == reference["caller.py"]
    assert reference["caller.py"] != suite.variants["initial"]["caller.py"]


@MACOS
@pytest.mark.parametrize("mode", ["public", "hidden"])
@pytest.mark.parametrize(
    ("fault", "source", "timeout"),
    [
        ("early_success", "raise SystemExit(0)\n", 15),
        ("early_failure", "raise SystemExit(1)\n", 15),
        ("import_error", "raise RuntimeError('synthetic import failure')\n", 15),
        ("timeout", "while True: pass\n", 0.2),
        ("output_limit", "print('X' * 300000)\n", 15),
    ],
)
def test_runtime_faults_do_not_count_as_expected_initial_failures(
    tmp_path, fault, source, timeout, mode
):
    candidate = variant_workspace(tmp_path, "initial")
    path = candidate / "fake_graph.py"
    path.write_text(source + path.read_text())
    result = selftest.evaluate_candidate(
        SUITE_ID, candidate, tmp_path / "audit", mode=mode, timeout=timeout
    )
    assert result["status"] == "error"
    assert set(result["groupStatus"].values()) == {"error"}
    execution_paths = list((tmp_path / "audit").rglob("execution.json"))
    assert len(execution_paths) == 1
    execution = json.loads(execution_paths[0].read_text())
    if fault == "timeout":
        assert execution["timed_out"] is True
    elif fault == "output_limit":
        assert execution["output_limited"] is True


@MACOS
def test_reported_module_from_another_path_is_an_error(tmp_path):
    candidate = variant_workspace(tmp_path)
    path = candidate / "fake_graph.py"
    path.write_text(path.read_text() + '\n__file__ = "/unrelated/fake_graph.py"\n')
    result = selftest.evaluate_candidate(SUITE_ID, candidate, tmp_path / "audit", mode="hidden")
    assert result["status"] == "error"


@MACOS
@pytest.mark.parametrize(
    "change", ["missing_check", "duplicate_check", "missing_report", "checks_none", "status_list"]
)
def test_incomplete_retained_report_cannot_match_the_fixed_failure_matrix(
    tmp_path, monkeypatch, change
):
    execute = selftest.execute_program

    def corrupted_report(*args, **kwargs):
        result = execute(*args, **kwargs)
        path = Path(args[2]) / "report.json"
        if change == "missing_report":
            path.unlink()
        else:
            report = json.loads(path.read_text())
            if change == "missing_check":
                report["checks"].pop()
            elif change == "duplicate_check":
                report["checks"][1] = report["checks"][0]
            elif change == "checks_none":
                report["checks"] = None
            else:
                report["checks"][0]["status"] = []
            path.write_text(json.dumps(report))
        return result

    monkeypatch.setattr(selftest, "execute_program", corrupted_report)
    result = selftest.check_variant(SUITE_ID, "initial", "hidden", tmp_path / "audit")
    assert result["status"] == "error"
    assert result["matchesExpectation"] is False
    retained = json.loads((tmp_path / "audit/selftest.json").read_text())
    assert retained["status"] == "error" and retained["matchesExpectation"] is False


@MACOS
def test_candidate_changed_after_execution_is_never_a_successful_asset_check(tmp_path, monkeypatch):
    execute = selftest.execute_program

    def change_after_run(*args, **kwargs):
        result = execute(*args, **kwargs)
        path = Path(args[1]) / "fake_graph.py"
        old_mode = path.stat().st_mode
        path.chmod(old_mode | 0o200)
        path.write_text("# Changed after the actual checks finished\n")
        path.chmod(old_mode)
        return result

    monkeypatch.setattr(selftest, "execute_program", change_after_run)
    candidate = variant_workspace(tmp_path)
    result = selftest.evaluate_candidate(SUITE_ID, candidate, tmp_path / "audit", mode="hidden")
    assert result["status"] == "error"
    assert result["candidateUnchanged"] is False
    assert set(result["groupStatus"].values()) == {"error"}


@pytest.mark.parametrize(
    "change", ["task", "public_checks", "extra_file", "extra_directory", "symlink"]
)
def test_protected_inputs_cannot_be_changed_to_manufacture_an_expected_failure(tmp_path, change):
    candidate = variant_workspace(tmp_path)
    if change == "task":
        (candidate / "TASK.md").write_text("A replacement contract")
    elif change == "public_checks":
        (candidate / "visible_checks.py").write_text("raise SystemExit(0)\n")
    elif change == "extra_file":
        (candidate / "unexpected.txt").write_text("Unregistered input")
    elif change == "extra_directory":
        (candidate / "extra").mkdir()
    else:
        (candidate / "TASK.md").unlink()
        (candidate / "TASK.md").symlink_to("visible_checks.py")
    with pytest.raises(ValueError, match="extension_candidate_policy_changed"):
        selftest.evaluate_candidate(SUITE_ID, candidate, tmp_path / "audit", mode="hidden")


@MACOS
def test_successful_extension_selftest_is_not_legacy_agent_admission(tmp_path):
    output = tmp_path / "audit"
    summary = selftest.audit_suite(SUITE_ID, output)
    assert summary["passed"] is True
    assert summary["purpose"] == "branch_extension_asset_selftest"
    assert summary["qualifiedAgentExperience"] is False
    assert summary["branchCoverage"] == []
    assert summary["modelCalls"] == 0
    for checker in (core_qualification, p41_qualification):
        with pytest.raises(ValueError):
            checker(output)


@MACOS
def test_solution_special_cased_for_public_identifiers_is_rejected_by_hidden_cases(tmp_path):
    candidate = variant_workspace(tmp_path)
    path = candidate / "fake_graph.py"
    path.write_text(
        path.read_text()
        + "\n_original_read = FakeGraphDB.get_node\n"
        + "def _public_only_read(self, memory_id, user_name=None):\n"
        + "    if memory_id not in {'p1', 'p2', 'd1', 'dream-cr'}:\n"
        + "        return None\n"
        + "    return _original_read(self, memory_id, user_name=user_name)\n"
        + "FakeGraphDB.get_node = _public_only_read\n"
    )
    public = selftest.evaluate_candidate(SUITE_ID, candidate, tmp_path / "public", mode="public")
    hidden = selftest.evaluate_candidate(SUITE_ID, candidate, tmp_path / "hidden", mode="hidden")
    assert public["status"] == "passed"
    assert hidden["status"] == "failed"
    assert "scoped.scope_generalization" in hidden["failedChecks"]


@MACOS
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("background_processes", True),
        ("limit_reasons", ["synthetic_resource_limit"]),
        ("start_error", "SyntheticFailure"),
    ],
)
def test_execution_fault_metadata_cannot_be_treated_as_expected_business_failure(
    tmp_path, monkeypatch, field, value
):
    execute = selftest.execute_program

    def resource_failure(*args, **kwargs):
        result = execute(*args, **kwargs)
        result[field] = value
        (Path(args[2]) / "execution.json").write_text(json.dumps(result))
        return result

    monkeypatch.setattr(selftest, "execute_program", resource_failure)
    result = selftest.check_variant(SUITE_ID, "initial", "hidden", tmp_path / "audit")
    assert result["status"] == "error"
    assert result["matchesExpectation"] is False


@MACOS
@pytest.mark.parametrize("mode", ["public", "hidden"])
@pytest.mark.parametrize(
    ("mutation", "failed_check"),
    [
        ("log_without_persisting", "scoped.caller_with_fake"),
        ("stored_alias", "dream.fake_contract"),
        ("merge_log_without_persisting", "dream.other_actions"),
    ],
)
def test_write_logs_cannot_hide_failed_persistence_or_mutable_storage_alias(
    tmp_path, mode, mutation, failed_check
):
    candidate = variant_workspace(tmp_path)
    path = candidate / "fake_graph.py"
    original = path.read_text()
    before = '        item["fields"].update(deepcopy(fields))'
    assert original.count(before) == 1
    if mutation == "log_without_persisting":
        after = '        if "embedding" not in fields:\n            item["fields"].update(deepcopy(fields))'
    elif mutation == "merge_log_without_persisting":
        after = '        if "merged_into" not in fields:\n            item["fields"].update(deepcopy(fields))'
    else:
        after = '        item["fields"].update(fields)'
    path.write_text(original.replace(before, after))
    result = selftest.evaluate_candidate(SUITE_ID, candidate, tmp_path / "audit", mode=mode)
    assert result["status"] == "failed"
    assert failed_check in result["failedChecks"]


@MACOS
def test_variant_cannot_borrow_expected_failures_from_a_different_suite_version(
    tmp_path, monkeypatch
):
    original = get_suite(SUITE_ID)
    changed = replace(original, oracle=original.oracle + "\n# different fixed suite identity\n")
    reads = []

    def changed_after_materialization(_):
        reads.append(1)
        return original if len(reads) == 1 else changed

    monkeypatch.setattr(selftest, "get_suite", changed_after_materialization)
    try:
        result = selftest.check_variant(SUITE_ID, "reference", "hidden", tmp_path / "audit")
    except ValueError:
        return  # Rejecting the changed suite before execution is also fail-closed.
    assert result["matchesExpectation"] is False


@MACOS
@pytest.mark.parametrize("change", ["mixed_rows_same_end", "changed_only_at_end"])
def test_audit_rejects_mixed_results_or_a_suite_changed_after_the_last_execution(
    tmp_path, monkeypatch, change
):
    original = get_suite(SUITE_ID)
    changed = replace(original, oracle=original.oracle + "\n# different fixed suite identity\n")
    selected = [original]
    calls = []
    check_variant = selftest.check_variant
    monkeypatch.setattr(selftest, "get_suite", lambda _: selected[0])

    def check_with_suite_race(*args, **kwargs):
        calls.append(1)
        # Each individual execution uses one internally consistent version.
        # The aggregate must still reject mixing identities across the matrix.
        if change == "mixed_rows_same_end":
            selected[0] = changed if len(calls) == 1 else original
        result = check_variant(*args, **kwargs)
        if change == "changed_only_at_end" and len(calls) == len(VARIANTS) * len(MODES):
            selected[0] = changed
        return result

    monkeypatch.setattr(selftest, "check_variant", check_with_suite_race)
    try:
        summary = selftest.audit_suite(SUITE_ID, tmp_path / "audit")
    except ValueError:
        return
    assert summary["passed"] is False
