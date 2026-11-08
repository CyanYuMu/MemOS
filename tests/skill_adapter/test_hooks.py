"""Command hooks enforce fixed dispatch and expose only bounded public event fields."""

import json
import subprocess
import sys

from pathlib import Path

from memos.skill_adapter.hooks import handle_hook


def context(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("frozen skill")
    import hashlib

    return {
        "runtime_alias": "memos-probe-r1",
        "skill_path": str(skill),
        "skill_sha256": hashlib.sha256(skill.read_bytes()).hexdigest(),
        "workspace": str(tmp_path / "workspace"),
        "run_id": "run-one",
        "attempt_id": "attempt-one",
    }


def test_unknown_native_command_is_blocked(tmp_path):
    output, code = handle_hook(
        {"hook_event_name": "UserPromptExpansion", "command_name": "unknown"}, context(tmp_path)
    )
    assert code == 2
    assert output["decision"] == "block"


def test_changed_skill_is_blocked_before_next_tool(tmp_path):
    config = context(tmp_path)
    Path(config["skill_path"]).write_text("changed")
    output, code = handle_hook(
        {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_use_id": "tool-one"}, config
    )
    assert code == 2
    assert "artifact_mismatch" in output["reason"]


def test_hook_output_is_observable_without_saving_transcript_or_thinking(tmp_path):
    output, code = handle_hook(
        {
            "hook_event_name": "UserPromptExpansion",
            "command_name": "memos-probe-r1",
            "command_source": "project",
            "expansion_type": "slash_command",
            "session_id": "native-session",
            "transcript_path": "/private/transcript.jsonl",
            "thinking": "private reasoning",
            "prompt": "/memos-probe-r1",
        },
        context(tmp_path),
    )
    assert code == 0
    text = json.dumps(output)
    assert "MEMOS_HOOK_EVENT" in text
    assert "native-session" in text
    assert "private reasoning" not in text
    assert "/private/transcript" not in text


def test_hook_file_runs_without_importing_the_memos_package(tmp_path):
    import memos.skill_adapter.hooks as module

    config = tmp_path / "context.json"
    config.write_text(json.dumps(context(tmp_path)))
    process = subprocess.run(
        [sys.executable, "-I", "-B", module.__file__, str(config)],
        input=json.dumps({"hook_event_name": "SessionStart", "session_id": "native-session"}),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["systemMessage"].startswith("MEMOS_HOOK_EVENT:")
