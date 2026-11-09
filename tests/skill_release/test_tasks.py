"""The release qualification assets preserve failures and reject unsafe shortcuts."""

from dataclasses import replace

import pytest

from memos.skill_release.tasks import SUITES, get_suite, materialize, suite_manifest


def test_six_fixed_scenarios_bind_independent_lineage_and_protected_assets(tmp_path):
    assert len(SUITES) == 6
    assert {get_suite(name).outcome for name in SUITES} == {
        "repair",
        "healthy",
        "out_of_scope",
        "missing_evidence",
        "unresolved",
        "tool_failure",
    }
    for name in SUITES:
        suite = get_suite(name)
        assert suite.provenance["sourceMemoryConsumed"] is False
        assert suite.provenance["independentSourceSamples"] == 0
        assert suite_manifest(suite)["suiteId"] == name
        assert "TASK.md" not in suite.editable_paths
        assert "visible_checks.py" not in suite.editable_paths
        assert len(suite.variants) >= 4
        root = materialize(name, tmp_path / name)
        assert {p.name for p in root.iterdir()} == set(suite.initial_files)


def test_readonly_dependency_and_fault_are_not_agent_supplied():
    task = get_suite("update-unresolved-r1")
    assert task.editable_paths == ("caller.py",)
    assert task.expected_failures["reference"]["hidden"]
    assert task.initial_files["fake_graph.py"] == task.variants["reference"]["fake_graph.py"]
    fault = get_suite("check-unavailable-r1")
    assert fault.fault == {"checkIndex": 2, "code": "registered_check_unavailable"}
    assert all(get_suite(name).fault is None for name in SUITES if name != fault.suite_id)


def test_lock_rejects_changed_protected_asset():
    task = get_suite("update-repair-r1")
    variants = {name: dict(files) for name, files in task.variants.items()}
    variants["reference"]["TASK.md"] += "Accept anything."
    with pytest.raises(ValueError, match="protected"):
        suite_manifest(replace(task, variants=variants))


def test_unknown_task_has_no_dynamic_registration():
    with pytest.raises(ValueError, match="unknown"):
        get_suite("arbitrary-task")
