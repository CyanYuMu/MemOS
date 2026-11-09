"""Fixed C2 orchestration keeps model fixtures distinct from actual Claude evidence."""

import sys

from pathlib import Path

import pytest

from memos.skill_adapter.runner import RunConfig
from memos.skill_branch_c2 import runner
from memos.skill_branch_c2.tasks import get_suite, suite_digest


def test_qualification_failure_precedes_cli_and_output(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("CLI called"))
    with pytest.raises(ValueError, match="qualification"):
        runner.run_extension_task(
            "scoped-healthy-dream-c2-v1",
            tmp_path / "run",
            RunConfig(cli_path=Path(sys.executable), model="test"),
            qualification_directory=tmp_path / "qualification",
            group="none",
            experiment_id="a" * 64,
            expected_suite_digest=suite_digest(get_suite("scoped-healthy-dream-c2-v1")),
        )
    assert not (tmp_path / "run").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS isolation")
@pytest.mark.parametrize(
    "mode", ["healthy", "regression", "missing", "wrong", "after_final", "over_budget", "none"]
)
def test_real_isolated_checks_and_native_process_binding(tmp_path, monkeypatch, mode):
    from c2_run_fixture import make_run

    fixture = make_run(
        tmp_path,
        monkeypatch,
        group="none" if mode == "none" else "skill",
        dream_regression=mode == "regression",
        delivery_mode=mode if mode in {"missing", "wrong"} else None,
        after_final=mode == "after_final",
        over_budget=mode == "over_budget",
    )
    report = fixture["report"]
    process = report["process_evidence"]
    assert report["branch_coverage"] == [] and not report["effectiveness_evaluation"]
    assert process["complete"] is (mode not in {"missing", "wrong", "after_final"})
    assert process["cyclesUsed"] == (
        3
        if mode == "over_budget"
        else 0
        if mode in {"missing", "wrong"}
        else 2
        if mode == "regression"
        else 1
    )
    assert process["budgetExceeded"] is (mode == "over_budget")
    assert report["intermediate_verifications"] == []
    if mode == "after_final":
        assert process["unverifiedEdits"]
    assert report["status"] == (
        "incomplete" if mode in {"missing", "wrong", "after_final"} else "passed"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS isolation")
def test_cancellation_preserves_task_without_starting_hidden_acceptance(tmp_path, monkeypatch):
    from c2_run_fixture import make_run

    fixture = make_run(tmp_path, monkeypatch, cancelled=True)
    report = fixture["report"]
    assert report["status"] == "incomplete"
    assert report["execution"]["cancelled"]
    assert "receipt" not in report and report["intermediate_verifications"] == []
    assert (fixture["run"] / "candidate").exists()
