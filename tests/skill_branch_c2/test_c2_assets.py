"""C2 repairs scoped reads while retaining an initially healthy Dream contract."""

import json

import pytest

from memos.skill_branch_c2 import tasks
from memos.skill_branch_extensions.combined import CALLER, FAKE, READ_BROKEN
from memos.skill_branch_extensions.tasks import get_suite as c1_suite


SUITE = "scoped-healthy-dream-c2-v1"


def test_c2_initial_has_only_scoped_defect_and_preserves_c1_identity():
    original = c1_suite("combined-c1-v1")
    suite = tasks.get_suite(SUITE)
    assert suite.task_id == "branch-scoped-healthy-dream"
    assert suite.initial_files["caller.py"] == CALLER
    assert suite.initial_files["fake_graph.py"] == READ_BROKEN
    assert suite.variants["reference"]["fake_graph.py"] == FAKE
    assert suite.lineage_id == original.lineage_id
    assert suite.provenance["independentSourceSamples"] == 0
    assert suite.target_branches == (
        "confirm_contract#3",
        "repair_scoped_reads#0",
        "verify_scoped_reads#1",
        "verify_dream_update#0",
    )
    for mode in tasks.MODES:
        assert suite.expected_failures["initial"][mode] == (
            "scoped.fake_scope_contract",
            "scoped.scope_generalization",
        )
    assert tasks.suite_manifest(suite) == json.loads(tasks.lock_path(SUITE).read_text())
    assert c1_suite("combined-c1-v1") == original
    with pytest.raises(ValueError, match="unknown_extension_suite"):
        tasks.get_suite("combined-c1-v1")


def test_public_export_has_no_hidden_variant_or_target_branch_hint(tmp_path):
    suite = tasks.get_suite(SUITE)
    path = tasks.materialize(SUITE, tmp_path / "workspace")
    assert {p.name: p.read_text() for p in path.iterdir()} == suite.initial_files
    assert set(suite.initial_files) == {
        "TASK.md",
        "caller.py",
        "fake_graph.py",
        "visible_checks.py",
    }
    assert "verify_scoped_reads#1" not in suite.initial_files["TASK.md"]
    with pytest.raises(TypeError):
        tasks.materialize(SUITE, tmp_path / "answer", variant="reference")
    with pytest.raises(FileExistsError):
        tasks.materialize(SUITE, path)


def test_lock_damage_is_rejected_without_rewriting(tmp_path, monkeypatch):
    lock = tmp_path / "lock.json"
    lock.write_text("{}")
    monkeypatch.setattr(tasks, "lock_path", lambda _: lock)
    with pytest.raises(ValueError, match="extension_suite_lock_changed"):
        tasks.get_suite(SUITE)
    assert lock.read_text() == "{}"


def test_expected_matrix_contains_dream_regression_mutants():
    suite = tasks.get_suite(SUITE)
    assert tuple(suite.variants) == tasks.VARIANTS
    for mode in tasks.MODES:
        assert suite.expected_failures["unsafe_dream_caller"][mode] == (
            "dream.caller_contract",
            "dream.caller_with_fake",
        )
        assert suite.expected_failures["unsafe_merge_sideeffect"][mode] == ("dream.other_actions",)
