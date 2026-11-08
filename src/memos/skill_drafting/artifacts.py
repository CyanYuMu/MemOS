"""Immutable review artifacts; no native Skill export or release is created here."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile

from pathlib import Path

from memos.skill_drafting.domain import hash_json, json_bytes, make_candidate


_FILES = {
    "packet.json",
    "proposal.json",
    "candidate.json",
    "generation.json",
    "validation.json",
    "REVIEW.md",
    "manifest.json",
}


def _encoded(value: object) -> bytes:
    return json_bytes(value) + b"\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require(condition: object, code: str) -> None:
    if not condition:
        raise ValueError("draft_artifact_" + code)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("draft_artifact_duplicate_json_key")
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError("draft_artifact_nonfinite_json")


def _read(path: Path) -> bytes:
    _require(path.is_file() and not path.is_symlink(), "invalid_file")
    _require(path.stat().st_size <= 32 * 1024 * 1024, "file_too_large")
    return path.read_bytes()


def _json(path: Path) -> dict:
    value = json.loads(_read(path), object_pairs_hook=_unique, parse_constant=_invalid_constant)
    _require(isinstance(value, dict), "invalid_json_document")
    return value


def _display(value: str) -> str:
    # Review text is untrusted model output, not executable Markdown extensions.
    text = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for symbol in ("\\", "`", "[", "]", "!", "*", "_", "#"):
        text = text.replace(symbol, "\\" + symbol)
    return text.replace("\n", " ")


def _render(candidate: dict) -> bytes:
    lines = [
        f"# Candidate Skill: {_display(candidate['title'])}",
        "",
        "**Unpublished candidate — not authorized for ordinary Agent use.**",
        "Structural/reference checks passed; semantic review is pending and independent task validation has not run.",
        "",
        f"Skill: `{candidate['skill_id']}`  ",
        f"Revision: `{candidate['revision']}`",
        "",
        "## Goal",
        "",
        _display(candidate["goal"]),
    ]
    for key, title in (
        ("applicability", "Applicability"),
        ("exclusions", "Excluded scenarios"),
        ("prerequisites", "Prerequisites"),
        ("constraints", "Constraints"),
    ):
        lines += ["", f"## {title}", ""]
        lines += [f"- {_display(item)}" for item in candidate[key]]
    lines += ["", "## Rules", "", f"Entry: `{candidate['entry_rule']}`"]
    for rule in candidate["rules"]:
        lines += ["", f"### {rule['rule_id']}", ""]
        for key in ("condition", "observation", "action", "expected_result"):
            lines += [f"**{key}:** {_display(rule[key])}", ""]
        lines += [f"Basis: `{rule['basis']}`; rule validation: `{rule['validation_status']}`.", ""]
        lines += [
            f"- {_display(branch['when'])} → `{branch['target']}`" for branch in rule["branches"]
        ]
        lines += ["", "Evidence references (semantic review pending):", ""]
        for link in rule["evidence_links"]:
            lines += [
                f"- `{link['link_id']}` — `{link['kind']}`, `{link['target_kind']}` "
                f"`{link['target_id']}` at `{link['locator']}`: {_display(link['claim'])}"
            ]
    lines += ["", "## Exits", ""]
    lines += [
        f"- `{key}`: {_display(candidate['exits'][key])}" for key in sorted(candidate["exits"])
    ]
    for key, title in (
        ("acceptance", "Acceptance"),
        ("limitations", "Limitations"),
        ("host_limits", "Host-enforced limits"),
    ):
        lines += ["", f"## {title}", ""]
        lines += [f"- {_display(item)}" for item in candidate[key]]
    return ("\n".join(lines) + "\n").encode()


def _validation(candidate: dict, packet: dict) -> dict:
    value = {
        "schema_version": 1,
        "kind": "structural_and_references",
        "status": "passed",
        "skill_id": candidate["skill_id"],
        "revision": candidate["revision"],
        "candidate_content_hash": candidate["content_hash"],
        "packet_id": packet["packet_id"],
        "semantic_review": "pending",
        "independent_validation": "not_run",
        "release_eligibility": "not_evaluated",
        "rule_count": len(candidate["rules"]),
        "source_count": len(packet["sources"]),
        "independent_lineage_count": len(packet["independent_lineages"]),
        "checks": [
            "packet_integrity",
            "source_identity",
            "proposal_structure",
            "rule_flow",
            "reference_binding",
            "qualification_limits",
        ],
    }
    value["validation_id"] = hash_json(value)
    return value


def _contents(packet, proposal, candidate, generation):
    expected = make_candidate(
        proposal, packet, skill_id=candidate["skill_id"], generation=generation
    )
    _require(candidate == expected, "candidate_mismatch")
    validation = _validation(candidate, packet)
    content = {
        "packet.json": _encoded(packet),
        "proposal.json": _encoded(proposal),
        "candidate.json": _encoded(candidate),
        "generation.json": _encoded(generation),
        "validation.json": _encoded(validation),
        "REVIEW.md": _render(candidate),
    }
    manifest = {
        "schema_version": 1,
        "kind": "candidate_review_artifacts",
        "project_id": candidate["project_id"],
        "skill_id": candidate["skill_id"],
        "revision": candidate["revision"],
        "packet_id": packet["packet_id"],
        "candidate_content_hash": candidate["content_hash"],
        "files": {key: _sha(value) for key, value in content.items()},
    }
    manifest["manifest_id"] = hash_json(manifest)
    content["manifest.json"] = _encoded(manifest)
    return content, validation, manifest


def write_candidate(
    destination: Path, packet: dict, proposal: dict, candidate: dict, generation: dict
) -> Path:
    """Publish a complete directory once, with a same-parent exclusive writer lock.

    The host and artifact filesystem are trusted. A crashed writer can leave its
    lock/staging directory for inspection; it cannot make a partial candidate load.
    """
    destination = Path(destination).absolute()
    _require(not destination.is_symlink() and not destination.exists(), "destination_exists")
    destination = destination.resolve()
    content, _, _ = _contents(packet, proposal, candidate, generation)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = destination.parent / f".{destination.name}.publish-lock"
    temporary = None
    claimed = False
    try:
        try:
            lock.mkdir(mode=0o700)
            claimed = True
        except FileExistsError:
            raise ValueError("draft_artifact_writer_busy") from None
        _require(not destination.exists() and not destination.is_symlink(), "destination_exists")
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        for name, data in content.items():
            with (temporary / name).open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        load_candidate(temporary)
        _require(not destination.exists() and not destination.is_symlink(), "destination_exists")
        os.rename(temporary, destination)
        temporary = None
        return destination / "manifest.json"
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
        if claimed:
            lock.rmdir()


def load_candidate(directory: Path) -> dict:
    """Recompute every derivation, including review text; a new manifest is not approval."""
    try:
        directory = Path(directory).absolute()
        _require(not directory.is_symlink() and directory.is_dir(), "invalid_directory")
        _require({path.name for path in directory.iterdir()} == _FILES, "unexpected_files")
        manifest = _json(directory / "manifest.json")
        _require(set(manifest["files"]) == _FILES - {"manifest.json"}, "manifest_files")
        for name, digest in manifest["files"].items():
            _require(_sha(_read(directory / name)) == digest, "digest_mismatch")
        packet, proposal, candidate, generation = (
            _json(directory / name)
            for name in ("packet.json", "proposal.json", "candidate.json", "generation.json")
        )
        content, validation, expected_manifest = _contents(packet, proposal, candidate, generation)
        _require(manifest == expected_manifest, "manifest_mismatch")
        for name, expected in content.items():
            _require(_read(directory / name) == expected, "canonical_content_changed")
        return {
            "packet": packet,
            "proposal": proposal,
            "candidate": candidate,
            "generation": generation,
            "validation": validation,
            "manifest": manifest,
        }
    except (OSError, KeyError, TypeError, AttributeError, IndexError, RecursionError):
        raise ValueError("draft_artifact_invalid") from None
