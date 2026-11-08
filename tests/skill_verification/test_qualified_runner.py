"""The new runner requires a fresh isolated self-test before any provider call."""

import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.runner import RunConfig
from memos.skill_evaluation.tasks import case_digest, get_case
from memos.skill_verification import runner


def test_missing_qualification_stops_before_cli_probe_or_output(tmp_path, monkeypatch):
    calls = []

    def reject(path):
        calls.append(path)
        raise ValueError("qualification_missing")

    monkeypatch.setattr(runner, "require_qualification", reject)
    monkeypatch.setattr(
        runner.subprocess, "run", lambda *a, **kw: pytest.fail("must not probe or call CLI")
    )
    case = get_case("holdout-fixture")
    with pytest.raises(ValueError, match="qualification_missing"):
        runner.run_qualified_case(
            case.case_id,
            tmp_path / "run",
            RunConfig(cli_path=Path(sys.executable), model="pinned-test"),
            qualification_directory=tmp_path / "absent",
            group="none",
            experiment_id="a" * 64,
            expected_case_digest=case_digest(case),
        )
    assert calls == [tmp_path / "absent"]
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("qualification_changed", [False, True])
def test_fixture_host_pipeline_uses_new_broker_receipt_and_retains_qualification(
    tmp_path, monkeypatch, qualification_changed
):
    from memos.skill_evaluation.verifier import check_receipt as old_check
    from memos.skill_verification.verifier import check_receipt

    cli = tmp_path / "cli-bin/claude"
    cli.parent.mkdir()
    cli.write_text('#!/bin/sh\nprintf "2.1.296 (Claude Code)\\n"\n')
    cli.chmod(0o700)
    config = RunConfig(cli_path=cli, model="pinned-host-fixture")
    case = get_case("holdout-fixture")
    qualification = {"qualification_id": "b" * 64}
    qualification_reads = []

    def qualify(_):
        qualification_reads.append(1)
        if qualification_changed and len(qualification_reads) > 1:
            return {"qualification_id": "c" * 64}
        return qualification

    monkeypatch.setattr(runner, "require_qualification", qualify)

    def simulated_native(command, cwd, env, journal, timeout):
        # Explicit host fixture: this proves orchestration, not model effectiveness.
        session = command[command.index("--session-id") + 1]
        tools = command[command.index("--allowedTools") + 1].split(",")
        journal.ingest_stream(
            {
                "type": "system",
                "subtype": "init",
                "session_id": session,
                "model": config.model,
                "claude_code_version": config.expected_version,
                "permissionMode": "dontAsk",
                "tools": tools,
                "skills": ["doctor", "plugin-authoring"],
                "slash_commands": [],
                "plugins": [],
                "mcp_servers": [{"name": "memos_run", "status": "connected"}],
            }
        )
        for name in ("SessionStart", "UserPromptSubmit", "Stop"):
            journal.ingest_hook({"hook_event_name": name, "session_id": session, "gate": "allowed"})
        for name, content in case.reference_files.items():
            (cwd / name).write_text(content)
        journal.ingest_stream(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session,
                "result": "host fixture complete",
                "total_cost_usd": 0,
            }
        )
        return {"returncode": 0, "timed_out": False, "elapsed_seconds": 0.01}

    monkeypatch.setattr(runner, "capture_process", simulated_native)
    path = runner.run_qualified_case(
        case.case_id,
        tmp_path / "run",
        config,
        qualification_directory=tmp_path / "selftest",
        group="none",
        experiment_id="a" * 64,
        expected_case_digest=case_digest(case),
    )
    report = json.loads(path.read_text())
    assert report["status"] == ("incomplete" if qualification_changed else "passed"), report
    if qualification_changed:
        assert "verification_qualification_changed" in report["capture"]["reasons"]
    assert report["task_accepted"] is True
    assert report["verification_qualification"] == qualification
    assert report["purpose"] == "verifier_compatibility_validation"
    assert report["effectiveness_evaluation"] is False
    assert report["qualified_agent_experience"] is False
    receipt = Path(report["receipt"])
    assert check_receipt(receipt, path.parent / "candidate", path.parent / "validation").matches
    assert not old_check(receipt, path.parent / "candidate", path.parent / "validation").matches
