"""Immutable, fixed-revision native Claude Skill exports for controlled runs."""

from __future__ import annotations

import hashlib
import json
import re

from dataclasses import asdict, dataclass
from pathlib import Path


EXPORTER_VERSION = "0.1"


@dataclass(frozen=True)
class SkillRevision:
    skill_id: str
    revision: str
    body: str
    rule_ids: tuple[str, ...]
    requires: tuple = ()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate(spec: SkillRevision) -> None:
    if not isinstance(spec, SkillRevision):
        raise ValueError("artifact_mismatch: expected a fixed SkillRevision")
    for name, value, pattern in (
        ("skill_id", spec.skill_id, r"[a-z0-9][a-z0-9-]{0,79}"),
        ("revision", spec.revision, r"[a-z0-9][a-z0-9._-]{0,63}"),
    ):
        if not isinstance(value, str) or not re.fullmatch(pattern, value) or value == "latest":
            raise ValueError(f"artifact_mismatch: invalid fixed {name}")
    if (
        not isinstance(spec.rule_ids, tuple)
        or not spec.rule_ids
        or any(
            not isinstance(rule, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rule)
            for rule in spec.rule_ids
        )
        or len(set(spec.rule_ids)) != len(spec.rule_ids)
    ):
        raise ValueError("artifact_mismatch: invalid rule identities")
    if spec.requires:
        raise ValueError("capability_missing: dependency closure export is not supported in P1a")
    if not isinstance(spec.requires, tuple):
        raise ValueError("artifact_mismatch: requires must be an immutable tuple")
    if (
        not isinstance(spec.body, str)
        or not spec.body.strip()
        or len(spec.body.encode()) > 100_000
        or "\x00" in spec.body
        or spec.body.lstrip().startswith("---")
        or "!`" in spec.body
        or "${" in spec.body
        or "$ARGUMENTS" in spec.body
        or re.search(r"\$[0-9]+", spec.body)
    ):
        raise ValueError("artifact_mismatch: unsupported dynamic or invalid Skill body")


def runtime_alias(spec: SkillRevision) -> str:
    """Derive a native name from complete identity and content, never latest."""
    _validate(spec)
    canonical = _digest(_json_bytes(asdict(spec)))
    version = re.sub(r"[^a-z0-9-]", "-", spec.revision)[:10]
    return f"mem-{spec.skill_id[:24]}-r{version}-{canonical[:12]}"


def _render(spec: SkillRevision) -> tuple[bytes, dict]:
    alias = runtime_alias(spec)
    description = json.dumps(
        f"Explicit fixed revision {spec.skill_id}/{spec.revision} for a controlled task.",
        ensure_ascii=False,
    )
    body = (
        f"---\nname: {alias}\ndescription: {description}\n"
        f"disable-model-invocation: true\n---\n\n{spec.body}"
    ).encode()
    manifest = {
        "schema_version": 1,
        "exporter_version": EXPORTER_VERSION,
        "skill_id": spec.skill_id,
        "revision": spec.revision,
        "canonical_digest": _digest(_json_bytes(asdict(spec))),
        "runtime_alias": alias,
        "rule_ids": list(spec.rule_ids),
        "requires": [],
        "files": {"SKILL.md": _digest(body)},
    }
    manifest["export_hash"] = _digest(_json_bytes(manifest))
    return body, manifest


def _check_path(destination: Path) -> None:
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError("artifact_mismatch: Skill path contains a symbolic link")


def export_skill(spec: SkillRevision, destination: Path) -> dict:
    """Create one native skill directory; callers choose its isolated parent directory."""
    body, manifest = _render(spec)
    destination = Path(destination).absolute()
    _check_path(destination)
    if destination.exists():
        raise ValueError("alias_conflict: Skill destination already exists")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "SKILL.md").write_bytes(body)
    (destination / "manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def verify_export(spec: SkillRevision, destination: Path) -> dict:
    """Recompute the exact exported bytes and reject any added or changed material."""
    body, manifest = _render(spec)
    destination = Path(destination).absolute()
    _check_path(destination)
    try:
        entries = {path.name: path for path in destination.iterdir()}
        if set(entries) != {"SKILL.md", "manifest.json"} or any(
            path.is_symlink() or not path.is_file() for path in entries.values()
        ):
            raise ValueError("artifact_mismatch: unexpected Skill files")
        if (destination / "SKILL.md").read_bytes() != body or (
            destination / "manifest.json"
        ).read_bytes() != _json_bytes(manifest):
            raise ValueError("artifact_mismatch: Skill content or manifest changed")
    except OSError as error:
        raise ValueError("artifact_mismatch: Skill export is not readable") from error
    return manifest
