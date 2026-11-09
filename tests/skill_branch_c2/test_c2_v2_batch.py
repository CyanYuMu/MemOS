"""Fixed batch scheduling must not call additional models after a failed calibration."""

import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.artifacts import SkillRevision
from memos.skill_adapter.runner import RunConfig
from memos.skill_branch_c2 import batch_v2 as batch
from memos.skill_branch_c2.execution_policy import policy_identity
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
            "loading": {"status": "not_requested" if kwargs["group"] == "none" else "verified"},
            "group": kwargs["group"],
            "execution_policy": policy_identity(),
            "process_evidence": {
                "complete": True,
                "maxCycles": 2,
                "cyclesUsed": 1,
                "budgetExceeded": False,
                "unverifiedEdits": False,
            },
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


@pytest.mark.parametrize("overbudget_arm", ["none", "skill"])
def test_overbudget_unit_preserves_passed_behavior_but_never_grants_plan_success(
    tmp_path, monkeypatch, overbudget_arm
):
    from types import SimpleNamespace

    identity = {
        "protocol": "c2-batch-2",
        "batchNonce": "f" * 32,
        "units": [
            {
                "id": "c2-none",
                "suiteId": "scoped-healthy-dream-c2-v1",
                "group": "none",
                "taskDigest": "d" * 64,
            },
            {
                "id": "c2-skill",
                "suiteId": "scoped-healthy-dream-c2-v1",
                "group": "skill",
                "taskDigest": "d" * 64,
            },
        ],
        "exportHash": "e" * 64,
    }
    monkeypatch.setattr(
        batch,
        "_identity",
        lambda *a, **k: (
            identity,
            {"runtime": {"providerEnvironment": {}}},
            SimpleNamespace(),
            None,
        ),
    )
    calls = []

    def run(suite, output, config, **kw):
        calls.append(kw["group"])
        over = kw["group"] == overbudget_arm
        output.mkdir(parents=True)
        path = output / "report.json"
        path.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "task_accepted": True,
                    "group": kw["group"],
                    "execution_policy": policy_identity(),
                    "capture": {"status": "complete"},
                    "loading": {"status": "not_requested" if kw["group"] == "none" else "verified"},
                    "process_evidence": {
                        "complete": True,
                        "maxCycles": 2,
                        "cyclesUsed": 3 if over else 1,
                        "budgetExceeded": over,
                        "unverifiedEdits": False,
                    },
                }
            )
        )
        return path

    monkeypatch.setattr(batch, "run_extension_task", run)
    result = batch.run_batch(
        tmp_path / "config", tmp_path / "candidate", tmp_path / "qual", tmp_path / "batch"
    )
    assert calls == (["none"] if overbudget_arm == "none" else ["none", "skill"])
    assert result["results"][0]["status"] == "passed"
    assert not result["success"] and not result["planQualified"]
    prefix = "calibration" if overbudget_arm == "none" else "trial"
    assert prefix + ":repair_budget_exceeded" in result["planIssues"]
