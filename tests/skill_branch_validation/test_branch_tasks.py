"""Independent assets must distinguish useful repairs from weakened detection."""

import json
import sys

import pytest

from memos.skill_branch_validation.selftest import check_variant
from memos.skill_branch_validation.tasks import get_task, list_tasks, materialize, task_digest


def test_separate_frozen_registry_and_initial_export(tmp_path):
    tasks = list_tasks()
    assert len(tasks) == 2
    for task in tasks:
        assert task.purpose == "branch_validation"
        assert task.provenance["kind"] == "independently_authored_contract_probe"
        assert len(task_digest(task)) == 64
        workspace = materialize(task.task_id, tmp_path / task.task_id)
        assert {p.name for p in workspace.iterdir()} == set(task.initial_files)
        assert "TASK.md" in task.initial_files and "visible_checks.py" in task.initial_files
        assert not any("oracle" in path or "reference" in path for path in task.initial_files)
        with pytest.raises(FileExistsError):
            materialize(task.task_id, workspace)
    with pytest.raises(ValueError, match="unknown_branch_task"):
        get_task("source-scoped-update")


@pytest.mark.parametrize("task_id", ["branch-scoped-table", "branch-dream-journal"])
@pytest.mark.parametrize("variant", ["initial", "reference", "unsafe"])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")
def test_real_isolated_asset_selfcheck(task_id, variant, tmp_path):
    result = check_variant(task_id, variant, tmp_path / "output")
    assert result["matchesExpectation"]
    assert result["status"] == ("passed" if variant == "reference" else "failed")
    assert result["qualifiedAgentExperience"] is False
    assert result["branchCoverage"] == []
    assert result["candidateUnchanged"]
    execution = json.loads((tmp_path / "output/execution/execution.json").read_text())
    assert execution["limits"]["fork_allowed"] is False
    assert execution["limits"]["network_allowed"] is False
    assert execution["limits"]["max_candidate_processes"] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")
def test_scope_always_empty_and_dream_permissive_are_not_correct_repairs(tmp_path):
    scoped = check_variant("branch-scoped-table", "unsafe", tmp_path / "scope")
    dream = check_variant("branch-dream-journal", "unsafe", tmp_path / "dream")
    assert "caller_with_fake" in scoped["failedChecks"]
    assert "fake_contract" in dream["failedChecks"]


def test_changed_task_lock_is_not_silently_refreshed(tmp_path, monkeypatch):
    from memos.skill_branch_validation import tasks

    changed = tasks.manifest()
    changed["tasks"][0]["digest"] = "0" * 64
    path = tmp_path / "changed-lock.json"
    original = json.dumps(changed)
    path.write_text(original)
    monkeypatch.setattr(tasks, "LOCK", path)
    with pytest.raises(ValueError, match="branch_task_lock_changed"):
        list_tasks()
    assert path.read_text() == original


@pytest.mark.parametrize("task_id", ["branch-scoped-table", "branch-dream-journal"])
@pytest.mark.parametrize("variant", ["initial", "reference"])
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS no-fork executor")
def test_public_checks_run_without_hidden_assets(task_id, variant, tmp_path):
    from memos.skill_verification.executor import execute_program

    task = get_task(task_id)
    workspace = materialize(task_id, tmp_path / "workspace", variant)
    # The fixed wrapper only imports the exported public files.
    wrapper = (
        "import runpy,sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "runpy.run_path(str(Path(sys.argv[1])/'visible_checks.py'), run_name='__main__')\n"
    )
    result = execute_program(wrapper, workspace, tmp_path / "visible", 15)
    assert result["returncode"] == (0 if variant == "reference" else 1)
    assert not result["timed_out"] and not result["output_limited"]
    output = (tmp_path / "visible/stdout.txt").read_text()
    assert all(name + ":" in output for name in task.required_checks)
