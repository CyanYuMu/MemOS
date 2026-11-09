"""Freeze seven-unit release or two-unit recovery plans without coverage retries."""

from __future__ import annotations

import hashlib
import uuid

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_experiments.__main__ import runtime
from memos.skill_management.config import read_config
from memos.skill_management.domain import branches, require
from memos.skill_management.runtime import export_identity, native_skill
from memos.skill_release.execution_policy import policy_identity, report_eligibility
from memos.skill_release.qualification import require_qualification
from memos.skill_release.runner import adapter_digest, run_extension_task
from memos.skill_release.tasks import SUITES, get_suite, suite_digest
from memos.skill_release.verifier import _no_symlinks
from memos.skill_verification.verifier import _read, _write


UNITS = (
    ("release-none", "update-repair-r1", "none"),
    *((suite + "-skill", suite, "skill") for suite in SUITES),
)


def plan_units(purpose):
    if purpose == "release":
        return UNITS
    if purpose == "recovery":
        return UNITS[:2]
    raise ValueError("release_unknown_plan")


def validate_intents(candidate, intents):
    require(
        isinstance(intents, dict) and set(intents) == set(SUITES), "release_branch_plan_invalid"
    )
    claimed = set()
    for items in intents.values():
        require(
            isinstance(items, list) and items and all(isinstance(x, str) for x in items),
            "release_branch_plan_invalid",
        )
        require(
            len(items) == len(set(items)) and set(items) <= branches(candidate),
            "release_branch_plan_invalid",
        )
        claimed.update(items)
    require(claimed == branches(candidate), "release_branch_plan_incomplete")
    return intents


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _identity(
    config_path,
    candidate_directory,
    qualification_directory,
    *,
    branch_intents,
    plan_purpose="release",
    batch_nonce=None,
):
    service = read_config(config_path)
    config = runtime(service)
    candidate = load_candidate(candidate_directory)["candidate"]
    validate_intents(candidate, branch_intents)
    units = plan_units(plan_purpose)
    identity = {
        "protocol": "release-batch-1",
        "planPurpose": plan_purpose,
        "batchNonce": batch_nonce or uuid.uuid4().hex,
        "batchDigest": _sha(__file__),
        "executionPolicy": policy_identity(),
        "adapterDigest": adapter_digest(),
        "configurationDigest": _sha(config_path),
        "qualification": {
            name: require_qualification(Path(qualification_directory) / name, suite_id=name)
            for name in SUITES
        },
        "branchIntents": branch_intents,
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
            {
                "id": name,
                "suiteId": suite_id,
                "taskId": get_suite(suite_id).task_id,
                "group": group,
                "taskDigest": suite_digest(get_suite(suite_id)),
            }
            for name, suite_id, group in units
        ],
        "maxAgentRuns": len(units),
        "newIndependentSamples": 0,
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
    branch_intents: dict,
    plan_purpose: str = "release",
    progress=None,
    cancellation_event=None,
    execution_guard=None,
) -> dict:
    """Never resume/replace a batch or treat calibration as an effectiveness arm."""
    _no_symlinks(Path(output))
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("extension_batch_already_exists")
    identity, service, config, skill = _identity(
        config_path,
        candidate_directory,
        qualification_directory,
        branch_intents=branch_intents,
        plan_purpose=plan_purpose,
    )
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    experiment_id = hash_json(identity)
    _write(output / "manifest.json", {**identity, "experimentId": experiment_id})
    results, blocked = [], None
    interrupted = False
    try:
        for unit in identity["units"]:
            if cancellation_event is not None and cancellation_event.is_set():
                blocked = "batch_cancelled"
                break
            if progress:
                progress({"stage": "started", "unit": unit["id"], "group": unit["group"]})
            report_path = run_extension_task(
                unit["suiteId"],
                output / "runs" / unit["id"],
                config,
                qualification_directory=Path(qualification_directory) / unit["suiteId"],
                group=unit["group"],
                experiment_id=experiment_id,
                expected_suite_digest=unit["taskDigest"],
                skill=skill if unit["group"] == "skill" else None,
                expected_export_hash=identity["exportHash"] if unit["group"] == "skill" else None,
                provider_environment=service["runtime"]["providerEnvironment"],
                cancellation_event=cancellation_event,
                execution_guard=execution_guard,
                execution_metadata={
                    "batchId": experiment_id,
                    "unitId": unit["id"],
                    "caseId": unit["taskId"],
                    "group": unit["group"],
                    "mode": "independent_validation",
                    "contentHash": identity["contentHash"],
                    "exportHash": identity["exportHash"],
                }
                if execution_guard is not None
                else None,
            )
            report = _read(report_path)
            eligibility = report_eligibility(report, unit["group"])
            result = {
                "planQualified": eligibility["qualified"],
                "planIssues": eligibility["issues"],
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
            if unit["group"] == "none" and not eligibility["qualified"]:
                blocked = "calibration_needs_review"
                break
            if (
                _identity(
                    config_path,
                    candidate_directory,
                    qualification_directory,
                    branch_intents=branch_intents,
                    plan_purpose=plan_purpose,
                    batch_nonce=identity["batchNonce"],
                )[0]
                != identity
            ):
                blocked = "frozen_configuration_changed"
                break
    except KeyboardInterrupt:
        blocked, interrupted = "batch_cancelled", True
    except Exception as error:
        # Exception messages may contain provider/private deployment details.
        blocked = "batch_failed:" + type(error).__name__
    issues = [
        ("calibration:" if row["unit"] == "release-none" else "trial:") + issue
        for row in results
        for issue in row["planIssues"]
    ]
    if len(results) != len(identity["units"]):
        issues.append("batch:execution_incomplete")
    if blocked is not None:
        issues.append("batch:" + blocked)
    summary = {
        "planQualified": not issues,
        "planIssues": issues,
        "protocol": identity["protocol"],
        "experimentId": experiment_id,
        "complete": blocked is None and len(results) == len(identity["units"]),
        "success": blocked is None
        and len(results) == len(identity["units"])
        and all(row["status"] == "passed" for row in results)
        and not issues,
        "blockedReason": blocked,
        "results": results,
        "branchCoverage": [],
        "humanApprovals": 0,
        "newIndependentSamples": 0,
        "effectivenessEvaluation": False,
    }
    _write(output / "summary.json", summary)
    if interrupted:
        raise KeyboardInterrupt()
    return summary
