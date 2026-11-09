"""The combined suite is an asset qualification, never an Agent evaluation."""

import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.isolation import IsolationUnavailableError
from memos.skill_branch_extensions import __main__ as cli
from memos.skill_branch_extensions import selftest, tasks
from memos.skill_branch_extensions.__main__ import main
from memos.skill_branch_extensions.selftest import audit_suite


SUITE = "combined-c1-v1"


def test_explicit_suite_lock_and_group_identity():
    suite = tasks.get_suite(SUITE)
    assert suite.suite_id == SUITE
    assert len(suite.checks) == 11
    assert len({row["id"] for row in suite.checks}) == 11
    assert {row["group"] for row in suite.checks} == {"scoped", "dream"}
    assert [row["ruleCheck"] for row in suite.checks].count("caller_contract") == 2
    assert tasks.suite_manifest(suite) == json.loads(tasks.lock_path(SUITE).read_text())
    with pytest.raises(ValueError, match="unknown_extension_suite"):
        tasks.get_suite("unknown")


def test_public_materialization_exports_only_initial(tmp_path):
    suite = tasks.get_suite(SUITE)
    path = tasks.materialize(SUITE, tmp_path / "candidate")
    assert {p.name: p.read_text() for p in path.iterdir()} == suite.initial_files
    assert set(suite.initial_files) == {
        "TASK.md",
        "caller.py",
        "fake_graph.py",
        "visible_checks.py",
    }
    with pytest.raises(FileExistsError):
        tasks.materialize(SUITE, path)
    with pytest.raises(TypeError):
        tasks.materialize(SUITE, tmp_path / "private-answer", variant="reference")


def test_changed_lock_is_rejected_without_rewriting(tmp_path, monkeypatch):
    original = tasks.lock_path(SUITE).read_bytes()
    lock = tmp_path / "bad-lock.json"
    lock.write_text("{}")
    monkeypatch.setattr(tasks, "lock_path", lambda _: lock)
    with pytest.raises(ValueError, match="extension_suite_lock_changed"):
        tasks.get_suite(SUITE)
    assert lock.read_text() == "{}"
    assert original != lock.read_bytes()


def test_cli_prepares_initial_and_rejects_private_variant(tmp_path, capsys):
    target = tmp_path / "initial"
    assert main(["prepare", "--suite", SUITE, "--workspace", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["variant"] == "initial"
    with pytest.raises(SystemExit) as error:
        main(
            [
                "prepare",
                "--suite",
                SUITE,
                "--workspace",
                str(tmp_path / "answer"),
                "--variant",
                "reference",
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "answer").exists()


def test_unrelated_suite_file_cannot_change_selected_identity(tmp_path, monkeypatch):
    suite = tasks.get_suite(SUITE)
    first = tasks.suite_digest(suite)
    selected = tmp_path / "combined-c1-v1.json"
    selected.write_text(json.dumps(tasks.suite_manifest(suite)))
    monkeypatch.setattr(tasks, "lock_path", lambda _: selected)
    (tmp_path / "future-c2.json").write_text('{"unrelated":"future suite"}')
    assert tasks.suite_digest(tasks.get_suite(SUITE)) == first


def test_cli_refuses_an_unsupported_isolation_backend(tmp_path, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise IsolationUnavailableError("unsupported test backend")

    monkeypatch.setattr(cli, "audit_suite", unavailable)
    assert main(["audit", "--suite", SUITE, "--output", str(tmp_path / "audit")]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["errorType"] == "IsolationUnavailableError"
    assert not (tmp_path / "audit").exists()


def test_audit_pins_its_initial_identity_even_if_a_loaded_object_is_mutated(tmp_path, monkeypatch):
    suite = tasks.get_suite(SUITE)
    initial_digest = tasks.suite_digest(suite)
    monkeypatch.setattr(selftest, "get_suite", lambda _: suite)

    def simulated_variant(*args, **kwargs):
        suite.provenance["changed_during_audit"] = True
        return {
            "suiteDigest": tasks.suite_digest(suite),
            "expectedSuiteDigest": tasks.suite_digest(suite),
            "suiteBindingIntact": True,
            "matchesExpectation": True,
        }

    monkeypatch.setattr(selftest, "check_variant", simulated_variant)
    result = selftest.audit_suite(SUITE, tmp_path / "audit")
    assert result["passed"] is False
    assert result["suiteDigest"] == initial_digest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS fixed no-fork executor")
def test_six_variants_actual_public_and_hidden_matrix(tmp_path):
    report = audit_suite(SUITE, tmp_path / "matrix")
    assert report["passed"]
    assert len(report["results"]) == 12
    assert report["modelCalls"] == report["humanApprovals"] == 0
    assert report["branchCoverage"] == []
    assert report["qualifiedAgentExperience"] is False
    rows = {(r["variant"], r["mode"]): r for r in report["results"]}
    for mode in ("public", "hidden"):
        assert rows["reference", mode]["status"] == "passed"
        assert rows["scoped_only", mode]["groupStatus"] == {"scoped": "passed", "dream": "failed"}
        assert rows["dream_only", mode]["groupStatus"] == {"scoped": "failed", "dream": "passed"}
        assert rows["unsafe_empty_read", mode]["groupStatus"] == {
            "scoped": "failed",
            "dream": "passed",
        }
        assert rows["unsafe_update_arguments", mode]["groupStatus"] == {
            "scoped": "passed",
            "dream": "failed",
        }
    for row in rows.values():
        assert row["matchesExpectation"] and row["candidateUnchanged"]
        assert Path(row["snapshotManifest"]).is_file()
        assert Path(row["executionRecord"]).is_file()
