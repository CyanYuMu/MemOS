"""A stopped visible compatibility run is retained, never silently repeated."""

import importlib.util
import json
import sys

from pathlib import Path

import pytest


def launcher():
    path = Path(__file__).resolve().parents[2] / "scripts/dev/skill_p41_console.py"
    spec = importlib.util.spec_from_file_location("p41_console", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_incomplete_qualification_is_not_overwritten_or_rerun(tmp_path, monkeypatch):
    from memos.skill_verification import selftest

    module = launcher()
    (tmp_path / "marker").write_text("original")

    def reject(_):
        raise ValueError("qualification_incomplete")

    monkeypatch.setattr(selftest, "require_qualification", reject)
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **kw: pytest.fail("must not overwrite self-test")
    )
    with pytest.raises(ValueError, match="qualification_incomplete"):
        module.ensure_qualification(tmp_path)
    assert (tmp_path / "marker").read_text() == "original"


@pytest.mark.parametrize("existing", ["partial", "unverified_success"])
def test_existing_model_attempt_never_silently_repeats(tmp_path, monkeypatch, existing):
    from memos.skill_verification import runner, selftest

    module = launcher()
    monkeypatch.setattr(selftest, "require_qualification", lambda _: {"qualification_id": "a" * 64})
    monkeypatch.setattr(runner, "adapter_digest", lambda: "b" * 64)
    monkeypatch.setattr(
        runner, "run_qualified_case", lambda *a, **kw: pytest.fail("must not repeat model call")
    )
    output = tmp_path / "run"
    output.mkdir()
    if existing == "unverified_success":
        (output / "report.json").write_text(json.dumps({"status": "passed"}))
    service = {
        "runtime": {
            "cliPath": sys.executable,
            "model": "pinned-test",
            "timeoutSeconds": 240,
            "maxTurns": 12,
            "maxBudgetUsd": 3,
            "providerEnvironment": {},
        }
    }
    with pytest.raises(ValueError, match="native_probe_needs_review"):
        module.ensure_native_probe(service, tmp_path / "qualification", output)
    assert output.exists()
