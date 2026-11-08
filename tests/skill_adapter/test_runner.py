"""The runner must reject invalid launches and preserve partial native evidence."""

import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.events import EventRecorder
from memos.skill_adapter.runner import (
    RunConfig,
    capture_process,
    command_for,
    context_matches,
    successful_run,
    validate_config,
)


def config(**changes):
    return RunConfig(cli_path=Path(sys.executable), model="claude-test-pinned-1", **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": 0},
        {"max_turns": 0},
        {"max_budget_usd": -1},
        {"tools": ("WebFetch",)},
        {"tools": ("Bash",)},
    ],
)
def test_invalid_runtime_policy_never_reaches_a_process(changes):
    with pytest.raises(ValueError):
        validate_config(config(**changes))


def test_floating_model_and_unknown_alias_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="fixed model"):
        validate_config(RunConfig(cli_path=Path(sys.executable), model="opus"))
    with pytest.raises(ValueError, match="unknown_command"):
        command_for(config(), tmp_path, "/unregistered", "session", runtime_alias="expected")
    with pytest.raises(ValueError, match="fixed model"):
        validate_config(RunConfig(cli_path=Path(sys.executable), model="opus[1m]"))


def test_native_command_pins_context_and_does_not_bypass_permissions(tmp_path):
    command = command_for(config(), tmp_path, "/expected", "session", runtime_alias="expected")
    assert "--bare" not in command
    assert "--dangerously-skip-permissions" not in command
    assert command[command.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in command
    assert "--include-hook-events" in command
    assert "--no-session-persistence" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"


def recorder(tmp_path):
    return EventRecorder(
        tmp_path / "events.jsonl",
        {"run_id": "run", "attempt_id": "attempt"},
        secret_values=("private-credential-value",),
    )


def test_stream_is_projected_before_persistence_and_hook_failures_stay_partial(tmp_path):
    messages = [
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "UserPromptSubmit",
            "hook_id": "h1",
            "exit_code": 1,
            "stdout": "",
            "stderr": "private-credential-value",
        },
        {
            "type": "assistant",
            "session_id": "s1",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "never persist private thought"},
                    {"type": "text", "text": "private-credential-value"},
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "s1",
            "result": "Hook blocked",
        },
    ]
    script = "\n".join("print(" + repr(json.dumps(row)) + ", flush=True)" for row in messages)
    journal = recorder(tmp_path)
    process = capture_process([sys.executable, "-I", "-c", script], tmp_path, {}, journal, 5)
    result = journal.finish()
    assert process["returncode"] == 0
    assert result["status"] == "partial"
    assert "hook_failed" in result["reasons"]
    saved = (tmp_path / "events.jsonl").read_text()
    assert "private-credential-value" not in saved
    assert "never persist private thought" not in saved


def test_timeout_does_not_become_a_successful_capture(tmp_path):
    journal = recorder(tmp_path)
    result = capture_process(
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"], tmp_path, {}, journal, 0.1
    )
    assert result["timed_out"]
    assert journal.finish()["status"] == "partial"


def test_invalid_json_is_not_stored_as_raw_content(tmp_path):
    journal = recorder(tmp_path)
    capture_process(
        [sys.executable, "-I", "-c", "print('unstructured private thought')"],
        tmp_path,
        {},
        journal,
        5,
    )
    assert journal.finish()["status"] == "partial"
    assert "unstructured private thought" not in (tmp_path / "events.jsonl").read_text()


def test_native_error_cannot_pass_even_with_successful_task_receipt():
    capture = {"status": "complete", "native_outcome": "reported_error"}
    assert not successful_run(capture, {"returncode": 0}, "verified", True, True)
    capture["native_outcome"] = "reported_success"
    assert successful_run(capture, {"returncode": 0}, "verified", True, True)
    assert not successful_run(capture, {"returncode": 0}, "verified", True, False)


