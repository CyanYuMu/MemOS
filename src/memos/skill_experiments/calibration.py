"""Append-only recalculation of existing dev evidence under an explicit scorer.

A reassessment is neither a new Agent run nor a replacement for the original
summary. Its verifier repeats receipt and scoring checks against the current
fixed inputs; rehashing a handwritten success record cannot grant eligibility.
"""

from __future__ import annotations

import json
import os

from pathlib import Path
from typing import TYPE_CHECKING

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_evaluation.tasks import case_digest, get_case
from memos.skill_experiments.manifest import (
    GROUPS,
    raw_context,
    read_json,
    runtime_binding,
    sha,
    verify_calibration,
)
from memos.skill_experiments.scoring import score_run
from memos.skill_management.runtime import export_identity


if TYPE_CHECKING:
    from memos.skill_adapter.runner import RunConfig


PROTOCOL = "calibration-reassessment-1"


def _assessment(calibration_directory: Path, candidate_directory: Path, config: RunConfig) -> dict:
    directory = Path(calibration_directory).resolve()
    summary_path = directory / "summary.json"
    summary = read_json(summary_path)
    results = summary.get("results", [])
    if (
        summary.get("purpose") != "dev_calibration"
        or not isinstance(results, list)
        or len(results) != 3
        or any(not isinstance(row, dict) for row in results)
        or {row.get("group") for row in results} != set(GROUPS)
    ):
        raise ValueError("calibration_reassessment_invalid_original")
    original_hash = sha(summary_path)
    loaded = load_candidate(candidate_directory)
    revision = loaded["candidate"]["revision"]
    projection = export_identity(loaded["candidate"])
    _, selection = raw_context(loaded["packet"], 32768)
    paths = [directory / group / "report.json" for group in GROUPS]
    hashes = {group: sha(path) for group, path in zip(GROUPS, paths, strict=True)}
    verified = verify_calibration(
        paths,
        config,
        revision,
        export_hash=projection["exportHash"],
        raw_hash=selection["sha256"],
    )
    scores = []
    for group, path in zip(GROUPS, paths, strict=True):
        report = read_json(path)
        score = score_run(
            path,
            group=group,
            expected_case_digest=case_digest(get_case(report["run_context"]["case_id"])),
        )
        if score["status"] != "passed":
            raise ValueError("calibration_reassessment_not_passed")
        previous = next(row for row in results if row["group"] == group)
        for key in ("runId", "attemptId", "caseId"):
            if key in previous and previous[key] != score[key]:
                raise ValueError("calibration_reassessment_original_run_mismatch")
        scores.append(score)
    if original_hash != sha(summary_path) or hashes != {
        group: sha(path) for group, path in zip(GROUPS, paths, strict=True)
    }:
        raise ValueError("calibration_reassessment_inputs_changed")
    binding = runtime_binding(config)
    value = {
        "schemaVersion": 1,
        "protocol": PROTOCOL,
        "purpose": "dev_calibration_reassessment",
        "candidateRevision": revision,
        "candidateManifest": loaded["manifest"]["manifest_id"],
        "exportHash": projection["exportHash"],
        "rawMemoryHash": selection["sha256"],
        "originalSummarySha256": original_hash,
        "reportHashes": hashes,
        "reassessmentDigest": sha(Path(__file__)),
        "scoringVersion": binding["scoringVersion"],
        "scoringDigest": binding["scoringDigest"],
        "runtime": binding,
        "verifiedCalibration": verified,
        "scores": scores,
        "originalRecordsReplaced": False,
        "newModelCalls": 0,
        "publication": "not_approved",
    }
    value["recordId"] = hash_json(value)
    return value


def create_reassessment(
    calibration_directory: Path,
    candidate_directory: Path,
    config: RunConfig,
    output: Path,
) -> dict:
    """Verify original evidence and exclusively create a separate private record."""
    output = Path(output)
    if os.path.lexists(output):
        raise FileExistsError("calibration_reassessment_output_exists")
    value = _assessment(calibration_directory, candidate_directory, config)
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return value


def verify_reassessment(
    record_path: Path,
    calibration_directory: Path,
    candidate_directory: Path,
    config: RunConfig,
) -> dict:
    """Recompute all eligibility; never accept the saved passed literals alone."""
    saved = read_json(Path(record_path))
    if saved.get("recordId") != hash_json({k: v for k, v in saved.items() if k != "recordId"}):
        raise ValueError("calibration_reassessment_record_changed")
    current = _assessment(calibration_directory, candidate_directory, config)
    if saved != current:
        raise ValueError("calibration_reassessment_binding_changed")
    return saved
