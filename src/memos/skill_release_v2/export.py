"""Read-only controls for the v2 protocol probe, separate from Skill publication.

These are run controls, not a new candidate or a published Skill export. The
candidate exporter will require a separate fixed revision and admission record.
"""

from __future__ import annotations

import hashlib
import json
import shlex

from pathlib import Path

from memos.skill_adapter.runner import RunConfig, validate_config


PROFILE_VERSION = "protocol-probe-controls-1"
MCP_TOOLS = ("mcp__memos_v2__record_rule", "mcp__memos_v2__run_visible_checks")
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


def prepare_control(
    destination: Path,
    *,
    python: Path,
    hook_context: dict,
    mcp_context: dict,
    client_root: Path | None = None,
) -> dict:
    """Create once. Credential contents stay in private per-run files, never reports."""
    destination = Path(destination)
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError("control_symlink")
    destination.mkdir(mode=0o700, exist_ok=False)
    client_root = client_root or Path(__file__).parent
    for name in ("hook_client.py", "mcp_server.py"):
        source = client_root / name
        if source.is_symlink() or not source.is_file():
            raise ValueError("client_source_invalid")
        (destination / name).write_bytes(source.read_bytes())
        (destination / name).chmod(0o600)
    _write(destination / "hook-context.json", hook_context)
    _write(destination / "mcp-context.json", mcp_context)
    hook = shlex.join(
        [
            str(python),
            "-I",
            "-S",
            "-B",
            str(destination / "hook_client.py"),
            "--config",
            str(destination / "hook-context.json"),
        ]
    )
    _write(
        destination / "settings.json",
        {
            "disableBundledSkills": True,
            "autoMemoryEnabled": False,
            "syncClaudeAiSkills": False,
            "syncClaudeAiPlugins": False,
            "hooks": {
                event: [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": hook,
                                "timeout": 5,
                                "onFailure": "block",
                            }
                        ],
                    }
                ]
                for event in HOOK_EVENTS
            },
        },
    )
    _write(
        destination / "mcp.json",
        {
            "mcpServers": {
                "memos_v2": {
                    "command": str(python),
                    "args": [
                        "-I",
                        "-S",
                        "-B",
                        str(destination / "mcp_server.py"),
                        "--config",
                        str(destination / "mcp-context.json"),
                    ],
                }
            }
        },
    )
    return {
        "version": PROFILE_VERSION,
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(destination.iterdir())
        },
    }


def command_for(config: RunConfig, control: Path, prompt: str, session_id: str) -> list[str]:
    """Use the exact new namespace and no implicit Skill dispatch for G1 probes."""
    validate_config(config)
    if prompt.lstrip().startswith("/"):
        raise ValueError("unexpected_skill_dispatch")
    return [
        str(config.cli_path.resolve()),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--setting-sources",
        "project",
        "--settings",
        str(control / "settings.json"),
        "--strict-mcp-config",
        "--mcp-config",
        str(control / "mcp.json"),
        "--tools",
        ",".join(config.tools),
        "--allowedTools",
        ",".join((*config.tools, *MCP_TOOLS)),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--no-chrome",
        "--session-id",
        session_id,
        "--max-turns",
        str(config.max_turns),
        "--max-budget-usd",
        str(config.max_budget_usd),
        "--model",
        config.model,
        "--effort",
        config.effort,
    ]
