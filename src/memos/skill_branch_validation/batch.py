"""Freeze and run one bounded protocol calibration plus two core branch trials."""

from __future__ import annotations

import hashlib

from pathlib import Path

from memos.skill_branch_validation.qualification import require_qualification
from memos.skill_branch_validation.runner import adapter_digest, run_branch_task
from memos.skill_branch_validation.tasks import CANDIDATE_REVISION, get_task, task_digest
from memos.skill_branch_validation.verifier import _no_symlinks
from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_experiments.__main__ import runtime
from memos.skill_management.config import read_config
from memos.skill_management.runtime import export_identity, native_skill
from memos.skill_verification.verifier import _read, _write


UNITS = (
    ("scoped-none", "branch-scoped-table", "none"),
    ("scoped-skill", "branch-scoped-table", "skill"),
    ("dream-skill", "branch-dream-journal", "skill"),
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _identity(config_path, candidate_directory, qualification_directory):
    service = read_config(config_path)
    config = runtime(service)
    candidate = load_candidate(candidate_directory)["candidate"]
    if candidate["revision"] != CANDIDATE_REVISION:
        raise ValueError("branch_candidate_revision_changed")
    identity = {
        "protocol": "branch-core-batch-1",
        "batchDigest": _sha(__file__),
        "adapterDigest": adapter_digest(),
        "configurationDigest": _sha(config_path),
        "qualification": require_qualification(qualification_directory),
        "candidateRevision": candidate["revision"],
        "contentHash": candidate["content_hash"],
        "exportHash": export_identity(candidate)["exportHash"],
        "runtime": {
            "cliHash": _sha(config.cli_path.resolve()),
            "cliVersion": config.expected_version,
            "model": config.model,
            "effort": config.effort,
            "tools": list(config.tools),
            "timeoutSeconds": config.timeout_seconds,
            "maxTurns": config.max_turns,
            "maxBudgetUsd": config.max_budget_usd,
        },
        "units": [
            {"id": name, "taskId": task, "group": group, "taskDigest": task_digest(get_task(task))}
            for name, task, group in UNITS
        ],
        "maxAgentRuns": 3,
        "branchCoverage": [],
        "effectivenessEvaluation": False,
    }
    return identity, service, config, native_skill(candidate)


def run_batch(
    config_path: Path,
    candidate_directory: Path,
    qualification_directory: Path,
    output: Path,
    *,
    progress=None,
) -> dict:
    """Never resume/replace a batch or treat calibration as an effectiveness arm."""
    _no_symlinks(Path(output))
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("branch_batch_already_exists")
    identity, service, config, skill = _identity(
        config_path, candidate_directory, qualification_directory
    )
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    experiment_id = hash_json(identity)
    _write(output / "manifest.json", {**identity, "experimentId": experiment_id})
    results, blocked = [], None
    interrupted = False
    try:
        for unit in identity["units"]:
            if progress:
                progress({"stage": "started", "unit": unit["id"], "group": unit["group"]})
            report_path = run_branch_task(
                unit["taskId"],
                output / "runs" / unit["id"],
                config,
                qualification_directory=qualification_directory,
                group=unit["group"],
                experiment_id=experiment_id,
                expected_task_digest=unit["taskDigest"],
                skill=skill if unit["group"] == "skill" else None,
                expected_export_hash=identity["exportHash"] if unit["group"] == "skill" else None,
                provider_environment=service["runtime"]["providerEnvironment"],
            )
            report = _read(report_path)
            result = {
                "unit": unit["id"],
                "status": report["status"],
                "taskAccepted": report.get("task_accepted", False),
                "capture": report.get("capture", {}).get("status"),
                "loading": report.get("loading", {}).get("status"),
                "report": report_path.relative_to(output).as_posix(),
                "reportSha256": _sha(report_path),
            }
            results.append(result)
            if progress:
                progress({"stage": "finished", **result})
            if unit["group"] == "none" and report["status"] != "passed":
                blocked = "calibration_needs_review"
                break
            if _identity(config_path, candidate_directory, qualification_directory)[0] != identity:
                blocked = "frozen_configuration_changed"
                break
    except KeyboardInterrupt:
        blocked, interrupted = "batch_cancelled", True
    except Exception as error:
        # Exception messages may contain provider/private deployment details.
        blocked = "batch_failed:" + type(error).__name__
    summary = {
        "protocol": identity["protocol"],
        "experimentId": experiment_id,
        "complete": blocked is None and len(results) == len(UNITS),
        "success": blocked is None
        and len(results) == len(UNITS)
        and all(row["status"] == "passed" for row in results),
        "blockedReason": blocked,
        "results": results,
        "branchCoverage": [],
        "humanApprovals": 0,
        "effectivenessEvaluation": False,
    }
    _write(output / "summary.json", summary)
    if interrupted:
        raise KeyboardInterrupt()
    return summary
