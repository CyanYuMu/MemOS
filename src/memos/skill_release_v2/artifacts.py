"""Atomic schema 2 review bundles with full derivation and authority readback."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile

from pathlib import Path

from memos.exceptions import MemOSError
from memos.skill_drafting.artifacts import _display
from memos.skill_drafting.artifacts import _render as render_parent_style
from memos.skill_drafting.domain import hash_json, json_bytes
from memos.skill_release_v2.authority import recheck_contract_context
from memos.skill_release_v2.candidate import make_candidate
from memos.skill_release_v2.capture import _reject_constant, _unique_pairs, _validate_json


_DOCUMENTS = {
    "packet.json": "packet",
    "contexts.json": "contexts",
    "parent.json": "parent",
    "input-manifest.json": "input_manifest",
    "exposure-manifest.json": "exposure_manifest",
    "proposal.json": "proposal",
    "generation.json": "generation",
    "candidate.json": "candidate",
}
_FILES = {*_DOCUMENTS, "validation.json", "REVIEW.md", "manifest.json"}
_MAX_FILE_BYTES = 64 * 1024 * 1024


class ArtifactContractError(MemOSError, ValueError):
    """Safe artifact reason without exposing source content or host locations."""


def _require(condition, code):
    if not condition:
        raise ArtifactContractError("candidate_artifact_" + code)


def _safe_path(path):
    path = Path(path).absolute()
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "unsafe_path")
    return path


def _read(path):
    path = _safe_path(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "invalid_file")
        raw = stream.read(_MAX_FILE_BYTES + 1)
    _require(len(raw) <= _MAX_FILE_BYTES, "file_too_large")
    return raw


def _encoded(value):
    _validate_json(value)
    return json_bytes(value) + b"\n"


def _parse(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
        _validate_json(value)
        return value
    except (ValueError, RecursionError, OverflowError, UnicodeError):
        raise ArtifactContractError("candidate_artifact_invalid_json") from None


def _recheck(contexts, authority_bindings):
    _require(type(contexts) is list and len(contexts) == 1, "contexts")
    for context in contexts:
        recheck_contract_context(context, authority_bindings=authority_bindings)


def _validation(candidate, packet):
    value = {
        "schema_version": 2,
        "kind": "structural_and_references",
        "status": "passed",
        "skill_id": candidate["skill_id"],
        "revision": candidate["revision"],
        "candidate_content_hash": candidate["content_hash"],
        "packet_id": packet["packet_id"],
        "semantic_review": "pending",
        "independent_validation": "not_run",
        "release_eligibility": "not_evaluated",
        "source_count": len(packet["sources"]),
        "independent_lineage_count": len(packet["independent_lineages"]),
        "contract_context_count": len(candidate["contract_context_ids"]),
        "memory_evidence_link_count": sum(
            len(rule["evidence_links"]) for rule in candidate["rules"]
        ),
        "contract_link_count": sum(len(rule["contract_links"]) for rule in candidate["rules"]),
        "checks": [
            "packet_integrity",
            "parent_integrity",
            "source_identity",
            "proposal_structure",
            "rule_flow",
            "reference_binding",
            "qualification_limits",
            "contract_binding",
            "input_reconstruction",
            "exposure_binding",
            "generation_binding",
        ],
    }
    value["validation_id"] = hash_json(value)
    return value


def _review(candidate, generation):
    base = render_parent_style(candidate).decode()
    lines = [
        "",
        "## Project contract references",
        "",
        "These references are normative authority, not Memory or successful experience.",
        "",
    ]
    for rule in candidate["rules"]:
        for reference in rule["contract_links"]:
            lines.append(
                f"- `{rule['rule_id']}` / `{reference['context_id']}` / `{reference['locator']}` / `{reference['value_sha256']}`: {_display(reference['claim'])}"
            )
    lines += [
        "",
        "## Generation identity",
        "",
        "Requested model: " + _display(generation["requested_model"]),
        "Reported model: " + _display(generation["reported_model"]),
        "Exact identity match: " + str(generation["model_identity_match"]).lower(),
        "A mismatch remains a review limitation; no alias equivalence is inferred.",
        "Content consistency does not authenticate a provider invocation. Provider cost is unknown.",
        "",
    ]
    return (base + "\n".join(lines)).encode()


def _contents(documents):
    packet, candidate = documents["packet"], documents["candidate"]
    expected = make_candidate(
        documents["proposal"],
        packet,
        skill_id=candidate["skill_id"],
        **{
            key: documents[key]
            for key in ("contexts", "parent", "input_manifest", "exposure_manifest", "generation")
        },
    )
    _require(json_bytes(candidate) == json_bytes(expected), "candidate_mismatch")
    validation = _validation(candidate, packet)
    content = {filename: _encoded(documents[key]) for filename, key in _DOCUMENTS.items()}
    content["validation.json"] = _encoded(validation)
    content["REVIEW.md"] = _review(candidate, documents["generation"])
    manifest = {
        "schema_version": 2,
        "kind": "candidate_review_artifacts_v2",
        "project_id": candidate["project_id"],
        "skill_id": candidate["skill_id"],
        "revision": candidate["revision"],
        "packet_id": packet["packet_id"],
        "candidate_content_hash": candidate["content_hash"],
        "context_ids": candidate["contract_context_ids"],
        "input_manifest_id": candidate["input_manifest_id"],
        "exposure_manifest_id": candidate["exposure_manifest_id"],
        "parent": candidate["parent"],
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in content.items()},
    }
    manifest["manifest_id"] = hash_json(manifest)
    content["manifest.json"] = _encoded(manifest)
    _require(all(len(raw) <= _MAX_FILE_BYTES for raw in content.values()), "file_too_large")
    return content, validation, manifest


def write_candidate(
    destination: Path,
    packet: dict,
    proposal: dict,
    candidate: dict,
    generation: dict,
    *,
    contexts: list,
    parent: dict,
    input_manifest: dict,
    exposure_manifest: dict,
    authority_bindings: dict,
    source_recheck,
) -> Path:
    """Publish once; a trusted callback must reread actual Memory before and after.

    The callback is an internal host capability, never a serialized trust flag.
    Cooperative writers share an exclusive lock in the trusted destination parent.
    """
    _require(callable(source_recheck), "source_recheck_missing")
    destination = _safe_path(destination)
    _require(not destination.exists(), "destination_exists")
    documents = copy.deepcopy(
        {
            "packet": packet,
            "proposal": proposal,
            "candidate": candidate,
            "generation": generation,
            "contexts": contexts,
            "parent": parent,
            "input_manifest": input_manifest,
            "exposure_manifest": exposure_manifest,
        }
    )
    source_recheck(copy.deepcopy(documents["packet"]))
    _recheck(documents["contexts"], authority_bindings)
    content, _, _ = _contents(documents)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_path(destination.parent)
    lock = destination.parent / f".{destination.name}.publish-lock"
    temporary = None
    claimed = False
    try:
        try:
            lock.mkdir(mode=0o700)
            claimed = True
        except FileExistsError:
            raise ArtifactContractError("candidate_artifact_writer_busy") from None
        _require(not destination.exists() and not destination.is_symlink(), "destination_exists")
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        for name, raw in content.items():
            with (temporary / name).open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        load_candidate(temporary, authority_bindings=authority_bindings)
        source_recheck(copy.deepcopy(documents["packet"]))
        _recheck(documents["contexts"], authority_bindings)
        _require(not destination.exists() and not destination.is_symlink(), "destination_exists")
        os.rename(temporary, destination)
        temporary = None
        return destination / "manifest.json"
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
        if claimed:
            lock.rmdir()


def load_candidate(directory: Path, *, authority_bindings: dict) -> dict:
    """Rebuild every byte and recheck authority; live Memory remains host-owned."""
    try:
        directory = _safe_path(directory)
        _require(
            directory.is_dir() and {path.name for path in directory.iterdir()} == _FILES,
            "unexpected_files",
        )
        raw = {name: _read(directory / name) for name in _FILES}
        manifest = _parse(raw["manifest.json"])
        _require(
            type(manifest) is dict
            and type(manifest.get("files")) is dict
            and set(manifest["files"]) == _FILES - {"manifest.json"},
            "manifest_files",
        )
        for name, digest in manifest["files"].items():
            _require(hashlib.sha256(raw[name]).hexdigest() == digest, "digest_mismatch")
        documents = {key: _parse(raw[name]) for name, key in _DOCUMENTS.items()}
        content, validation, expected_manifest = _contents(documents)
        _require(raw == content, "canonical_content_changed")
        _recheck(documents["contexts"], authority_bindings)
        return {**documents, "validation": validation, "manifest": expected_manifest}
    except (
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RecursionError,
        OverflowError,
        UnicodeError,
    ):
        raise ArtifactContractError("candidate_artifact_invalid") from None
