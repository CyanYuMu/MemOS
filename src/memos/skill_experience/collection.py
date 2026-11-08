"""Collect actual source attempts only after observing the registered failure.

This module is a trusted host entry point. It does not accept imported Agent
reports as execution proof, and it never changes the original P0/P1a receipts.
"""

from __future__ import annotations

import json
import uuid

from datetime import datetime, timezone
from pathlib import Path

from memos.skill_adapter.runner import RunConfig, run_case
from memos.skill_adapter.verification import execute_guarded
from memos.skill_evaluation.tasks import case_digest, get_case, materialize_case
from memos.skill_evaluation.verifier import check_receipt, verify_candidate


def _write_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def prepare_preflight(case_id: str, destination: Path) -> Path:
    """Check healthy and broken inputs before the Agent gets a writable copy."""
    case = get_case(case_id)
    if case.split != "source":
        raise ValueError("source_required")
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("output_exists")
    destination.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "case_id": case_id,
        "case_revision": case.revision,
        "asset_digest": case_digest(case),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "variants": {},
    }
    preflight_run = "preflight_" + uuid.uuid4().hex
    for variant, expected_status in (("baseline", "passed"), ("initial", "failed")):
        candidate = materialize_case(case_id, destination / f"{variant}-candidate", variant)
        artifact_root = destination / f"{variant}-validation"
        result = verify_candidate(
            case_id,
            candidate,
            artifact_root,
            run_id=preflight_run,
            attempt_id=variant,
            executor=execute_guarded,
        )
        checked = check_receipt(result.receipt_path, candidate, artifact_root)
        receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
        # An import error is not the expected bug. P0's expected_failure_checks
        # names the required failures, not an exhaustive list of every symptom.
        # The receipt retains all failures, including scope generalization.
        failed = {item["name"] for item in receipt["checks"] if item["status"] == "failed"}
        valid = checked.matches and checked.verification_status == expected_status
        if variant == "initial":
            valid = valid and set(case.expected_failure_checks["initial"]) <= failed
        manifest["variants"][variant] = {
            "candidate": str(candidate),
            "artifact_root": str(artifact_root),
            "receipt": str(result.receipt_path),
        }
        if not valid:
            _write_new(
                destination / "preflight-failure.json",
                {
                    **manifest,
                    "status": "failed",
                    "failed_variant": variant,
                },
            )
            raise ValueError("preflight_failed")
    path = destination / "preflight.json"
    _write_new(path, manifest)
    return path


def collect_source(
    case_id: str,
    output: Path,
    config: RunConfig,
    *,
    provider_environment: dict | None = None,
) -> Path:
    """Run one fresh source attempt; retries use a new directory, same lineage.

    Only this path sets the local collector provenance: the caller asks us to
    launch the Agent, rather than supplying a JSON report that claims it ran.
    The host and artifact store remain trusted, as in P0 and P1a.
    """
    case = get_case(case_id)
    if case.split != "source":
        raise ValueError("source_required")
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("output_exists")
    output.mkdir(parents=True)
    result = {
        "schema_version": 1,
        "purpose": "source_collection",
        "case_id": case_id,
        "case_revision": case.revision,
        "lineage_id": case.lineage_id,
        "split": case.split,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "preflight_failed",
        "agent_report": None,
        "evidence": None,
        "qualification": "needs_evidence",
    }
    try:
        preflight = prepare_preflight(case_id, output / "preflight")
        result["preflight"] = str(preflight)
        result["status"] = "collection_failed"
        report = run_case(
            case_id, output / "agent", config, provider_environment=provider_environment
        )
        result["agent_report"] = str(report)

        from memos.skill_experience.evidence import seal_evidence, verify_evidence

        evidence_path = seal_evidence(
            report.parent,
            preflight.parent,
            output / "evidence",
            trusted_collection=True,
        )
        evidence = verify_evidence(evidence_path)
        result["evidence"] = str(evidence_path)
        result["qualification"] = evidence["qualification"]
        result["status"] = "collected"
    except Exception as error:
        # Exception strings may contain provider information. The original run
        # and its safe diagnostics remain available for inspection and retry.
        result["failure_type"] = type(error).__name__
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    path = output / "collection.json"
    _write_new(path, result)
    return path
