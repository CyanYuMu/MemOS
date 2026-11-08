"""Visible preparation cannot silently repeat a failed calibration."""

import importlib.util
import json

from pathlib import Path
from types import SimpleNamespace

import pytest


def launcher():
    path = Path(__file__).resolve().parents[2] / "scripts/dev/skill_p4_console.py"
    spec = importlib.util.spec_from_file_location("p4_console", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_calibration_is_not_silently_retried(tmp_path, monkeypatch):
    module = launcher()
    calibration = tmp_path / "calibration"
    calibration.mkdir()
    (calibration / "summary.json").write_text(json.dumps({"results": [{"status": "failed"}]}))
    monkeypatch.setattr(
        module.subprocess, "run", lambda *_args, **_kw: pytest.fail("must not repeat model call")
    )
    with pytest.raises(ValueError, match="calibration_needs_review"):
        module.ensure_calibration(Path("config"), Path("candidate"), calibration)


def test_new_calibration_executes_in_visible_terminal_and_stops_on_failure(tmp_path, monkeypatch):
    module = launcher()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(ValueError, match="calibration_needs_review"):
        module.ensure_calibration(Path("config"), Path("candidate"), tmp_path / "new")
    assert len(calls) == 1
    assert "calibrate" in calls[0][0]
    assert "stdout" not in calls[0][1] and "stderr" not in calls[0][1]


def test_complete_calibration_reused_without_model_call(tmp_path, monkeypatch):
    module = launcher()
    (tmp_path / "summary.json").write_text(
        json.dumps(
            {"results": [{"group": g, "status": "passed"} for g in ["none", "raw_memory", "skill"]]}
        )
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *_args, **_kw: pytest.fail("must reuse fixed receipts")
    )
    module.ensure_calibration(Path("config"), Path("candidate"), tmp_path)
