"""Fixed batch scheduling must not call additional models after a failed calibration."""

import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.artifacts import SkillRevision
from memos.skill_adapter.runner import RunConfig
from memos.skill_branch_c2 import batch
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION


@pytest.mark.parametrize("calibration", ["passed", "incomplete", "cancelled"])
def test_fixed_two_units_calibration_gate_and_no_secret_output(tmp_path, monkeypatch, calibration):
    config_path = tmp_path / "service.json"
    config_path.write_text("private-provider-token")
    config = {"runtime": {"providerEnvironment": {"ANTHROPIC_API_KEY": "private-provider-token"}}}
    monkeypatch.setattr(batch, "read_config", lambda _: config)
    monkeypatch.setattr(
        batch, "runtime", lambda _: RunConfig(cli_path=Path(sys.executable), model="test")
    )
    candidate = {"revision": CANDIDATE_REVISION, "content_hash": "a" * 64}
    monkeypatch.setattr(batch, "load_candidate", lambda _: {"candidate": candidate})
    monkeypatch.setattr(batch, "export_identity", lambda _: {"exportHash": "b" * 64})
    monkeypatch.setattr(
        batch,
        "native_skill",
        lambda _: SkillRevision("test", CANDIDATE_REVISION, "Fixed", ("rule",)),
    )
    monkeypatch.setattr(
        batch, "require_qualification", lambda _, **kw: {"qualificationId": "c" * 64}
    )
    calls = []

    def run(task_id, output, runtime, **kwargs):
        assert (output.parents[1] / "manifest.json").is_file()
        calls.append((task_id, kwargs["group"]))
        if calibration == "cancelled":
            raise KeyboardInterrupt()
        output.mkdir(parents=True)
        report = {
            "status": calibration if kwargs["group"] == "none" else "passed",
            "capture": {"status": "complete"},
            "task_accepted": True,
            "loading": {"status": "verified"},
        }
        path = output / "report.json"
        path.write_text(json.dumps(report))
        return path

    monkeypatch.setattr(batch, "run_extension_task", run)
    output = tmp_path / "batch"
    if calibration == "cancelled":
        with pytest.raises(KeyboardInterrupt):
            batch.run_batch(config_path, tmp_path / "candidate", tmp_path / "qualification", output)
        assert len(calls) == 1
        return
    result = batch.run_batch(
        config_path, tmp_path / "candidate", tmp_path / "qualification", output
    )
    assert len(calls) == (2 if calibration == "passed" else 1)
    assert result["complete"] is (calibration == "passed")
    assert "private-provider-token" not in (output / "manifest.json").read_text()
    assert "private-provider-token" not in (output / "summary.json").read_text()
    with pytest.raises(ValueError, match="already_exists"):
        batch.run_batch(config_path, tmp_path / "candidate", tmp_path / "qualification", output)
    assert len(calls) == (2 if calibration == "passed" else 1)