def test_unexpected_context_fails_even_if_skill_alias_is_discovered():
    expected = {
        "model": "claude-test-pinned-1",
        "claude_code_version": "2.1.296",
        "permissionMode": "dontAsk",
        "tools": [
            "Read",
            "Edit",
            "Write",
            "mcp__memos_run__run_visible_checks",
            "mcp__memos_run__record_rule",
        ],
        "skills": ["locked", "doctor", "plugin-authoring"],
        "plugins": [],
        "mcp_servers": [{"name": "memos_run", "status": "connected"}],
    }
    events = [{"kind": "session_init", "payload": expected}]
    assert context_matches(events, config(), "locked")
    expected["skills"].append("unrequested")
    assert not context_matches(events, config(), "locked")
    expected["skills"].pop()
    expected["model"] = "another-model"
    assert not context_matches(events, config(), "locked")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS runner")
def test_post_launch_failure_preserves_partial_report_and_quarantines_unsafe_tree(
    tmp_path, monkeypatch
):
    from memos.skill_adapter import runner

    cli = tmp_path / "cli" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\necho '2.1.296 (Claude Code)'\n")
    cli.chmod(0o700)
    monkeypatch.setattr(runner, "_managed_configuration_present", lambda: False)
    monkeypatch.setattr(runner, "probe_isolation", lambda _: {"passed": True})

    def fail_after_edit(command, cwd, environment, journal, timeout):
        (cwd / "escape").symlink_to("/tmp")
        journal.mark_incomplete("injected_failure")
        raise ValueError("sensitive error text must not reach report")

    monkeypatch.setattr(runner, "capture_process", fail_after_edit)
    path = runner.run_case(
        "dev-strict-signature",
        tmp_path / "output",
        RunConfig(cli_path=cli, model="claude-test-pinned-1"),
    )
    report = json.loads(path.read_text())
    assert report["status"] == "incomplete"
    assert report["candidate_quarantined"]
    assert report["candidate_snapshot"] is None
    assert report["capture"]["status"] == "partial"
    assert report["events_sha256"]
    assert "sensitive error text" not in path.read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS runner")
@pytest.mark.parametrize(
    "case_id,tools",
    [
        ("source-dream-update", ("Read", "Edit")),
        ("source-scoped-update", ("Read", "Write")),
        ("dev-strict-signature", ("Read", "Edit", "Write")),
    ],
)
def test_actual_launch_prompt_explains_configured_tools_and_task_boundaries(
    tmp_path, monkeypatch, case_id, tools
):
    from memos.skill_adapter import runner
    from memos.skill_evaluation.tasks import get_case

    cli = tmp_path / "cli" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\necho '2.1.296 (Claude Code)'\n")
    cli.chmod(0o700)
    monkeypatch.setattr(runner, "_managed_configuration_present", lambda: False)
    monkeypatch.setattr(runner, "probe_isolation", lambda _: {"passed": True})
    commands = []

    def capture_launch(command, cwd, environment, journal, timeout):
        commands.append(command)
        raise ValueError("test stops before launching the Agent")

    monkeypatch.setattr(runner, "capture_process", capture_launch)
    path = runner.run_case(
        case_id,
        tmp_path / "output",
        RunConfig(cli_path=cli, model="claude-test-pinned-1", tools=tools),
    )
    assert len(commands) == 1
    command = commands[0]
    prompt = command[command.index("--output-format") - 1]
    available = (*tools, "mcp__memos_run__run_visible_checks")
    assert command[command.index("--allowedTools") + 1] == ",".join(available)
    assert "Available tools: " + ", ".join(available) + "." in prompt
    case = get_case(case_id)
    expected_paths = case.editable_paths or ("caller.py", "fake_graph.py")
    assert "Editable task paths: " + ", ".join(expected_paths) + "." in prompt
    assert "Do not request Bash, shell commands, or arbitrary program execution." in prompt
    assert "Do not create scratch files, temporary tests, or extra files" in prompt
    assert "do not write outside the task workspace" in prompt
    assert "Keep TASK.md and visible_checks.py unchanged." in prompt
    assert "Run checks only with mcp__memos_run__run_visible_checks." in prompt
    assert "The host performs additional independent acceptance" in prompt
    assert "without a Skill" in prompt
    assert not prompt.startswith("/")
    assert json.loads(path.read_text())["skill_export"] is None
