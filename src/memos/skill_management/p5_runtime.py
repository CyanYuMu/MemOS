"""Management orchestration v2 using the unchanged fixed P5 executor.

Separate CLI binary hashing from the evidence JSON size limit. The frozen
service_runtime producer and its unsuccessful preflight artifacts remain intact.
"""

from pathlib import Path

from memos.skill_drafting.artifacts import load_candidate
from memos.skill_drafting.domain import hash_json
from memos.skill_governance.collector import _read, _sha
from memos.skill_management.runtime import export_identity, native_skill
from memos.skill_p5.collector import _read_run
from memos.skill_p5.execution_policy import policy_identity
from memos.skill_p5.qualification import audit_protocol, require_qualification
from memos.skill_p5.runner import adapter_digest, run_extension_task
from memos.skill_p5.service_runtime import dev_case
from memos.skill_p5.tasks import get_suite, suite_digest
from memos.skill_verification.verifier import _write


def _binary_sha(path):
    """CLI binaries are larger than bounded JSON evidence; hash them as a stream."""
    import hashlib

    path = Path(path)
    if not path.is_file() or path.stat().st_size > 1024 * 1024 * 1024:
        raise ValueError("managed_invalid_cli_binary")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def execute_task(
    candidate_directory, case_id, output, config, provider, *, mode, authorize, execution_guard=None
):
    """Normal runs retain native facts but never contribute independent coverage."""
    if mode not in {"normal", "candidate_evaluation"}:
        raise ValueError("managed_invalid_mode")
    case = dev_case(case_id)
    suite = get_suite(case_id.removeprefix("p5-"))
    candidate = load_candidate(candidate_directory)["candidate"]
    projection = export_identity(candidate)
    before = authorize()
    if before.get("allowed") is not True:
        raise ValueError("managed_admission_denied")
    if (
        type(before.get("sequence")) is not int
        or before["sequence"] < 0
        or (mode == "normal" and not before.get("releaseId"))
        or any(before.get(k) != projection[k] for k in ("contentHash", "exportHash"))
    ):
        raise ValueError("managed_admission_mismatch")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = {"status": "unknown", "errorCode": None, "validation": None, "projection": projection}
    try:
        audit_protocol(suite.suite_id, output / "qualification")
        proof = require_qualification(output / "qualification", suite_id=suite.suite_id)
        unit = {
            "id": suite.suite_id + "-skill",
            "suiteId": suite.suite_id,
            "taskId": suite.task_id,
            "taskDigest": suite_digest(suite),
            "group": "skill",
            "split": "dev",
        }
        identity = {
            "protocol": "p5-normal-2",
            "runtimeProducer": _sha(Path(__file__)),
            "executionMode": mode,
            "candidateRevision": candidate["revision"],
            "contentHash": candidate["content_hash"],
            "exportHash": projection["exportHash"],
            "adapterDigest": adapter_digest(),
            "executionPolicy": policy_identity(),
            "qualification": {suite.suite_id: proof},
            "units": [unit],
            "runtime": {
                "cliHash": _binary_sha(config.cli_path.resolve()),
                "cliVersion": config.expected_version,
                "model": config.model,
                "effort": config.effort,
                "tools": list(config.tools),
                "timeoutSeconds": config.timeout_seconds,
                "maxTurns": config.max_turns,
                "maxBudgetUsd": config.max_budget_usd,
            },
            "admission": before,
            "maxAgentRuns": 1,
            "independentCoverage": False,
        }
        manifest = {**identity, "experimentId": hash_json(identity)}
        _write(output / "manifest.json", manifest)
        root = output / "runs" / unit["id"]
        run_extension_task(
            suite.suite_id,
            root,
            config,
            qualification_directory=output / "qualification",
            group="skill",
            experiment_id=manifest["experimentId"],
            expected_suite_digest=suite_digest(suite),
            skill=native_skill(candidate),
            expected_export_hash=projection["exportHash"],
            provider_environment=provider,
            task_split="dev",
            execution_mode=mode,
            execution_guard=execution_guard,
            execution_metadata={
                "batchId": manifest["experimentId"],
                "unitId": unit["id"],
                "caseId": suite.task_id,
                "group": "skill",
                "mode": mode,
                "contentHash": candidate["content_hash"],
                "exportHash": projection["exportHash"],
            }
            if execution_guard is not None
            else None,
        )
        trial = _read_run(
            candidate,
            projection["exportHash"],
            root,
            output,
            manifest,
            output,
            tuple(v for v in provider.values() if v),
        )
        if _read(output / "manifest.json") != manifest:
            raise ValueError("managed_manifest_changed")
        if any(_sha(path) != digest for path, digest in trial["tracked"].items()):
            raise ValueError("managed_evidence_changed")
        try:
            after = authorize()
        except Exception:
            after = {"allowed": False}
        current = after.get("allowed") is True and all(
            after.get(k) == before.get(k)
            for k in ("sequence", "releaseId", "contentHash", "exportHash")
        )
        if execution_guard is not None:
            current = current and _read(root / "execution-ticket.json")["status"] == "completed"
        report, context = trial["report"], trial["context"]
        usage = {
            "loading": "verified" if trial["loaded"] else "unknown",
            "capture": "complete" if trial["capture"] else "partial",
            "usageReview": "unreviewed",
            "declaredRuleIds": sorted(
                {
                    e["payload"]["rule_id"]
                    for e in trial["events"]
                    if e["kind"] == "rule_declaration"
                }
            ),
            "endedAdmission": "allowed" if current else "needs_review",
        }
        status = (
            "unknown"
            if not current
            or not trial["loaded"]
            or not trial["capture"]
            or trial["unitFacts"]["status"] == "incomplete"
            else "passed"
            if trial["accepted"]
            else "failed"
        )
        validation = {
            "kind": "isolated_dev",
            "purpose": "actual",
            "status": status,
            "contentHash": candidate["content_hash"],
            "exportHash": projection["exportHash"],
            "caseId": case.case_id,
            "caseRevision": case.revision,
            "lineageId": case.lineage_id,
            "split": "dev",
            "runId": context["run_id"],
            "attemptId": context["attempt_id"],
            "taskResult": {
                "status": trial["final_receipt"]["status"],
                "accepted": trial["accepted"],
                "receiptMatches": True,
            },
            "skillUsage": usage,
            "receiptVerified": True,
            "loadVerified": trial["loaded"],
            "receipt": {
                "uri": Path(report["receipt"]).as_uri(),
                "sha256": report["receipt_sha256"],
            },
            "branchCoverage": [],
            "boundaryCoverage": [],
            "scope": {"caseIds": [case.case_id], "agent": "claude_code", "mode": "controlled_task"},
        }
        result.update(
            status=status,
            validation=validation,
            errorCode="admission_changed"
            if not current
            else "execution_unverified"
            if trial["unitFacts"]["status"] == "incomplete"
            else None,
        )
    except Exception:
        # Exception text may contain deployment locations or provider credentials.
        result["errorCode"] = "execution_unverified"
    _write(output / "managed-result.json", result)
    return result
