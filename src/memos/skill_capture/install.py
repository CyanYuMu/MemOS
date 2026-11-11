"""Add/remove our exact command while preserving unrelated Claude configuration."""

import json
import os
import subprocess
import tempfile
import time

from pathlib import Path

from .core import EVENTS


def _update(path, command, remove, previous_command=None):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("settings_symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else b"{}"
    value = json.loads(original)
    if value.get("disableAllHooks") and not remove:
        raise ValueError("hooks_disabled_by_user")
    hooks = value.setdefault("hooks", {})
    for kind in EVENTS:
        kept = []
        for entry in hooks.get(kind, []):
            handlers = [
                h
                for h in entry.get("hooks", [])
                if h.get("command") not in {command, previous_command}
            ]
            if handlers:
                kept.append({**entry, "hooks": handlers})
        if not remove:
            kept.append({"hooks": [{"type": "command", "command": command, "timeout": 3}]})
        if kept:
            hooks[kind] = kept
        else:
            hooks.pop(kind, None)
    if not hooks:
        value.pop("hooks", None)
    if json.loads(original) == value:
        return "unchanged"
    if path.exists():
        backup = path.with_name(path.name + f".memos-backup-{time.time_ns()}")
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".memos-settings-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        if (path.read_bytes() if path.exists() else b"{}") != original:
            raise ValueError("settings_changed_during_install")
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    return "removed" if remove else "installed"


def install(path, command, previous_command=None):
    return _update(path, command, False, previous_command)


def uninstall(path, command):
    return _update(path, command, True)


def ignore_private_settings(project):
    """Local Git exclusion only; never commit machine paths or configuration backups."""
    result = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--git-path", "info/exclude"],
        text=True,
        capture_output=True,
        check=True,
    )
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = Path(project) / path
    existing = path.read_text() if path.exists() else ""
    additions = [
        p
        for p in ("/.claude/settings.local.json", "/.claude/*.memos-backup-*")
        if p not in existing.splitlines()
    ]
    if additions:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(
                "\n# Private MemOS Claude capture configuration\n" + "\n".join(additions) + "\n"
            )
