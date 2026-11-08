"""Draft one immutable candidate from complete, freshly read source inputs."""

from __future__ import annotations

import json
import re

from datetime import datetime, timezone
from pathlib import Path

from memos.skill_drafting.generation import (
    GenerationError,
    assert_public_json,
    validate_failure_diagnostic,
)


def collect_packet(bridge, plan):
    from memos.skill_drafting.sources import collect_packet as collect

    return collect(bridge, plan)


def recheck_packet(bridge, packet):
    from memos.skill_drafting.sources import recheck_packet as recheck

    return recheck(bridge, packet)


def make_candidate(proposal, packet, *, skill_id, generation):
    from memos.skill_drafting.domain import make_candidate as make

    return make(proposal, packet, skill_id=skill_id, generation=generation)


def write_candidate(destination, packet, proposal, candidate, generation):
    from memos.skill_drafting.artifacts import write_candidate as write

    return write(destination, packet, proposal, candidate, generation)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, value) -> None:
    assert_public_json(value)
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    if len(data) > 4_000_000:
        raise ValueError("draft_artifact_too_large")
    with path.open("xb") as stream:
        stream.write(data)


def draft(bridge, plan: dict, generator, output: Path, *, skill_id: str) -> Path:
    """A new output directory is a new job, never an implicit model retry.

    Full packets remain host artifacts. Only the generator's explicit public
    projection reaches the model. A successful structural result has no release
    authority, and a changed source prevents candidate sealing.
    """
    if not isinstance(skill_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", skill_id) is None:
        raise ValueError("draft_invalid_skill_id")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("draft_output_exists")
    if any(parent.is_symlink() for parent in output.parents):
        # /tmp can be an OS alias; canonicalize the caller's parent once without
        # allowing an already-existing destination to be overwritten.
        output = output.parent.resolve() / output.name
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = {
        "schema_version": 1,
        "status": "failed",
        "started_at": _now(),
        "skill_id": skill_id,
        "candidate_directory": None,
        "qualification": "not_created",
    }
    stage = "collect"
    try:
        packet = collect_packet(bridge, plan)
        _write(output / "packet.json", packet)
        result["packet_id"] = packet["packet_id"]
        stage = "generate"
        proposal, generation = generator.generate(packet)
        # Validate privacy before writing either model-produced object. An
        # invalid but public proposal is retained to diagnose a rejected draft.
        assert_public_json(proposal)
        assert_public_json(generation)
        _write(output / "proposal.json", proposal)
        _write(output / "generation.json", generation)
        stage = "validate"
        candidate = make_candidate(proposal, packet, skill_id=skill_id, generation=generation)
        stage = "recheck"
        recheck_packet(bridge, packet)
        result["source_rechecked_at"] = _now()
        stage = "seal"
        destination = output / "candidate"
        manifest = write_candidate(destination, packet, proposal, candidate, generation)
        if not Path(manifest).is_file():
            raise ValueError("draft_manifest_missing")
        result.update(
            status="completed",
            candidate_directory=str(destination),
            manifest=str(manifest),
            revision=candidate["revision"],
            qualification="unpublished_candidate",
        )
    except Exception as error:
        if isinstance(error, GenerationError):
            result["generation_diagnostic_status"] = "unavailable"
            if error.diagnostic is not None:
                try:
                    result["generation_diagnostic"] = validate_failure_diagnostic(error.diagnostic)
                    result["generation_diagnostic_status"] = "recorded"
                except ValueError:
                    result["generation_diagnostic_status"] = "rejected"
        result.update(
            failure_stage=stage,
            error_code=error.code
            if isinstance(error, GenerationError)
            else f"draft_{stage}_failed",
            error_type=type(error).__name__
            if type(error).__name__
            in {
                "ValueError",
                "TypeError",
                "OSError",
                "RuntimeError",
                "TimeoutError",
                "ImportError",
                "GenerationError",
            }
            else "Error",
        )
    result["finished_at"] = _now()
    path = output / "job.json"
    _write(path, result)
    return path
