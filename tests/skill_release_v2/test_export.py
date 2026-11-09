"""Execution profile is fixed separately from a candidate Skill revision."""

import json
import sys

from pathlib import Path

import pytest

from memos.skill_adapter.runner import RunConfig
from memos.skill_release_v2.export import command_for, prepare_control


def context(role):
    return {"role": role, "token": "private-example", "session_id": "session"}


def test_control_uses_only_new_namespace_and_blocking_tool_hooks(tmp_path):
    # Client contents are test inputs; actual runs bind installed producer bytes.
    clients = tmp_path / "clients"
    clients.mkdir()
    for name in ("hook_client.py", "mcp_server.py"):
        (clients / name).write_text('print("test-only")\n')
    manifest = prepare_control(
        tmp_path / "control",
        python=Path(sys.executable).resolve(),
        hook_context=context("hook"),
        mcp_context=context("mcp"),
        client_root=clients,
    )
    settings = json.loads((tmp_path / "control/settings.json").read_text())
    assert set(settings["hooks"]) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert all(
        row["hooks"][0]["onFailure"] == "block"
        for entries in settings["hooks"].values()
        for row in entries
    )
    mcp = json.loads((tmp_path / "control/mcp.json").read_text())
    assert set(mcp["mcpServers"]) == {"memos_v2"}
    assert len(manifest["files"]) == 6
    assert "private-example" not in json.dumps(manifest)


def test_command_cannot_load_old_mcp_or_implicit_configuration(tmp_path):
    cfg = RunConfig(cli_path=Path(sys.executable), model="claude-opus-5-5")
    args = command_for(cfg, tmp_path, "read task", "session")
    assert args[args.index("--tools") + 1] == "Read,Edit,Write"
    allowed = args[args.index("--allowedTools") + 1]
    assert "mcp__memos_v2__record_rule" in allowed and "memos_run" not in allowed
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in args and "--no-chrome" in args
    assert args[args.index("--setting-sources") + 1] == "project"


def test_protocol_probe_does_not_implicitly_dispatch_a_skill(tmp_path):
    cfg = RunConfig(cli_path=Path(sys.executable), model="claude-opus-5-5")
    with pytest.raises(ValueError, match="unexpected_skill_dispatch"):
        command_for(cfg, tmp_path, "/unfixed-latest", "session")


def test_existing_control_is_not_replaced(tmp_path):
    destination = tmp_path / "control"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        prepare_control(
            destination,
            python=Path(sys.executable),
            hook_context=context("hook"),
            mcp_context=context("mcp"),
            client_root=tmp_path,
        )
