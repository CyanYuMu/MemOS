"""Standalone command hook: fixed dispatch, immutable Skill and public event metadata.

This file is copied into a read-only control directory and runs without MemOS imports.
It writes no files. Claude's native hook stream carries its bounded output to the host.
"""

import hashlib
import json
import sys

from pathlib import Path


HOOK_MARKER = "MEMOS_HOOK_EVENT:"
OBSERVED_FIELDS = (
    "hook_event_name",
    "session_id",
    "prompt_id",
    "tool_use_id",
    "tool_name",
    "command_name",
    "command_source",
    "expansion_type",
    "is_interrupt",
)


def handle_hook(payload: dict, context: dict) -> tuple[dict, int]:
    event = payload.get("hook_event_name")
    reason = None
    path = context.get("skill_path")
    if path:
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            digest = None
        if digest != context["skill_sha256"]:
            reason = "artifact_mismatch"
    if event == "UserPromptExpansion" and (
        payload.get("command_name") != context.get("runtime_alias")
        or payload.get("expansion_type", "slash_command") != "slash_command"
    ):
        reason = "unknown_command"
    observation = {
        key: value
        for key in OBSERVED_FIELDS
        if isinstance(value := payload.get(key), str | bool) and len(str(value)) <= 512
    }
    observation["gate"] = "blocked" if reason else "allowed"
    observation["reason_code"] = reason
    output = {
        "systemMessage": HOOK_MARKER + json.dumps(observation, ensure_ascii=False, sort_keys=True),
    }
    if reason:
        output.update({"decision": "block", "reason": reason})
        if event == "PreToolUse":
            output["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        return output, 2
    return output, 0


def main() -> int:
    try:
        context = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("oversized hook input")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(context, dict):
            raise ValueError("invalid hook input")
        output, code = handle_hook(payload, context)
    except (OSError, ValueError, KeyError, IndexError):
        output, code = {"decision": "block", "reason": "capture_incomplete"}, 2
    print(json.dumps(output, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
